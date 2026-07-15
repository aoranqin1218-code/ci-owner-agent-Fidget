from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from collections.abc import Mapping

from ci_owner_agent.services.wecom_bot_adapter import WeComBotAdapterProtocol
from ci_owner_agent.services.wecom_bot_worker import WeComBotWorker
from tests.test_history_store import make_store


class _MockAdapter:
    """Mock adapter that records interactions."""

    def __init__(self):
        self.text_handler = None
        self.card_handler = None
        self.fatal_handler = None
        self.text_replies = []
        self.card_replies = []
        self.card_updates = []
        self.started = False
        self.stopped = False

    def set_text_handler(self, handler):
        self.text_handler = handler

    def set_template_card_event_handler(self, handler):
        self.card_handler = handler

    def set_fatal_error_handler(self, handler):
        self.fatal_handler = handler

    async def start(self):
        self.started = True

    async def stop(self):
        self.stopped = True

    async def reply_text(self, frame, text):
        self.text_replies.append(text)

    async def reply_template_card(self, frame, template_card):
        self.card_replies.append(template_card)

    async def update_template_card(self, frame, template_card, userids=None):
        self.card_updates.append((template_card, userids))


def test_worker_initializes_handlers():
    adapter = _MockAdapter()
    store = make_store()
    worker = WeComBotWorker(adapter, store)
    assert adapter.text_handler is not None
    assert adapter.card_handler is not None
    assert adapter.fatal_handler is not None


def test_worker_creates_service_with_ai_parser():
    adapter = _MockAdapter()
    store = make_store()
    parser = MagicMock()
    parser.parse = MagicMock(return_value=MagicMock(intent_type="unknown"))
    worker = WeComBotWorker(adapter, store, ai_parser=parser)
    assert worker.service.ai_parser is parser



def test_completed_card_event_replays_with_userids():
    """Completed card event replay should preserve userids via Worker handler."""
    import asyncio
    from ci_owner_agent.services.wecom_bot_models import WeComInboundMessage
    from ci_owner_agent.services.feedback_context_store import FeedbackContextStore
    from ci_owner_agent.schemas import CiResponsibilityNotice
    from tests.test_notification_formatter import item, notice_payload

    adapter = _MockAdapter()
    store = make_store()
    worker = WeComBotWorker(adapter, store)

    # Count service calls
    original_service = worker.service.handle_template_card_event
    service_calls = 0
    def counted(event):
        nonlocal service_calls
        service_calls += 1
        return original_service(event)
    worker.service.handle_template_card_event = counted

    # Create a notice and context
    notice = CiResponsibilityNotice.model_validate(notice_payload([item("\u5f20\u4e09")]))
    key = {"repo": notice.repo, "job": notice.job, "branch": notice.branch, "buildNumber": notice.buildNumber}
    store.notices.update_one(key, {"$set": {**key, "notice": notice.model_dump(mode="json")}}, upsert=True)
    context = FeedbackContextStore(store).get_or_create_for_notice(notice)

    # Create a pending via service
    msg = WeComInboundMessage(
        event_key="msg:card_replay",
        chat_id="chat",
        sender_userid="wangwu",
        sender_name="\u738b\u4e94",
        content=context["code"] + " 1 \u5224\u65ad\u6b63\u786e",
        mentioned_userids=[],
        mentioned_users=[],
    )
    reply = worker.service.handle_text(msg)
    assert reply.reply_type == "template_card"
    task_id = reply.template_card["task_id"]

    # Build a template card event frame for non-originator click
    msg_id = "forbidden_replay"
    frame = {
        "body": {
            "event": {
                "eventtype": "template_card_event",
                "task_id": task_id,
                "event_key": "confirm",
            },
            "from": {"userid": "lisi"},
            "msgid": msg_id,
            "chatid": "chat",
        },
        "headers": {"req_id": "req_" + msg_id},
    }

    # First call: non-originator click -> forbidden card with userids
    asyncio.run(worker.handle_template_card_event_frame(frame))
    assert len(adapter.card_updates) == 1
    card, userids = adapter.card_updates[0]
    assert userids == ["lisi"]
    assert service_calls == 1

    # Second call: same event -> completed replay preserves userids
    adapter.card_updates.clear()
    asyncio.run(worker.handle_template_card_event_frame(frame))
    assert len(adapter.card_updates) == 1
    card2, userids2 = adapter.card_updates[0]
    assert userids2 == ["lisi"]
    # Service must NOT be called again on completed replay
    assert service_calls == 1

    # Pending should still be pending (non-originator cannot confirm)
    pending = store.wecom_pending_feedback.find_one({"cardTaskId": task_id})
    assert pending is not None
    assert pending["status"] == "pending"




