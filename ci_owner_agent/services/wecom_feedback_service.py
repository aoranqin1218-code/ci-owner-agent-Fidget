from __future__ import annotations

from typing import Any
import logging

from ci_owner_agent.services.feedback_context_store import FeedbackContextStore
from ci_owner_agent.services.feedback_store import FeedbackStore
from ci_owner_agent.services.failure_identity import build_responsibility_signature
from ci_owner_agent.services.pending_feedback_store import PendingFeedbackStore
from ci_owner_agent.services.wecom_bot_models import (
    ParsedFeedbackIntent,
    WeComBotReply,
    WeComInboundMessage,
    WeComTemplateCardEvent,
)
from ci_owner_agent.services.wecom_feedback_parser import parse_fixed_feedback_intent
from ci_owner_agent.services.wecom_feedback_ai_parser import WeComFeedbackAiParserProtocol

HELP_TEXT = """群内反馈命令：
CI-XXXXXX 1 判断正确
CI-XXXXXX 1 责任人改为 @AoranQin-秦奥然
CI-XXXXXX 1 标记偶发
CI-XXXXXX 1 无法定责
查看 CI-XXXXXX"""


_OPERATION_LOOKUP_FAILED = object()


class WeComFeedbackService:
    def __init__(
        self,
        history_store: Any,
        *,
        context_ttl_days: int = 30,
        confirm_ttl_seconds: int = 300,
        ai_parser: WeComFeedbackAiParserProtocol | None = None,
    ) -> None:
        self.history_store = history_store
        self.contexts = FeedbackContextStore(history_store, context_ttl_days)
        self.pending = PendingFeedbackStore(history_store, confirm_ttl_seconds)
        self.feedback = FeedbackStore(history_store)
        self.confirm_ttl_seconds = confirm_ttl_seconds
        self.ai_parser = ai_parser

    def handle_text(self, message: WeComInboundMessage) -> WeComBotReply:
        """Handle a text message. Returns a reply (text or template_card)."""
        intent = parse_fixed_feedback_intent(message)
        if intent is None and self.ai_parser is not None:
            intent = self.ai_parser.parse(message)
        elif intent is None:
            intent = ParsedFeedbackIntent(
                intent_type="unknown",
                error="自然语言解析暂不可用，请使用固定命令，或发送“帮助”查看用法。",
            )

        if intent.intent_type == "help":
            return WeComBotReply(reply_type="text", text=HELP_TEXT)
        if intent.intent_type == "unknown":
            return WeComBotReply(
                reply_type="text",
                text=intent.error or "未识别命令，请发送“帮助”查看用法。",
            )
        if intent.intent_type == "list_feedback":
            return WeComBotReply(reply_type="text", text=self._list(intent))
        if intent.intent_type == "create_feedback":
            return self._create(message, intent)
        return WeComBotReply(
            reply_type="text",
            text="未识别命令，请发送“帮助”查看用法。",
        )

    def handle_template_card_event(
        self, event: WeComTemplateCardEvent
    ) -> WeComBotReply:
        """Handle a template card button click event."""
        pending = self.pending.get_by_card_task_id(event.task_id)
        if pending is None:
            return WeComBotReply(
                reply_type="template_card",
                template_card=_build_updated_card(
                    title="反馈已失效",
                    desc="该反馈记录不存在或已过期，请重新发起。",
                    task_id=event.task_id,
                    status="expired",
                ),
            )

        if event.button_key == "confirm":
            return self._confirm_by_card(pending, event)
        elif event.button_key == "cancel":
            return self._cancel_by_card(pending, event)
        else:
            return WeComBotReply(
                reply_type="template_card",
                template_card=_build_updated_card(
                    title="未知操作",
                    desc="该按钮操作不可识别。",
                    task_id=event.task_id,
                    status="failed",
                ),
            )

    def _create(self, message: WeComInboundMessage, intent: ParsedFeedbackIntent) -> WeComBotReply:
        try:
            context, item = self.contexts.resolve_item(intent.feedback_code or "", intent.item_index or 0)
        except ValueError as exc:
            return WeComBotReply(reply_type="text", text=str(exc))
        pending = self.pending.create(message=message, context=context, item=item, intent=intent)
        owner = item.get("owner") or {}
        minutes = max(1, self.confirm_ttl_seconds // 60)
        action_lines = _action_lines(intent, owner)
        desc_lines = [
            f"构建：{context['repo']} / {context['branch']} / {context['job']} #{context['buildNumber']}",
            f"责任项：{item['itemIndex']}. {item.get('failureTitle') or '-'}",
            *action_lines,
            f"发起人：{message.sender_name or message.sender_userid}",
            f"有效期：{minutes} 分钟",
        ]
        return WeComBotReply(
            reply_type="template_card",
            template_card=_build_button_card(
                title="确认 CI 反馈",
                desc="请核对以下反馈内容",
                desc_lines=desc_lines,
                task_id=pending["cardTaskId"],
            ),
        )

    def _confirm_by_card(self, pending: dict[str, Any], event: WeComTemplateCardEvent) -> WeComBotReply:
        if pending.get("senderUserId") != event.sender_userid:
            return WeComBotReply(
                reply_type="template_card",
                template_card=_build_updated_card(
                    title="无权限",
                    desc="只有反馈发起人可以确认或取消。",
                    task_id=event.task_id,
                    status="forbidden",
                    userids=[event.sender_userid],
                ),
            )
        status, claimed = self.pending.claim(pending["confirmationCode"], event.sender_userid)
        if claimed is None:
            return WeComBotReply(
                reply_type="template_card",
                template_card=_build_updated_card(
                    title=_card_status_title(status, default="操作失败"),
                    desc=_card_status_desc(status, default="请稍后重试。"),
                    task_id=event.task_id,
                    status=status,
                ),
            )
        if status == "forbidden":
            return WeComBotReply(
                reply_type="template_card",
                template_card=_build_updated_card(
                    title="无权限",
                    desc="只有反馈发起人可以确认或取消。",
                    task_id=event.task_id,
                    status="forbidden",
                    userids=[event.sender_userid],
                ),
            )
        if status in {"expired", "cancelled", "failed", "stale"}:
            return WeComBotReply(
                reply_type="template_card",
                template_card=_build_updated_card(
                    title=_card_status_title(status),
                    desc=_card_status_desc(status),
                    task_id=event.task_id,
                    status=status,
                ),
            )
        if status == "applied":
            return self._recover_and_finalize(pending, event)
        if status not in {"claimed", "applying"}:
            return WeComBotReply(
                reply_type="template_card",
                template_card=_build_updated_card(
                    title=_card_status_title(status, default="操作失败"),
                    desc=_card_status_desc(status, default="请稍后重试。"),
                    task_id=event.task_id,
                    status=status,
                ),
            )
        operation = self._safe_find_operation(pending)
        if operation is _OPERATION_LOOKUP_FAILED:
            return WeComBotReply(
                reply_type="template_card",
                template_card=_build_updated_card(
                    title="确认中",
                    desc="反馈结果正在确认，请勿重复提交。",
                    task_id=event.task_id,
                    status="applying",
                ),
            )
        if operation is None and status == "applying":
            return WeComBotReply(
                reply_type="template_card",
                template_card=_build_updated_card(
                    title="处理中",
                    desc="反馈正在提交，请勿重复操作。",
                    task_id=event.task_id,
                    status="applying",
                ),
            )
        if operation is not None:
            if operation.get("isCommitted"):
                result = self._safe_reconcile_applied(pending)
                if result is True:
                    return self._finalize_card(pending, event)
                if result is False:
                    return WeComBotReply(
                        reply_type="template_card",
                        template_card=_build_updated_card(
                            title=_card_status_title(
                                self._safe_read_pending(pending).get("status", "applying")
                            ),
                            desc=_card_status_desc(
                                self._safe_read_pending(pending).get("status", "applying")
                            ),
                            task_id=event.task_id,
                            status="applying",
                        ),
                    )
                return WeComBotReply(
                    reply_type="template_card",
                    template_card=_build_updated_card(
                        title="确认中",
                        desc="反馈结果正在确认，请勿重复提交。",
                        task_id=event.task_id,
                        status="applying",
                    ),
                )
            if status == "claimed":
                return self._finalize_card(pending, event)
            return self._finalize_card(pending, event)
        context = pending.get("feedbackContext") or {}
        try:
            intent = ParsedFeedbackIntent.model_validate(pending["intent"])
            owner_name = intent.target_display_name
            owner_email = None
            self.feedback.apply_feedback(
                repo=context["repo"],
                job=context["job"],
                branch=context["branch"],
                build_number=context["buildNumber"],
                failure_id=context.get("failureId"),
                failure_signature=context.get("failureSignature"),
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
                return WeComBotReply(
                    reply_type="template_card",
                    template_card=_build_updated_card(
                        title="确认中",
                        desc="反馈结果正在确认，请勿重复提交。",
                        task_id=event.task_id,
                        status="applying",
                    ),
                )
            if operation is None:
                try:
                    if not self.pending.mark_failed(pending, pending.get("applyToken"), str(exc)):
                        return WeComBotReply(
                            reply_type="template_card",
                            template_card=_build_updated_card(
                                title=_card_status_title(
                                    self._safe_read_pending(pending).get("status", "applying")
                                ),
                                desc=_card_status_desc(
                                    self._safe_read_pending(pending).get("status", "applying")
                                ),
                                task_id=event.task_id,
                                status="applying",
                            ),
                        )
                except Exception:
                    logging.getLogger(__name__).exception("Failed to mark pending feedback failed")
                    return WeComBotReply(
                        reply_type="template_card",
                        template_card=_build_updated_card(
                            title="确认中",
                            desc="反馈结果正在确认，请勿重复提交。",
                            task_id=event.task_id,
                            status="applying",
                        ),
                    )
                return WeComBotReply(
                    reply_type="template_card",
                    template_card=_build_updated_card(
                        title="反馈写入失败",
                        desc="请重新发起反馈。",
                        task_id=event.task_id,
                        status="failed",
                    ),
                )
            if operation.get("isCommitted"):
                result = self._safe_reconcile_applied(pending)
                if result is True:
                    return self._finalize_card(pending, event)
                if result is False:
                    return WeComBotReply(
                        reply_type="template_card",
                        template_card=_build_updated_card(
                            title=_card_status_title(
                                self._safe_read_pending(pending).get("status", "applying")
                            ),
                            desc=_card_status_desc(
                                self._safe_read_pending(pending).get("status", "applying")
                            ),
                            task_id=event.task_id,
                            status="applying",
                        ),
                    )
                return WeComBotReply(
                    reply_type="template_card",
                    template_card=_build_updated_card(
                        title="确认中",
                        desc="反馈结果正在确认，请勿重复提交。",
                        task_id=event.task_id,
                        status="applying",
                    ),
                )
            return self._finalize_card(pending, event)
        return self._finalize_card(pending, event)

    def _cancel_by_card(self, pending: dict[str, Any], event: WeComTemplateCardEvent) -> WeComBotReply:
        if pending.get("senderUserId") != event.sender_userid:
            return WeComBotReply(
                reply_type="template_card",
                template_card=_build_updated_card(
                    title="无权限",
                    desc="只有反馈发起人可以确认或取消。",
                    task_id=event.task_id,
                    status="forbidden",
                    userids=[event.sender_userid],
                ),
            )
        status = self.pending.cancel(pending["confirmationCode"], event.sender_userid)
        return WeComBotReply(
            reply_type="template_card",
            template_card=_build_updated_card(
                title=_card_status_title(status, default="已取消"),
                desc=_card_status_desc(status, default="未写入正式反馈。"),
                task_id=event.task_id,
                status=status,
            ),
        )

    def _finalize_card(self, pending: dict[str, Any], event: WeComTemplateCardEvent) -> WeComBotReply:
        try:
            if self.pending.mark_applied(pending, pending.get("applyToken")):
                activation = self._safe_activate_operation(pending)
                if activation in ("committed", "already_committed"):
                    return WeComBotReply(
                        reply_type="template_card",
                        template_card=_build_updated_card(
                            title="反馈已提交",
                            desc="该反馈已成功写入。",
                            task_id=event.task_id,
                            status="completed",
                        ),
                    )
                return WeComBotReply(
                    reply_type="template_card",
                    template_card=_build_updated_card(
                        title="反馈已接收",
                        desc="结果正在同步。",
                        task_id=event.task_id,
                        status="applied",
                    ),
                )
        except Exception:
            logging.getLogger(__name__).exception("Failed to mark pending feedback applied")
        refreshed = self._safe_read_pending(pending)
        status = refreshed.get("status", "")
        if status == "applied":
            activation = self._safe_activate_operation(pending)
            if activation in ("committed", "already_committed"):
                return WeComBotReply(
                    reply_type="template_card",
                    template_card=_build_updated_card(
                        title="反馈已提交",
                        desc="该反馈已成功写入。",
                        task_id=event.task_id,
                        status="completed",
                    ),
                )
            return WeComBotReply(
                reply_type="template_card",
                template_card=_build_updated_card(
                    title="反馈已接收",
                    desc="结果正在同步。",
                    task_id=event.task_id,
                    status="applied",
                ),
            )
        if status == "applying":
            return WeComBotReply(
                reply_type="template_card",
                template_card=_build_updated_card(
                    title="处理中",
                    desc="反馈正在提交，请勿重复操作。",
                    task_id=event.task_id,
                    status="applying",
                ),
            )
        if status in {"stale", "failed", "cancelled", "expired"}:
            return WeComBotReply(
                reply_type="template_card",
                template_card=_build_updated_card(
                    title=_card_status_title(status),
                    desc=_card_status_desc(status),
                    task_id=event.task_id,
                    status=status,
                ),
            )
        return WeComBotReply(
            reply_type="template_card",
            template_card=_build_updated_card(
                title="确认中",
                desc="反馈结果正在确认，请勿重复提交。",
                task_id=event.task_id,
                status="applying",
            ),
        )

    def _recover_and_finalize(self, pending: dict[str, Any], event: WeComTemplateCardEvent) -> WeComBotReply:
        operation = self._safe_find_operation(pending)
        if operation is _OPERATION_LOOKUP_FAILED:
            return WeComBotReply(
                reply_type="template_card",
                template_card=_build_updated_card(
                    title="确认中",
                    desc="反馈结果正在确认，请勿重复提交。",
                    task_id=event.task_id,
                    status="applying",
                ),
            )
        if operation is None:
            try:
                if not self.pending.mark_failed(pending, pending.get("applyToken"), "recovery: operation not found"):
                    return WeComBotReply(
                        reply_type="template_card",
                        template_card=_build_updated_card(
                            title=_card_status_title(
                                self._safe_read_pending(pending).get("status", "applying")
                            ),
                            desc=_card_status_desc(
                                self._safe_read_pending(pending).get("status", "applying")
                            ),
                            task_id=event.task_id,
                            status="applying",
                        ),
                    )
            except Exception:
                logging.getLogger(__name__).exception("Failed to mark pending feedback failed")
                return WeComBotReply(
                    reply_type="template_card",
                    template_card=_build_updated_card(
                        title="确认中",
                        desc="反馈结果正在确认，请勿重复提交。",
                        task_id=event.task_id,
                        status="applying",
                    ),
                )
            return WeComBotReply(
                reply_type="template_card",
                template_card=_build_updated_card(
                    title="反馈写入失败",
                    desc="请重新发起反馈。",
                    task_id=event.task_id,
                    status="failed",
                ),
            )
        if operation.get("isCommitted"):
            result = self._safe_reconcile_applied(pending)
            if result is True:
                return self._finalize_card(pending, event)
            if result is False:
                return WeComBotReply(
                    reply_type="template_card",
                    template_card=_build_updated_card(
                        title=_card_status_title(
                            self._safe_read_pending(pending).get("status", "applying")
                        ),
                        desc=_card_status_desc(
                            self._safe_read_pending(pending).get("status", "applying")
                        ),
                        task_id=event.task_id,
                        status="applying",
                    ),
                )
            return WeComBotReply(
                reply_type="template_card",
                template_card=_build_updated_card(
                    title="确认中",
                    desc="反馈结果正在确认，请勿重复提交。",
                    task_id=event.task_id,
                    status="applying",
                ),
            )
        return self._finalize_card(pending, event)

    # ---- internal helpers ----

    def _safe_find_operation(self, pending: dict[str, Any]) -> Any:
        try:
            operation_id = pending.get("operationId")
            if not operation_id:
                return None
            return self.feedback.collection.find_one({"operationId": operation_id})
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
            suffix = f" -> {owner}" if owner else ""
            lines.append(f"- 责任项 {item_by_failure.get(doc.get('failureId'), '?')}：{_action_label(doc.get('action'))}{suffix}")
        return "\n".join(lines)


def _build_button_card(*, title: str, desc: str, desc_lines: list[str], task_id: str) -> dict[str, Any]:
    return {
        "card_type": "button_interaction",
        "main_title": {"title": title, "desc": desc},
        "sub_title_text": "\n".join(desc_lines),
        "button_list": [
            {"text": "确认提交", "style": 1, "key": "confirm"},
            {"text": "取消", "style": 2, "key": "cancel"},
        ],
        "task_id": task_id,
    }


def _build_updated_card(
    *, title: str, desc: str, task_id: str, status: str, userids: list[str] | None = None
) -> dict[str, Any]:
    card: dict[str, Any] = {
        "card_type": "button_interaction",
        "main_title": {"title": title, "desc": desc},
        "task_id": task_id,
    }
    if userids is not None:
        card["userids"] = userids
    return card


def _card_status_title(status: str, default: str = "操作失败") -> str:
    return {
        "cancelled": "反馈已取消",
        "forbidden": "无权限",
        "expired": "确认已过期",
        "not_found": "反馈不存在",
        "applied": "反馈已提交",
        "applying": "处理中",
        "failed": "反馈写入失败",
        "stale": "反馈已失效",
        "completed": "反馈已提交",
        "already_processed": "已处理",
    }.get(status, default)


def _card_status_desc(status: str, default: str = "请稍后重试。") -> str:
    return {
        "cancelled": "未写入正式反馈。",
        "forbidden": "只有反馈发起人可以确认或取消。",
        "expired": "请重新发起反馈。",
        "not_found": "该反馈记录不存在。",
        "applied": "结果正在同步。",
        "applying": "反馈正在提交，请勿重复操作。",
        "failed": "请重新发起反馈。",
        "stale": "该责任项已更新，请重新发起反馈。",
        "completed": "该反馈已成功写入。",
        "already_processed": "该反馈已经处理。",
    }.get(status, default)


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


class StaleFeedbackError(Exception):
    pass