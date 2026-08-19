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
from ci_owner_agent.services.wecom_feedback_cards import (
    action_label as _action_label,
    build_confirmation_card,
    build_status_card,
    card_status_desc as _card_status_desc,
    card_status_title as _card_status_title,
)

HELP_TEXT = """发送命令请先@机器人
群内反馈命令：
CI-XXXXXX 1 判断正确
CI-XXXXXX 1 责任人改为 @Lisi-李四
CI-XXXXXX 2 标记偶发
CI-XXXXXX 1 无法定责
查看 CI-XXXXXX
中间数字为责任项序号，另外支持自然语言反馈。"""


_OPERATION_LOOKUP_FAILED = object()


class WeComFeedbackService:
    def __init__(
        self,
        history_store: Any,
        *,
        context_ttl_days: int = 30,
        confirm_ttl_seconds: int = 300,
        ai_parser: WeComFeedbackAiParserProtocol | None = None,
        card_action_url: str | None = None,
    ) -> None:
        self.history_store = history_store
        self.contexts = FeedbackContextStore(history_store, context_ttl_days)
        self.pending = PendingFeedbackStore(history_store, confirm_ttl_seconds)
        self.feedback = FeedbackStore(history_store)
        self.confirm_ttl_seconds = confirm_ttl_seconds
        self.ai_parser = ai_parser
        self.card_action_url = (
            str(card_action_url or "").strip()
            or "https://work.weixin.qq.com/"
        )
    def _build_updated_card(
        self, *, title: str, desc: str, task_id: str, status: str, userids: list[str] | None = None
    ) -> dict[str, Any]:
        return build_status_card(
            title=title,
            desc=desc,
            task_id=task_id,
            action_url=self.card_action_url,
            status=status,
            userids=userids,
        )



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
                template_card=self._build_updated_card(
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
                template_card=self._build_updated_card(
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
        return WeComBotReply(
            reply_type="template_card",
            template_card=build_confirmation_card(
                original_owner=(item.get("owner") or {}).get("name") or "未识别",
                target_owner=intent.target_display_name or intent.target_userid,
                action=intent.action,
                item_index=item["itemIndex"],
                repo=context["repo"],
                build_number=context["buildNumber"],
                job=context["job"],
                branch=context["branch"],
                sender=message.sender_name or message.sender_userid,
                failure_title=item.get("failureTitle"),
                note=intent.note,
                confirm_ttl_seconds=self.confirm_ttl_seconds,
                task_id=pending["cardTaskId"],
            ),
        )
    def _confirm_by_card(self, pending: dict[str, Any], event: WeComTemplateCardEvent) -> WeComBotReply:
        if pending.get("senderUserId") != event.sender_userid:
            return WeComBotReply(
                reply_type="template_card",
                template_card=self._build_updated_card(
                    title="无权限",
                    desc="只有反馈发起人可以确认或取消。",
                    task_id=event.task_id,
                    status="forbidden",
                    userids=[event.sender_userid],
                ),
            )
        status, claimed = self.pending.claim(pending["confirmationCode"], event.sender_userid)
        if claimed is not None:
            pending = claimed
        if claimed is None:
            return WeComBotReply(
                reply_type="template_card",
                template_card=self._build_updated_card(
                    title=_card_status_title(status, default="操作失败"),
                    desc=_card_status_desc(status, default="请稍后重试。"),
                    task_id=event.task_id,
                    status=status,
                ),
            )
        if status == "forbidden":
            return WeComBotReply(
                reply_type="template_card",
                template_card=self._build_updated_card(
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
                template_card=self._build_updated_card(
                    title=_card_status_title(status),
                    desc=_card_status_desc(status),
                    task_id=event.task_id,
                    status=status,
                ),
            )
        if status == "applied":
            return self._recover_applied_operation(pending, event)
        # Active applying lease: another handler owns this pending
        if status == "applying":
            return WeComBotReply(
                reply_type="template_card",
                template_card=self._build_updated_card(
                    title="\u5904\u7406\u4e2d",
                    desc="\u53cd\u9988\u6b63\u5728\u63d0\u4ea4\uff0c\u8bf7\u52ff\u91cd\u590d\u64cd\u4f5c\u3002",
                    task_id=event.task_id,
                    status="applying",
                ),
            )
        # Only "claimed" requests may proceed to stale check and operation
        if status != "claimed":
            return WeComBotReply(
                reply_type="template_card",
                template_card=self._build_updated_card(
                    title=_card_status_title(status, default="\u64cd\u4f5c\u5931\u8d25"),
                    desc=_card_status_desc(status, default="\u8bf7\u7a0d\u540e\u91cd\u8bd5\u3002"),
                    task_id=event.task_id,
                    status=status,
                ),
            )

        # Stale check before operation write
        resolve_result = self._safe_resolve_current_item(pending)
        if resolve_result[0] == "error":
            return WeComBotReply(
                reply_type="template_card",
                template_card=self._build_updated_card(
                    title="确认中",
                    desc="反馈结果正在确认，请稍后重试。",
                    task_id=event.task_id,
                    status="applying",
                ),
            )
        if resolve_result[0] == "stale":
            stale_ok = self._safe_mark_stale(pending, "责任项在确认前已更新")
            if stale_ok is True:
                return WeComBotReply(
                    reply_type="template_card",
                    template_card=self._build_updated_card(
                        title="反馈已失效",
                        desc="该责任项已经更新，请重新发起反馈。",
                        task_id=event.task_id,
                        status="stale",
                    ),
                )
            return WeComBotReply(
                reply_type="template_card",
                template_card=self._build_updated_card(
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

        # ok: use fresh context/item for operation
        _current_context, _current_item = resolve_result[1], resolve_result[2]
        operation = self._safe_find_operation(pending)
        if operation is _OPERATION_LOOKUP_FAILED:
            return WeComBotReply(
                reply_type="template_card",
                template_card=self._build_updated_card(
                    title="确认中",
                    desc="反馈结果正在确认，请勿重复提交。",
                    task_id=event.task_id,
                    status="applying",
                ),
            )
        if operation is not None:
            return self._complete_existing_operation(pending, event, operation)
        try:
            intent = ParsedFeedbackIntent.model_validate(pending["intent"])
            owner_name = intent.target_display_name
            owner_email = None

            self.feedback.apply_feedback(
                repo=_current_context["repo"],
                job=_current_context["job"],
                branch=_current_context["branch"],
                build_number=_current_context["buildNumber"],
                failure_id=_current_item.get("failureId"),
                failure_signature=_current_item.get("failureSignature"),
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
                    template_card=self._build_updated_card(
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
                            template_card=self._build_updated_card(
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
                        template_card=self._build_updated_card(
                            title="确认中",
                            desc="反馈结果正在确认，请勿重复提交。",
                            task_id=event.task_id,
                            status="applying",
                        ),
                    )
                return WeComBotReply(
                    reply_type="template_card",
                    template_card=self._build_updated_card(
                        title="反馈写入失败",
                        desc="请重新发起反馈。",
                        task_id=event.task_id,
                        status="failed",
                    ),
                )
            return self._complete_existing_operation(pending, event, operation)
        return self._finalize_card(pending, event)

    def _complete_existing_operation(
        self,
        pending: dict[str, Any],
        event: WeComTemplateCardEvent,
        operation: dict[str, Any],
    ) -> WeComBotReply:
        """Finalize a prepared operation without writing a second feedback record."""
        if not operation.get("isCommitted"):
            return self._finalize_card(pending, event)
        reconciled = self._safe_reconcile_applied(pending)
        if reconciled is True:
            return self._finalize_card(pending, event)
        if reconciled is False:
            current_status = self._safe_read_pending(pending).get("status", "applying")
            return WeComBotReply(
                reply_type="template_card",
                template_card=self._build_updated_card(
                    title=_card_status_title(current_status),
                    desc=_card_status_desc(current_status),
                    task_id=event.task_id,
                    status="applying",
                ),
            )
        return WeComBotReply(
            reply_type="template_card",
            template_card=self._build_updated_card(
                title="确认中",
                desc="反馈结果正在确认，请勿重复提交。",
                task_id=event.task_id,
                status="applying",
            ),
        )

    def _cancel_by_card(self, pending: dict[str, Any], event: WeComTemplateCardEvent) -> WeComBotReply:
        if pending.get("senderUserId") != event.sender_userid:
            return WeComBotReply(
                reply_type="template_card",
                template_card=self._build_updated_card(
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
            template_card=self._build_updated_card(
                title=_card_status_title(status, default="已取消"),
                desc=_card_status_desc(status, default="未写入正式反馈。"),
                task_id=event.task_id,
                status=status,
            ),
        )

    def _finalize_card(self, pending: dict[str, Any], event: WeComTemplateCardEvent) -> WeComBotReply:
        try:
            if self.pending.mark_applied(pending, pending.get("applyToken")):
                return self._activate_operation_reply(pending, event)
        except Exception:
            logging.getLogger(__name__).exception("Failed to mark pending feedback applied")
        refreshed = self._safe_read_pending(pending)
        status = refreshed.get("status", "")
        if status == "applied":
            return self._activate_operation_reply(pending, event)
        if status == "applying":
            return WeComBotReply(
                reply_type="template_card",
                template_card=self._build_updated_card(
                    title="处理中",
                    desc="反馈正在提交，请勿重复操作。",
                    task_id=event.task_id,
                    status="applying",
                ),
            )
        if status in {"stale", "failed", "cancelled", "expired"}:
            return WeComBotReply(
                reply_type="template_card",
                template_card=self._build_updated_card(
                    title=_card_status_title(status),
                    desc=_card_status_desc(status),
                    task_id=event.task_id,
                    status=status,
                ),
            )
        return WeComBotReply(
            reply_type="template_card",
            template_card=self._build_updated_card(
                title="确认中",
                desc="反馈结果正在确认，请勿重复提交。",
                task_id=event.task_id,
                status="applying",
            ),
        )

    def _activate_operation_reply(
        self, pending: dict[str, Any], event: WeComTemplateCardEvent
    ) -> WeComBotReply:
        activation = self._safe_activate_operation(pending)
        if activation in ("committed", "already_committed"):
            return self._completed_reply(event)
        return self._syncing_reply(event)

    def _completed_reply(self, event: WeComTemplateCardEvent) -> WeComBotReply:
        return WeComBotReply(
            reply_type="template_card",
            template_card=self._build_updated_card(
                title="反馈已提交",
                desc="该反馈已成功写入。",
                task_id=event.task_id,
                status="completed",
            ),
        )

    def _syncing_reply(self, event: WeComTemplateCardEvent) -> WeComBotReply:
        return WeComBotReply(
            reply_type="template_card",
            template_card=self._build_updated_card(
                title="反馈已接收",
                desc="结果正在同步。",
                task_id=event.task_id,
                status="applied",
            ),
        )


    def _recover_applied_operation(self, pending: dict[str, Any], event: WeComTemplateCardEvent) -> WeComBotReply:
        """Recover an already-applied pending.

        applied means the confirm CAS was accepted.  This method
        must NOT re-check stale, call mark_stale, call mark_failed,
        or call mark_applied.  It only looks up the prepared
        operation and activates it if needed.
        """
        operation = self._safe_find_operation(pending)
        if operation is _OPERATION_LOOKUP_FAILED:
            return WeComBotReply(
                reply_type="template_card",
                template_card=self._build_updated_card(
                    title="\u7ed3\u679c\u6b63\u5728\u540c\u6b65",
                    desc="\u53cd\u9988\u5df2\u63a5\u53d7\uff0c\u7ed3\u679c\u6b63\u5728\u540c\u6b65\u3002",
                    task_id=event.task_id,
                    status="applied",
                ),
            )
        if operation is None:
            logging.getLogger(__name__).error(
                "Applied pending without operation: confirmationCode=%s",
                pending.get("confirmationCode", "?"),
            )
            return WeComBotReply(
                reply_type="template_card",
                template_card=self._build_updated_card(
                    title="\u7ed3\u679c\u6b63\u5728\u540c\u6b65",
                    desc="\u53cd\u9988\u5df2\u63a5\u53d7\uff0c\u7ed3\u679c\u6b63\u5728\u540c\u6b65\u3002",
                    task_id=event.task_id,
                    status="applied",
                ),
            )
        if operation.get("isCommitted"):
            return WeComBotReply(
                reply_type="template_card",
                template_card=self._build_updated_card(
                    title="\u53cd\u9988\u5df2\u63d0\u4ea4",
                    desc="\u8be5\u53cd\u9988\u5df2\u6210\u529f\u5199\u5165\u3002",
                    task_id=event.task_id,
                    status="completed",
                ),
            )
        activation = self._safe_activate_operation(pending)
        if activation in ("committed", "already_committed"):
            return WeComBotReply(
                reply_type="template_card",
                template_card=self._build_updated_card(
                    title="\u53cd\u9988\u5df2\u63d0\u4ea4",
                    desc="\u8be5\u53cd\u9988\u5df2\u6210\u529f\u5199\u5165\u3002",
                    task_id=event.task_id,
                    status="completed",
                ),
            )
        return WeComBotReply(
            reply_type="template_card",
            template_card=self._build_updated_card(
                title="\u7ed3\u679c\u6b63\u5728\u540c\u6b65",
                desc="\u53cd\u9988\u5df2\u63a5\u53d7\uff0c\u7ed3\u679c\u6b63\u5728\u540c\u6b65\u3002",
                task_id=event.task_id,
                status="applied",
            ),
        )

    # ---- internal helpers ----

    def _safe_resolve_current_item(
        self,
        pending: dict[str, Any],
    ) -> tuple[
        str,
        dict[str, Any] | None,
        dict[str, Any] | None,
    ]:
        try:
            context = pending["feedbackContext"]
            try:
                current_context, current_item = self.contexts.resolve_item(
                    str(context.get("feedbackCode") or context.get("code") or ""),
                    int(context.get("itemIndex") or 0),
                )
            except ValueError:
                return "stale", None, None
            if not _same_failure(context, current_item):
                return "stale", None, None
            return "ok", current_context, current_item
        except Exception:
            logging.getLogger(__name__).exception(
                "Failed to resolve current item"
            )
            return "error", None, None

    def _safe_mark_stale(
        self,
        pending: dict[str, Any],
        reason: str,
    ) -> bool | None:
        try:
            return self.pending.mark_stale(
                pending,
                pending.get("applyToken"),
                reason,
            )
        except Exception:
            logging.getLogger(__name__).exception(
                "Failed to mark pending stale"
            )
            return None

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
