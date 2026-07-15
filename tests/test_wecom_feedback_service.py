from __future__ import annotations

from ci_owner_agent.schemas import CiResponsibilityNotice
from ci_owner_agent.services.feedback_context_store import FeedbackContextStore
from ci_owner_agent.services.wecom_bot_models import (
    WeComBotReply,
    WeComInboundMessage,
    WeComMentionedUser,
    WeComTemplateCardEvent,
    ParsedFeedbackIntent,
)
from ci_owner_agent.services.wecom_feedback_service import WeComFeedbackService
from ci_owner_agent.services.wecom_feedback_ai_parser import WeComFeedbackAiParserProtocol
from tests.test_history_store import make_store
from tests.test_notification_formatter import item, notice_payload
import datetime as dt


class _RecordingAiParser:
    """Records calls so tests can verify LLM was or was not invoked."""

    def __init__(self):
        self.call_count = 0
        self.return_value = ParsedFeedbackIntent(intent_type="unknown")

    def parse(self, message):
        self.call_count += 1
        return self.return_value


def _setup():
    store = make_store()
    notice = CiResponsibilityNotice.model_validate(notice_payload([item("张三")]))
    key = {"repo": notice.repo, "job": notice.job, "branch": notice.branch, "buildNumber": notice.buildNumber}
    store.notices.update_one(key, {"$set": {**key, "notice": notice.model_dump(mode="json")}}, upsert=True)
    context = FeedbackContextStore(store).get_or_create_for_notice(notice)
    return store, notice, context


def _message(event: str, content: str, userid: str = "wangwu", mentions=()):
    users = [WeComMentionedUser(userid=value, display_name=name) for value, name in mentions]
    return WeComInboundMessage(
        event_key=event,
        chat_id="chat",
        sender_userid=userid,
        sender_name="王五" if userid == "wangwu" else "其他人",
        content=content,
        mentioned_userids=[user.userid for user in users],
        mentioned_users=users,
    )


def test_fixed_command_does_not_call_llm():
    """Fixed command match should NOT call LLM."""
    store, _, context = _setup()
    parser = _RecordingAiParser()
    service = WeComFeedbackService(store, ai_parser=parser)
    reply = service.handle_text(_message("msg:create", f"{context['code']} 1 判断正确"))
    assert reply.reply_type == "template_card"
    assert parser.call_count == 0


def test_non_fixed_command_calls_llm():
    """Non-fixed command should call LLM."""
    store, _, context = _setup()
    parser = _RecordingAiParser()
    parser.return_value = ParsedFeedbackIntent(
        intent_type="create_feedback",
        action="confirm_owner",
        feedback_code=context["code"],
        item_index=1,
    )
    service = WeComFeedbackService(store, ai_parser=parser)
    reply = service.handle_text(_message("msg:nl", f"{context['code']} 第一条判断没问题"))
    assert reply.reply_type == "template_card"
    assert parser.call_count == 1


def test_no_parser_fallback_to_text():
    """When no ai_parser is set, non-fixed commands return text error."""
    store, _, _ = _setup()
    service = WeComFeedbackService(store, ai_parser=None)
    reply = service.handle_text(_message("msg:nl", "随便聊聊"))
    assert reply.reply_type == "text"
    assert "自然语言解析暂不可用" in (reply.text or "")


def test_create_feedback_returns_template_card():
    """Create feedback should return a button_interaction template card."""
    store, _, context = _setup()
    service = WeComFeedbackService(store)
    reply = service.handle_text(_message("msg:create", f"{context['code']} 1 判断正确"))
    assert reply.reply_type == "template_card"
    card = reply.template_card
    assert card is not None
    assert card["card_type"] == "button_interaction"
    assert card["task_id"] is not None
    assert card["task_id"].startswith("ci-feedback-")
    buttons = card.get("button_list", [])
    keys = {b["key"] for b in buttons}
    assert keys == {"confirm", "cancel"}
    # confirmationCode should NOT appear in card
    card_str = str(card)
    assert "confirmationCode" not in card_str
    assert "ABCD" not in card_str
    # Verify pending was created with cardTaskId
    pending = store.wecom_pending_feedback.docs[0]
    assert pending["cardTaskId"] == card["task_id"]
    assert pending["confirmationCode"] is not None


