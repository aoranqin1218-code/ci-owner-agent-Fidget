from __future__ import annotations

from typing import Any
import logging

from ci_owner_agent.services.feedback_context_store import FeedbackContextStore
from ci_owner_agent.services.feedback_store import FeedbackStore
from ci_owner_agent.services.failure_identity import build_responsibility_signature
from ci_owner_agent.services.pending_feedback_store import PendingFeedbackStore
from ci_owner_agent.services.wecom_bot_models import ParsedFeedbackIntent, WeComInboundMessage
from ci_owner_agent.services.wecom_feedback_parser import parse_feedback_intent
from ci_owner_agent.services.wecom_user_directory import WeComUserDirectory

HELP_TEXT = """群内反馈命令：
CI-XXXXXX 1 判断正确
CI-XXXXXX 1 责任人改为 @AoranQin-秦奥然
CI-XXXXXX 1 标记偶发
CI-XXXXXX 1 无法定责
查看 CI-XXXXXX
确认 ABCD / 取消 ABCD"""


_OPERATION_LOOKUP_FAILED = object()


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
        if pending is None:
            return _pending_status_text(status)
        if status in {"forbidden", "not_found", "expired", "cancelled", "failed", "stale"}:
            return _pending_status_text(status)
        if status == "applied":
            return self._recover_applied_uncommitted(pending)
        if status not in {"claimed", "applying"}:
            return _pending_status_text(status)
        operation = self._safe_find_operation(pending)
        if operation is _OPERATION_LOOKUP_FAILED:
            return "反馈结果正在确认，请勿重复提交。"
        if operation is None and status == "applying":
            return _pending_status_text(status)
        if operation is not None:
            if operation.get("isCommitted"):
                result = self._safe_reconcile_applied(pending)
                if result is True:
                    return "反馈已提交。"
                if result is False:
                    return _pending_status_text(
                        self._safe_read_pending(pending).get("status", "applying")
                    )
                return "反馈结果正在确认，请勿重复提交。"
            if status == "applying":
                return "反馈结果正在确认，请勿重复提交。"
            if status != "claimed":
                return _pending_status_text(status)
            return self._adopt_prepared_operation(pending)
        if status != "claimed":
            return _pending_status_text(status)
        return self._execute_apply_and_finalize(message, pending)

    def _safe_resolve_current_item(
        self,
        pending: dict[str, Any],
    ):
        """Safely resolve current context and item for a pending feedback.

        Returns ("ok", current_context, current_item) on success,
        ("stale", None, None) when item changed or no longer exists,
        ("error", None, None) when MongoDB or other unrecoverable error occurs.
        """
        try:
            context = pending["feedbackContext"]
            try:
                current_context, current_item = self.contexts.resolve_item(
                    str(context.get("feedbackCode") or context.get("code") or ""),
                    int(context.get("itemIndex") or 0),
                )
            except ValueError:
                return ("stale", None, None)
            if not _same_failure(context, current_item):
                return ("stale", None, None)
            return ("ok", current_context, current_item)
        except Exception:
            logging.getLogger(__name__).exception("Failed to resolve current item")
            return ("error", None, None)

    def _safe_mark_stale(
        self,
        pending: dict[str, Any],
        reason: str,
    ) -> bool | None:
        """Safely mark pending as stale.

        Returns True on success, False on CAS failure, None on DB exception.
        """
        try:
            return self.pending.mark_stale(pending, pending.get("applyToken"), reason)
        except Exception:
            logging.getLogger(__name__).exception("Failed to mark pending stale")
            return None

    def _adopt_prepared_operation(self, pending: dict[str, Any]) -> str:
        """Re-validate context for a claimed pending with a prepared operation,
        then finalize using the current token.

        All paths are exception-safe: DB failures do not escape this method,
        and the prepared operation stays uncommitted when state is uncertain.
        """
        result = self._safe_resolve_current_item(pending)
        if result[0] == "error":
            return "反馈结果正在确认，请勿重复提交。"
        if result[0] == "stale":
            stale_ok = self._safe_mark_stale(pending, "责任项在确认前已更新")
            if stale_ok is True:
                return "该构建的责任项已经更新，本次确认未提交。请根据最新通知重新发起反馈。"
            if stale_ok is False:
                return _pending_status_text(
                    self._safe_read_pending(pending).get("status", "applying")
                )
            return "反馈结果正在确认，请勿重复提交。"
        return self._finalize_prepared_operation(pending)

    def _execute_apply_and_finalize(self, message: WeComInboundMessage, pending: dict[str, Any]) -> str:
        """Execute apply_feedback then finalize the prepared operation.

        Uses _safe_resolve_current_item so that MongoDB failures are not
        mistaken for stale items and do not cause permanent mark_failed.
        """
        resolve_result = self._safe_resolve_current_item(pending)
        if resolve_result[0] == "error":
            return "反馈结果正在确认，请勿重复提交。"
        if resolve_result[0] == "stale":
            stale_ok = self._safe_mark_stale(pending, "责任项在确认前已更新")
            if stale_ok is True:
                return "该构建的责任项已经更新，本次确认未提交。请根据最新通知重新发起反馈。"
            if stale_ok is False:
                return _pending_status_text(
                    self._safe_read_pending(pending).get("status", "applying")
                )
            return "反馈结果正在确认，请勿重复提交。"
        _context, item = resolve_result[1], resolve_result[2]
        try:
            intent = ParsedFeedbackIntent.model_validate(pending["intent"])
            owner_name = intent.target_display_name
            owner_email = None
            if intent.target_userid:
                matches = self.users.search_users(intent.target_userid, limit=1)
                if matches and matches[0].get("wecomUserId") == intent.target_userid:
                    owner_name = matches[0].get("displayName") or owner_name
                    owner_email = matches[0].get("preferredEmail")
            self.feedback.apply_feedback(
                repo=_context["repo"],
                job=_context["job"],
                branch=_context["branch"],
                build_number=_context["buildNumber"],
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
                operation_id=pending.get("operationId"),
                submitted_at=pending.get("operationSubmittedAt"),
                is_committed=False,
            )
        except Exception as exc:
            operation = self._safe_find_operation(pending)
            if operation is _OPERATION_LOOKUP_FAILED:
                return "反馈结果正在确认，请勿重复提交。"
            if operation is None:
                try:
                    if not self.pending.mark_failed(pending, pending.get("applyToken"), str(exc)):
                        return _pending_status_text(
                            self._safe_read_pending(pending).get("status", "applying")
                        )
                except Exception:
                    logging.getLogger(__name__).exception("Failed to mark pending feedback failed")
                    return "反馈结果正在确认，请勿重复提交。"
                return "反馈写入失败，请重新发起反馈。"
            if operation.get("isCommitted"):
                result = self._safe_reconcile_applied(pending)
                if result is True:
                    return "反馈已提交。"
                if result is False:
                    return _pending_status_text(
                        self._safe_read_pending(pending).get("status", "applying")
                    )
                return "反馈结果正在确认，请勿重复提交。"
            return self._finalize_prepared_operation(pending)
        return self._finalize_prepared_operation(pending)

    def _finalize_prepared_operation(self, pending: dict[str, Any]) -> str:
        """Mark pending as applied and activate the prepared operation."""
        try:
            if self.pending.mark_applied(pending, pending.get("applyToken")):
                activation = self._safe_activate_operation(pending)
                if activation in ("committed", "already_committed"):
                    try:
                        context = pending.get("feedbackContext") or {}
                        intent = ParsedFeedbackIntent.model_validate(pending.get("intent") or {})
                        return f"反馈已提交：{context.get('code', '?')} 责任项 {context.get('itemIndex', '?')}（{_action_label(intent.action)}）。"
                    except Exception:
                        return "反馈已提交。"
                return "反馈已接收，结果正在同步。"
        except Exception:
            logging.getLogger(__name__).exception("Failed to mark pending feedback applied")
        refreshed = self._safe_read_pending(pending)
        status = refreshed.get("status", "")
        if status == "applied":
            activation = self._safe_activate_operation(pending)
            if activation in ("committed", "already_committed"):
                return "反馈已提交。"
            return "反馈已接收，结果正在同步。"
        if status == "applying":
            return "反馈已经写入，状态正在协调，请勿重复提交。"
        if status in {"stale", "failed", "cancelled", "expired"}:
            return _pending_status_text(status)
        return "反馈结果正在确认，请勿重复提交。"

    def _recover_applied_uncommitted(self, pending: dict[str, Any]) -> str:
        """Recover a pending that is already applied but may have an uncommitted operation."""
        operation = self._safe_find_operation(pending)
        if operation is _OPERATION_LOOKUP_FAILED:
            return "反馈结果正在确认，请勿重复提交。"
        if operation is None:
            logging.getLogger(__name__).error(
                "Applied pending %s has no operation", pending.get("confirmationCode")
            )
            return "反馈结果正在确认，请勿重复提交。"
        if operation.get("isCommitted"):
            return "该确认码已经提交过。"
        activation = self._safe_activate_operation(pending)
        if activation in ("committed", "already_committed"):
            return "反馈已提交。"
        return "反馈已接收，结果正在同步。"

    def _safe_find_operation(self, pending: dict[str, Any]) -> dict | None:
        try:
            return self.feedback.find_by_operation_id(pending.get("operationId"))
        except Exception:
            logging.getLogger(__name__).exception("Failed to query operation")
            return _OPERATION_LOOKUP_FAILED

    def _safe_read_pending(self, pending: dict[str, Any]) -> dict[str, Any]:
        try:
            return self.pending.collection.find_one(
                {"confirmationCode": pending["confirmationCode"]}
            ) or {}
        except Exception:
            logging.getLogger(__name__).exception("Failed to read pending status")
            return {"status": pending.get("status", "applying")}

    def _safe_reconcile_applied(self, pending: dict[str, Any]) -> bool | None:
        try:
            return self.pending.reconcile_applied(
                pending["confirmationCode"], pending["operationId"]
            )
        except Exception:
            logging.getLogger(__name__).exception("Failed to reconcile applied")
            return None

    def _safe_activate_operation(self, pending: dict[str, Any]) -> str:
        try:
            return self.pending.activate_operation_if_pending_applied(
                pending["confirmationCode"],
                pending["operationId"],
                self.feedback.collection,
            )
        except Exception:
            logging.getLogger(__name__).exception("Failed to activate operation")
            return "error"

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
        "stale": "该确认码对应的责任项已经更新，请重新发起反馈。",
        "already_processed": "该确认码已经处理，不能重复使用。",
    }.get(status, f"确认码状态为 {status}，不能继续操作。")


def _same_failure(pending_context: dict[str, Any], current_item: dict[str, Any]) -> bool:
    pending_id = str(pending_context.get("failureId") or "").strip()
    current_id = str(current_item.get("failureId") or "").strip()
    if pending_id and current_id:
        return pending_id == current_id
    pending_signature = _canonical_signature(pending_context.get("failureSignature"))
    current_signature = _canonical_signature(current_item.get("failureSignature"))
    return bool(pending_signature and current_signature and pending_signature == current_signature)


def _canonical_signature(value: Any) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    return build_responsibility_signature(failure_title=None, failure_summary=None, existing_signature=raw)


class StaleFeedbackError(Exception):
    pass