def test_completed_global_card_event_replays_userids_none():
    """Completed card event for global (authorized) update should replay with userids=None."""
    import asyncio
    from ci_owner_agent.services.wecom_bot_models import WeComInboundMessage
    from ci_owner_agent.services.feedback_context_store import FeedbackContextStore
    from ci_owner_agent.schemas import CiResponsibilityNotice
    from tests.test_notification_formatter import item, notice_payload

    adapter = _MockAdapter()
    store = make_store()
    worker = WeComBotWorker(adapter, store)

    # Count service calls
    original_service = worker.service.handle_template_card_event
    service_calls = 0
    def counted(event):
        nonlocal service_calls
        service_calls += 1
        return original_service(event)
    worker.service.handle_template_card_event = counted

    # Create a notice and context
    notice = CiResponsibilityNotice.model_validate(notice_payload([item("\u5f20\u4e09")]))
    key = {"repo": notice.repo, "job": notice.job, "branch": notice.branch, "buildNumber": notice.buildNumber}
    store.notices.update_one(key, {"$set": {**key, "notice": notice.model_dump(mode="json")}}, upsert=True)
    context = FeedbackContextStore(store).get_or_create_for_notice(notice)

    # Create a pending
    msg = WeComInboundMessage(
        event_key="msg:global_replay",
        chat_id="chat",
        sender_userid="zhangsan",
        sender_name="\u5f20\u4e09",
        content=context["code"] + " 1 \u5224\u65ad\u6b63\u786e",
        mentioned_userids=[],
        mentioned_users=[],
    )
    reply = worker.service.handle_text(msg)
    assert reply.reply_type == "template_card"
    task_id = reply.template_card["task_id"]

    # Authorized click - global update (userids=None)
    msg_id = "global_replay"
    frame = {
        "body": {
            "event": {
                "eventtype": "template_card_event",
                "task_id": task_id,
                "event_key": "confirm",
            },
            "from": {"userid": "zhangsan"},
            "msgid": msg_id,
            "chatid": "chat",
        },
        "headers": {"req_id": "req_" + msg_id},
    }

    # First call
    asyncio.run(worker.handle_template_card_event_frame(frame))
    assert len(adapter.card_updates) == 1
    card, userids = adapter.card_updates[0]
    assert userids is None
    assert service_calls == 1

    # Second call (completed replay)
    adapter.card_updates.clear()
    asyncio.run(worker.handle_template_card_event_frame(frame))
    assert len(adapter.card_updates) == 1
    card2, userids2 = adapter.card_updates[0]
    assert userids2 is None
    assert service_calls == 1

def test_worker_update_template_card_handles_userids():
    """Worker adapter update_template_card should preserve userids param."""
    import asyncio
    from collections.abc import Mapping

    adapter = _MockAdapter()
    store = make_store()
    worker = WeComBotWorker(adapter, store)

    frame: Mapping[str, object] = {"test": True}
    card = {"card_type": "button_interaction", "main_title": {"title": "Test"}}

    # Without userids
    asyncio.run(worker.adapter.update_template_card(frame, card, None))
    assert len(adapter.card_updates) == 1
    assert adapter.card_updates[0] == (card, None)

    # With userids
    adapter.card_updates.clear()
    asyncio.run(worker.adapter.update_template_card(frame, card, ["lisi"]))
    assert len(adapter.card_updates) == 1
    assert adapter.card_updates[0] == (card, ["lisi"])


class FailIfCalledAiParser:
    """AI parser that must never be called during fixed command processing."""
    def parse(self, message):
        raise AssertionError(
            "fixed command replay must not call LLM"
        )


def test_completed_text_event_replays_template_card():
    """Same text event replayed must reply same template card without calling Service again."""
    import asyncio
    from ci_owner_agent.services.wecom_bot_models import WeComInboundMessage
    from ci_owner_agent.services.feedback_context_store import FeedbackContextStore
    from ci_owner_agent.schemas import CiResponsibilityNotice
    from tests.test_notification_formatter import item, notice_payload

    adapter = _MockAdapter()
    store = make_store()
    worker = WeComBotWorker(adapter, store, ai_parser=FailIfCalledAiParser())

    # Count service.handle_text calls
    original_handle_text = worker.service.handle_text
    handle_text_calls = 0
    def counted_handle_text(message):
        nonlocal handle_text_calls
        handle_text_calls += 1
        return original_handle_text(message)
    worker.service.handle_text = counted_handle_text

    # Create a notice and context
    notice = CiResponsibilityNotice.model_validate(notice_payload([item("\u5f20\u4e09")]))
    key = {"repo": notice.repo, "job": notice.job, "branch": notice.branch, "buildNumber": notice.buildNumber}
    store.notices.update_one(key, {"$set": {**key, "notice": notice.model_dump(mode="json")}}, upsert=True)
    context = FeedbackContextStore(store).get_or_create_for_notice(notice)

    feedback_code = context["code"]

    # Build a text frame that produces a template card (fixed command)
    msg_id = "text_card_replay"
    frame = {
        "body": {
            "text": {
                "content": feedback_code + " 1 \u5224\u65ad\u6b63\u786e",
            },
            "from": {
                "userid": "wangwu",
                "name": "\u738b\u4e94",
            },
            "msgid": msg_id,
            "chatid": "chat",
        },
        "headers": {"req_id": "req_" + msg_id},
    }

    # First call: should create pending and reply with template card
    asyncio.run(worker.handle_text_frame(frame))
    assert handle_text_calls == 1
    assert len(adapter.card_replies) == 1
    assert len(adapter.text_replies) == 0
    first_card = adapter.card_replies[0]
    assert first_card["card_type"] == "button_interaction"
    task_id = first_card["task_id"]

    # Second call: same frame -> completed replay, same card, no service call
    asyncio.run(worker.handle_text_frame(frame))
    assert handle_text_calls == 1
    assert len(adapter.card_replies) == 2
    assert len(adapter.text_replies) == 0
    second_card = adapter.card_replies[1]
    assert second_card["task_id"] == task_id

    # Only one pending created
    assert len(store.wecom_pending_feedback.docs) == 1
    pending = store.wecom_pending_feedback.find_one({"cardTaskId": task_id})
    assert pending is not None
    assert pending["status"] == "pending"

    # confirmationCode must NOT appear in card text
    card_text = str(first_card)
    assert pending["confirmationCode"] not in card_text

    # No TypeError or "??????" on second call
    assert "\u6d88\u606f\u5904\u7406\u5931\u8d25" not in str(adapter.text_replies)