def test_help_returns_text():
    store, _, _ = _setup()
    service = WeComFeedbackService(store)
    reply = service.handle_text(_message("msg:help", "帮助"))
    assert reply.reply_type == "text"
    assert "CI-XXXXXX" in (reply.text or "")


def test_list_returns_text():
    store, _, context = _setup()
    service = WeComFeedbackService(store)
    reply = service.handle_text(_message("msg:list", f"查看 {context['code']}"))
    assert reply.reply_type == "text"
    assert context["code"] in (reply.text or "")


def test_unknown_returns_text():
    store, _, _ = _setup()
    service = WeComFeedbackService(store)
    reply = service.handle_text(_message("msg:unknown", "随便聊聊"))
    assert reply.reply_type == "text"
    assert reply.text is not None


def test_confirm_by_card_commits():
    """Card confirm should go through the full state machine and commit."""
    store, _, context = _setup()
    service = WeComFeedbackService(store)
    create_reply = service.handle_text(_message("msg:create", f"{context['code']} 1 判断正确"))
    task_id = create_reply.template_card["task_id"]
    pending = store.wecom_pending_feedback.docs[0]
    assert pending["status"] == "pending"
    assert store.feedback.docs == []

    event = WeComTemplateCardEvent(
        event_key="wecom-card:msg:confirm1",
        message_id="msg:confirm1",
        sender_userid="wangwu",
        chat_id="chat",
        task_id=task_id,
        button_key="confirm",
    )
    confirm_reply = service.handle_template_card_event(event)
    assert confirm_reply.reply_type == "template_card"
    assert confirm_reply.template_card is not None
    # After confirm, feedback should be committed
    assert len(store.feedback.docs) > 0
    # Pending should be completed
    pending_refreshed = store.wecom_pending_feedback.docs[0]
    assert pending_refreshed["status"] in ("completed", "applied")


def test_cancel_by_card_does_not_write_feedback():
    """Card cancel should NOT write feedback."""
    store, _, context = _setup()
    service = WeComFeedbackService(store)
    create_reply = service.handle_text(_message("msg:create", f"{context['code']} 1 判断正确"))
    task_id = create_reply.template_card["task_id"]

    event = WeComTemplateCardEvent(
        event_key="wecom-card:msg:cancel1",
        message_id="msg:cancel1",
        sender_userid="wangwu",
        chat_id="chat",
        task_id=task_id,
        button_key="cancel",
    )
    cancel_reply = service.handle_template_card_event(event)
    assert cancel_reply.reply_type == "template_card"
    assert store.feedback.docs == []
    pending_refreshed = store.wecom_pending_feedback.docs[0]
    assert pending_refreshed["status"] == "cancelled"


def test_non_originator_cannot_confirm():
    """Only the original sender can confirm."""
    store, _, context = _setup()
    service = WeComFeedbackService(store)
    create_reply = service.handle_text(_message("msg:create", f"{context['code']} 1 判断正确"))
    task_id = create_reply.template_card["task_id"]
    assert len(store.feedback.docs) == 0

    event = WeComTemplateCardEvent(
        event_key="wecom-card:msg:other",
        message_id="msg:other",
        sender_userid="lisi",
        chat_id="chat",
        task_id=task_id,
        button_key="confirm",
    )
    confirm_reply = service.handle_template_card_event(event)
    assert confirm_reply.template_card.get("userids") == ["lisi"]
    assert store.feedback.docs == []


def test_non_originator_cannot_cancel():
    """Only the original sender can cancel."""
    store, _, context = _setup()
    service = WeComFeedbackService(store)
    create_reply = service.handle_text(_message("msg:create", f"{context['code']} 1 判断正确"))
    task_id = create_reply.template_card["task_id"]

    event = WeComTemplateCardEvent(
        event_key="wecom-card:msg:other2",
        message_id="msg:other2",
        sender_userid="lisi",
        chat_id="chat",
        task_id=task_id,
        button_key="cancel",
    )
    cancel_reply = service.handle_template_card_event(event)
    assert cancel_reply.template_card.get("userids") == ["lisi"]
    pending = store.wecom_pending_feedback.docs[0]
    assert pending["status"] == "pending"


