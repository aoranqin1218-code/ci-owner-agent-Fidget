from __future__ import annotations

import datetime as dt
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