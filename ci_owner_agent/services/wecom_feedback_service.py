from __future__ import annotations

from typing import Any

from ci_owner_agent.services.feedback_context_store import FeedbackContextStore
from ci_owner_agent.services.feedback_store import FeedbackStore
from ci_owner_agent.services.pending_feedback_store import PendingFeedbackStore
from ci_owner_agent.services.wecom_bot_models import ParsedFeedbackIntent, WeComInboundMessage
from ci_owner_agent.services.wecom_feedback_parser import parse_feedback_intent
from ci_owner_agent.services.wecom_user_directory import WeComUserDirectory

HELP_TEXT = """群内反馈命令：
CI-XXXXXX 1 判断正确
CI-XXXXXX 1 责任人改为 @某人
CI-XXXXXX 1 标记偶发
CI-XXXXXX 1 无法定责
查看 CI-XXXXXX
确认 ABCD / 取消 ABCD"""


class WeComFeedbackService:
    def __init__(self, history_store: Any, *, context_ttl_days: int = 30, confirm_ttl_seconds: int = 300) -> None:
        self.history_store = history_store
        self.contexts = FeedbackContextStore(history_store, context_ttl_days)
        self.pending = PendingFeedbackStore(history_store, confirm_ttl_seconds)
        self.feedback = FeedbackStore(history_store)
        self.users = WeComUserDirectory(history_store)
        self.confirm_ttl_seconds = confirm_ttl_seconds

    def handle(self, message: WeComInboundMessage) -> str:
        intent = parse_feedback_intent(message)
        if intent.intent_type == "help":
            return HELP_TEXT
        if intent.intent_type == "unknown":
            return intent.error or "未识别命令，请发送“帮助”查看用法。"
        if intent.intent_type == "list_feedback":
            return self._list(intent)
        if intent.intent_type == "create_feedback":
            return self._create(message, intent)
        if intent.intent_type == "cancel_pending":
            status = self.pending.cancel(intent.confirmation_code or "", message.sender_userid)
            return _pending_status_text(status, cancelled=True)
        if intent.intent_type == "confirm_pending":
            return self._confirm(message, intent.confirmation_code or "")
        return "未识别命令，请发送“帮助”查看用法。"

    def _create(self, message: WeComInboundMessage, intent: ParsedFeedbackIntent) -> str:
        try:
            context, item = self.contexts.resolve_item(intent.feedback_code or "", intent.item_index or 0)
        except ValueError as exc:
            return str(exc)
        pending = self.pending.create(message=message, context=context, item=item, intent=intent)
        owner = item.get("owner") or {}
        action_lines = _action_lines(intent, owner)
        minutes = max(1, self.confirm_ttl_seconds // 60)
        return "\n".join(
            [
                "请确认反馈：",
                "",
                f"构建：{context['repo']} / {context['branch']} / {context['job']} #{context['buildNumber']}",
                f"责任项：{item['itemIndex']}. {item.get('failureTitle') or '-'}",
                *action_lines,
                f"提交人：{message.sender_name or message.sender_userid}",
                "",
                f"回复“确认 {pending['confirmationCode']}”提交，回复“取消 {pending['confirmationCode']}”放弃。",
                f"确认码 {minutes} 分钟内有效。",
            ]
        )

    def _confirm(self, message: WeComInboundMessage, code: str) -> str:
        status, pending = self.pending.claim(code, message.sender_userid)
        if status != "claimed" or pending is None:
            return _pending_status_text(status)
        context = pending["feedbackContext"]
        item = context["item"]
        intent = ParsedFeedbackIntent.model_validate(pending["intent"])
        owner_name = intent.target_display_name
        owner_email = None
        if intent.target_userid:
            matches = self.users.search_users(intent.target_userid, limit=1)
            if matches and matches[0].get("wecomUserId") == intent.target_userid:
                owner_name = matches[0].get("displayName") or owner_name
                owner_email = matches[0].get("preferredEmail")
        try:
            self.feedback.apply_feedback(
                repo=context["repo"],
                job=context["job"],
                branch=context["branch"],
                build_number=context["buildNumber"],
                failure_id=item.get("failureId"),
                failure_signature=item.get("failureSignature"),
                action=intent.action or "",
                owner_name=owner_name,
                owner_email=owner_email,
                owner_type="high_confidence",
                owner_wecom_userid=intent.target_userid,
                reviewer=pending.get("senderName") or pending.get("senderUserId"),
                reviewer_wecom_userid=pending.get("senderUserId"),
                note=intent.note,
                source="wecom_bot",
            )
        except Exception as exc:
            self.pending.mark_failed(pending, str(exc))
            return f"反馈写入失败：{exc}"
        self.pending.mark_applied(pending)
        return f"反馈已提交：{context['code']} 责任项 {item['itemIndex']}（{_action_label(intent.action)}）。"

    def _list(self, intent: ParsedFeedbackIntent) -> str:
        context = self.contexts.get_active(intent.feedback_code or "")
        if context is None:
            return "反馈码不存在或已过期。"
        docs = self.feedback.list_feedback(
            repo=context["repo"], job=context["job"], branch=context["branch"], build_number=context["buildNumber"]
        )
        if not docs:
            return f"{context['code']} 当前没有有效反馈。"
        item_by_failure = {item.get("failureId"): item.get("itemIndex") for item in context.get("responsibilityItems") or []}
        lines = [f"{context['code']} 当前有效反馈："]
        for doc in docs:
            owner = (doc.get("correctedOwner") or {}).get("name")
            suffix = f" → {owner}" if owner else ""
            lines.append(f"- 责任项 {item_by_failure.get(doc.get('failureId'), '?')}：{_action_label(doc.get('action'))}{suffix}")
        return "\n".join(lines)


def _action_lines(intent: ParsedFeedbackIntent, owner: dict[str, Any]) -> list[str]:
    original = owner.get("name") or "未识别"
    if intent.action == "correct_owner":
        return [f"原责任人：{original}", f"新责任人：{intent.target_display_name or intent.target_userid}"]
    return [f"原责任人：{original}", f"操作：{_action_label(intent.action)}"]


def _action_label(action: str | None) -> str:
    return {
        "confirm_owner": "判断正确",
        "correct_owner": "修正责任人",
        "mark_flaky": "标记偶发",
        "mark_no_owner": "无法定责",
    }.get(action or "", action or "未知")


def _pending_status_text(status: str, cancelled: bool = False) -> str:
    return {
        "cancelled": "待确认反馈已取消。" if cancelled else "该确认码已取消。",
        "forbidden": "只能由发起该反馈的用户确认或取消。",
        "expired": "确认码已过期，请重新发起反馈。",
        "not_found": "确认码不存在。",
        "applied": "该确认码已经提交过。",
        "applying": "该确认码正在处理中，请勿重复提交。",
        "failed": "该确认码上次写入失败，请重新发起反馈。",
        "already_processed": "该确认码已经处理，不能重复使用。",
    }.get(status, f"确认码状态为 {status}，不能继续操作。")