def test_duplicate_confirm_is_idempotent():
    """Repeated confirm should not write duplicate operations."""
    store, _, context = _setup()
    service = WeComFeedbackService(store)
    create_reply = service.handle_text(_message("msg:create", f"{context['code']} 1 判断正确"))
    task_id = create_reply.template_card["task_id"]

    event = WeComTemplateCardEvent(
        event_key="wecom-card:msg:dup1",
        message_id="msg:dup1",
        sender_userid="wangwu",
        chat_id="chat",
        task_id=task_id,
        button_key="confirm",
    )
    r1 = service.handle_template_card_event(event)
    assert r1.reply_type == "template_card"

    # Second confirm with same task_id
    event2 = WeComTemplateCardEvent(
        event_key="wecom-card:msg:dup2",
        message_id="msg:dup2",
        sender_userid="wangwu",
        chat_id="chat",
        task_id=task_id,
        button_key="confirm",
    )
    r2 = service.handle_template_card_event(event2)
    assert r2.reply_type == "template_card"
    assert "已提交" in (r2.template_card.get("main_title", {}).get("title", ""))


def test_expired_pending_card():
    """Expired pending should show expired card."""
    store, _, context = _setup()
    service = WeComFeedbackService(store, confirm_ttl_seconds=0)
    create_reply = service.handle_text(_message("msg:create", f"{context['code']} 1 判断正确"))
    task_id = create_reply.template_card["task_id"]

    event = WeComTemplateCardEvent(
        event_key="wecom-card:msg:expired",
        message_id="msg:expired",
        sender_userid="wangwu",
        chat_id="chat",
        task_id=task_id,
        button_key="confirm",
    )
    confirm_reply = service.handle_template_card_event(event)
    assert confirm_reply.reply_type == "template_card"
    title = confirm_reply.template_card.get("main_title", {}).get("title", "")
    assert "过期" in title


def test_card_task_id_matches_pending():
    store, _, context = _setup()
    service = WeComFeedbackService(store)
    reply = service.handle_text(_message("msg:create", f"{context['code']} 1 判断正确"))
    task_id = reply.template_card["task_id"]
    pending = store.wecom_pending_feedback.docs[0]
    assert pending["cardTaskId"] == task_id
    assert task_id.startswith("ci-feedback-")


def test_confirmation_code_not_in_reply():
    store, _, context = _setup()
    service = WeComFeedbackService(store)
    reply = service.handle_text(_message("msg:create", f"{context['code']} 1 判断正确"))
    reply_str = str(reply.model_dump(mode="json"))
    pending = store.wecom_pending_feedback.docs[0]
    code = pending["confirmationCode"]
    assert code not in reply_str


def test_card_confirmation_is_rejected_when_item_changed():
    """Stale responsibility item should prevent confirmation."""
    import copy
    import datetime as dt
    store, notice, context = _setup()
    service = WeComFeedbackService(store)
    reply = service.handle_text(_message("msg:stale1", f"{context['code']} 1 判断正确"))
    task_id = reply.template_card["task_id"]
    assert len(store.feedback.docs) == 0

    # Update the responsibility item to have a different failureId
    ctx_doc = store.feedback_contexts.docs[0]
    old_item = copy.deepcopy(ctx_doc["responsibilityItems"][0])
    old_item["failureId"] = "new-failure-id"
    old_item["failureSignature"] = "new-signature"
    ctx_doc["responsibilityItems"] = [old_item]
    ctx_doc["updatedAt"] = dt.datetime.now(dt.timezone.utc)

    event = WeComTemplateCardEvent(
        event_key="wecom-card:stale1",
        message_id="stale1",
        sender_userid="wangwu",
        chat_id="chat",
        task_id=task_id,
        button_key="confirm",
    )
    confirm_reply = service.handle_template_card_event(event)
    assert confirm_reply.reply_type == "template_card"
    title = confirm_reply.template_card.get("main_title", {}).get("title", "")
    assert "失效" in title
    pending = store.wecom_pending_feedback.docs[0]
    assert pending["status"] == "stale"
    assert len(store.feedback.docs) == 0


def test_card_confirmation_succeeds_when_context_refreshes_but_item_is_same():
    """Context refresh with same failureId should allow confirmation."""
    store, notice, context = _setup()
    service = WeComFeedbackService(store)
    reply = service.handle_text(_message("msg:refresh1", f"{context['code']} 1 判断正确"))
    task_id = reply.template_card["task_id"]

    # Update context but keep the same item
    ctx_doc = store.feedback_contexts.docs[0]
    ctx_doc["updatedAt"] = dt.datetime.now(dt.timezone.utc)

    event = WeComTemplateCardEvent(
        event_key="wecom-card:refresh1",
        message_id="refresh1",
        sender_userid="wangwu",
        chat_id="chat",
        task_id=task_id,
        button_key="confirm",
    )
    confirm_reply = service.handle_template_card_event(event)
    assert confirm_reply.reply_type == "template_card"
    title = confirm_reply.template_card.get("main_title", {}).get("title", "")
    assert "提交" in title


def test_context_lookup_exception_keeps_card_pending_applying():
    """Context query error should keep pending applying, not fail or stale."""
    store, _, context = _setup()
    service = WeComFeedbackService(store)
    reply = service.handle_text(_message("msg:err1", f"{context['code']} 1 判断正确"))
    task_id = reply.template_card["task_id"]

    # Monkey-patch resolve_item to raise exception
    original_resolve = service.contexts.resolve_item
    def _broken_resolve(code, idx):
        raise RuntimeError("lookup failed")
    service.contexts.resolve_item = _broken_resolve

    event = WeComTemplateCardEvent(
        event_key="wecom-card:err1",
        message_id="err1",
        sender_userid="wangwu",
        chat_id="chat",
        task_id=task_id,
        button_key="confirm",
    )
    confirm_reply = service.handle_template_card_event(event)
    assert confirm_reply.reply_type == "template_card"
    title = confirm_reply.template_card.get("main_title", {}).get("title", "")
    assert "确认中" in title
    pending = store.wecom_pending_feedback.docs[0]
    assert pending["status"] == "applying"
    assert len(store.feedback.docs) == 0
    service.contexts.resolve_item = original_resolve


def test_mark_stale_uses_claim_apply_token():
    """mark_stale should use the applyToken from the claim result."""
    store, notice, context = _setup()
    service = WeComFeedbackService(store)
    reply = service.handle_text(_message("msg:token1", f"{context['code']} 1 判断正确"))
    task_id = reply.template_card["task_id"]

    # Update item to make it stale
    ctx_doc = store.feedback_contexts.docs[0]
    old_item = ctx_doc["responsibilityItems"][0].copy()
    old_item["failureId"] = "new-failure-id"
    ctx_doc["responsibilityItems"] = [old_item]

    original_mark_stale = service.pending.mark_stale
    captured_args = []

    def _capturing_mark_stale(doc, token, reason):
        captured_args.append((doc.get("confirmationCode"), token, reason))
        return original_mark_stale(doc, token, reason)
    service.pending.mark_stale = _capturing_mark_stale

    event = WeComTemplateCardEvent(
        event_key="wecom-card:token1",
        message_id="token1",
        sender_userid="wangwu",
        chat_id="chat",
        task_id=task_id,
        button_key="confirm",
    )
    service.handle_template_card_event(event)

    assert len(captured_args) == 1
    code, token, reason = captured_args[0]
    assert token is not None
    assert len(token) > 0
    assert reason == "责任项在确认前已更新"
    service.pending.mark_stale = original_mark_stale


def test_expired_lease_reclaim_rechecks_prepared_operation_stale():
    """Existing prepared operation should be rejected when item changed."""
    store, notice, context = _setup()
    service = WeComFeedbackService(store)
    reply = service.handle_text(_message("msg:prep1", f"{context['code']} 1 判断正确"))
    task_id = reply.template_card["task_id"]

    # Manually create a prepared operation (isCommitted=False)
    pending = store.wecom_pending_feedback.docs[0]
    operation_doc = {
        "operationId": pending["operationId"],
        "isCommitted": False,
        "action": "confirm_owner",
        "failureId": context["responsibilityItems"][0]["failureId"],
    }
    store.feedback.docs.append(operation_doc)

    # Claim to get applying status with prepared operation
    status, claimed = service.pending.claim(pending["confirmationCode"], "wangwu")
    pending = claimed  # use claimed doc

    # Expire the lease so second click can reclaim
    pending_doc = store.wecom_pending_feedback.docs[0]
    pending_doc["applyLeaseUntil"] = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1)

    # Change the responsibility item
    ctx_doc = store.feedback_contexts.docs[0]
    old_item = ctx_doc["responsibilityItems"][0].copy()
    old_item["failureId"] = "changed-failure-id"
    old_item["failureSignature"] = "changed-signature"
    ctx_doc["responsibilityItems"] = [old_item]

    event = WeComTemplateCardEvent(
        event_key="wecom-card:prep1",
        message_id="prep1",
        sender_userid="wangwu",
        chat_id="chat",
        task_id=task_id,
        button_key="confirm",
    )
    confirm_reply = service.handle_template_card_event(event)
    assert confirm_reply.reply_type == "template_card"
    title = confirm_reply.template_card.get("main_title", {}).get("title", "")
    assert "失效" in title
    # Operation should still be uncommitted
    op = store.feedback.docs[0]
    assert op["isCommitted"] is False


def test_confirm_claim_uses_fresh_pending():
    """After claim, use the returned document (with applyToken) not the old snapshot."""
    store, _, context = _setup()
    service = WeComFeedbackService(store)
    reply = service.handle_text(_message("msg:claim1", f"{context['code']} 1 判断正确"))
    task_id = reply.template_card["task_id"]

    # Get the pending before claim
    pending_before = store.wecom_pending_feedback.docs[0]
    assert pending_before.get("applyToken") is None

    # Monkey-patch claim to verify it returns a doc with applyToken
    original_claim = service.pending.claim
    def _claim_and_verify(code, userid):
        status, doc = original_claim(code, userid)
        if doc is not None:
            assert doc.get("applyToken") is not None, "Claim should return doc with applyToken"
        return status, doc
    service.pending.claim = _claim_and_verify

    event = WeComTemplateCardEvent(
        event_key="wecom-card:claim1",
        message_id="claim1",
        sender_userid="wangwu",
        chat_id="chat",
        task_id=task_id,
        button_key="confirm",
    )
    confirm_reply = service.handle_template_card_event(event)
    assert confirm_reply.reply_type == "template_card"

    # Verify the pending was updated (applyToken cleared by mark_applied, check status)
    pending_after = store.wecom_pending_feedback.docs[0]
    assert pending_after["status"] in ("completed", "applied")
    service.pending.claim = original_claim



def test_applied_uncommitted_operation_activates_without_stale_recheck():
    """Applied pending should activate operation without stale re-check."""
    import copy
    store, notice, context = _setup()
    service = WeComFeedbackService(store)
    reply = service.handle_text(_message("msg:applied1", f"{context['code']} 1 \u5224\u65ad\u6b63\u786e"))
    task_id = reply.template_card["task_id"]

    # First claim
    pending = store.wecom_pending_feedback.docs[0]
    status, claimed = service.pending.claim(pending["confirmationCode"], "wangwu")
    assert status == "claimed"
    pending = claimed

    # Create prepared operation
    operation_id = pending["operationId"]
    operation_doc = {
        "_id": f"operation:{operation_id}",
        "recordType": "operation",
        "operationId": operation_id,
        "isCommitted": False,
        "action": "confirm_owner",
        "failureId": context["responsibilityItems"][0]["failureId"],
    }
    store.feedback.docs.append(operation_doc)

    # Manually mark applied using the applyToken
    mark_ok = service.pending.mark_applied(pending, pending["applyToken"])
    assert mark_ok
    pending_after_mark = store.wecom_pending_feedback.docs[0]
    assert pending_after_mark["status"] == "applied"
    assert pending_after_mark.get("applyToken") is None

    # Change responsibility item (should not block recovery)
    ctx_doc = store.feedback_contexts.docs[0]
    old_item = copy.deepcopy(ctx_doc["responsibilityItems"][0])
    old_item["failureId"] = "post-applied-change"
    ctx_doc["responsibilityItems"] = [old_item]

    # Monkey-patch _safe_mark_stale to raise if called
    original_safe_mark = service._safe_mark_stale
    def _raise_on_stale(*args, **kwargs):
        raise AssertionError("applied recovery must not call _safe_mark_stale")
    service._safe_mark_stale = _raise_on_stale

    event = WeComTemplateCardEvent(
        event_key="wecom-card:applied1",
        message_id="applied1",
        sender_userid="wangwu",
        chat_id="chat",
        task_id=task_id,
        button_key="confirm",
    )
    confirm_reply = service.handle_template_card_event(event)
    assert confirm_reply.reply_type == "template_card"
    title = confirm_reply.template_card.get("main_title", {}).get("title", "")
    assert "\u63d0\u4ea4" in title
    # Operation should now be committed
    op = store.feedback.docs[0]
    assert op["isCommitted"] is True
    service._safe_mark_stale = original_safe_mark


def test_applied_missing_operation_stays_applied():
    """Applied pending without operation should stay applied."""
    store, _, context = _setup()
    service = WeComFeedbackService(store)
    reply = service.handle_text(_message("msg:missingop1", f"{context['code']} 1 \u5224\u65ad\u6b63\u786e"))
    task_id = reply.template_card["task_id"]

    # Claim and mark applied without creating operation
    pending = store.wecom_pending_feedback.docs[0]
    status, claimed = service.pending.claim(pending["confirmationCode"], "wangwu")
    assert status == "claimed"
    mark_ok = service.pending.mark_applied(claimed, claimed["applyToken"])
    assert mark_ok

    event = WeComTemplateCardEvent(
        event_key="wecom-card:missingop1",
        message_id="missingop1",
        sender_userid="wangwu",
        chat_id="chat",
        task_id=task_id,
        button_key="confirm",
    )
    confirm_reply = service.handle_template_card_event(event)
    assert confirm_reply.reply_type == "template_card"
    title = confirm_reply.template_card.get("main_title", {}).get("title", "")
    # Should show "\u7ed3\u679c\u6b63\u5728\u540c\u6b65" not "\u5931\u8d25"
    assert "\u540c\u6b65" in title
    # Pending should still be applied
    pending_after = store.wecom_pending_feedback.docs[0]
    assert pending_after["status"] == "applied"


def test_card_confirmation_uses_current_signature_when_failure_id_is_same():
    """Same failureId but different signature should use new signature for operation."""
    import copy
    store, notice, context = _setup()
    service = WeComFeedbackService(store)
    reply = service.handle_text(_message("msg:sig1", f"{context['code']} 1 \u5224\u65ad\u6b63\u786e"))
    task_id = reply.template_card["task_id"]

    # Update the context's responsibility item with new signature but same failureId
    ctx_doc = store.feedback_contexts.docs[0]
    old_item = copy.deepcopy(ctx_doc["responsibilityItems"][0])
    old_item["failureSignature"] = "new-signature-v2"
    ctx_doc["responsibilityItems"] = [old_item]
    ctx_doc["updatedAt"] = dt.datetime.now(dt.timezone.utc)

    # Also update the notice snapshot for FeedbackStore validation
    notice_doc = store.notices.find_one({"repo": notice.repo, "job": notice.job, "branch": notice.branch, "buildNumber": notice.buildNumber})
    if notice_doc:
        notice_items = notice_doc.get("notice", {}).get("responsibilityItems", [])
        for ni in notice_items:
            if ni.get("failureId") == context["responsibilityItems"][0]["failureId"]:
                ni["failureSignature"] = "new-signature-v2"

    event = WeComTemplateCardEvent(
        event_key="wecom-card:sig1",
        message_id="sig1",
        sender_userid="wangwu",
        chat_id="chat",
        task_id=task_id,
        button_key="confirm",
    )
    confirm_reply = service.handle_template_card_event(event)
    assert confirm_reply.reply_type == "template_card"
    title = confirm_reply.template_card.get("main_title", {}).get("title", "")
    assert "\u63d0\u4ea4" in title or "\u6210\u529f" in title
    # Operation should use new signature
    assert len(store.feedback.docs) >= 1
    op = store.feedback.docs[0]
    assert op["failureSignature"] == "new-signature-v2"
    assert op["isCommitted"] is True
