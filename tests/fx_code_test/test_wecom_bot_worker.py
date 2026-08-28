from __future__ import annotations

import asyncio
import logging
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from collections.abc import Mapping

from ci_owner_agent.services.wecom_bot_adapter import WeComBotAdapterProtocol
from ci_owner_agent.services.wecom_bot_worker import WeComBotWorker
from ci_owner_agent.services.wecom_bot_models import WeComInboundMessage
from tests.fx_code_test.test_history_store import make_store


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
        self.send_markdown_calls = []
        self.send_markdown_error = None

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

    async def send_markdown(self, chat_id, markdown):
        if self.send_markdown_error:
            raise self.send_markdown_error
        self.send_markdown_calls.append((chat_id, markdown))

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


def test_deliver_one_notification_sends_and_marks_sent():
    import asyncio
    adapter = _MockAdapter(); store = make_store(); worker = WeComBotWorker(adapter, store, notification_chat_id="chat")
    worker.notification_outbox.enqueue_markdown(notification_type="ci_notice", target_chat_id="chat", markdown="hello", dedup_key="one")
    assert asyncio.run(worker.deliver_one_notification()) is True
    assert adapter.send_markdown_calls == [("chat", "hello")]
    assert store.wecom_notification_outbox.docs[0]["status"] == "sent"


def test_deliver_one_notification_requeues_failure():
    import asyncio
    adapter = _MockAdapter(); adapter.send_markdown_error = RuntimeError("offline")
    store = make_store(); worker = WeComBotWorker(adapter, store, notification_chat_id="chat")
    worker.notification_outbox.enqueue_markdown(notification_type="ci_notice", target_chat_id="chat", markdown="hello", dedup_key="one")
    assert asyncio.run(worker.deliver_one_notification()) is True
    assert store.wecom_notification_outbox.docs[0]["status"] == "pending"


def test_invalid_claimed_notification_is_marked_dead():
    import asyncio
    adapter = _MockAdapter(); store = make_store(); worker = WeComBotWorker(adapter, store, notification_chat_id="chat")
    queued = worker.notification_outbox.enqueue_markdown(notification_type="ci_notice", target_chat_id="chat", markdown="private", dedup_key="broken")
    store.wecom_notification_outbox.docs[0]["payload"] = {}
    assert asyncio.run(worker.deliver_one_notification()) is True
    assert adapter.send_markdown_calls == []
    assert store.wecom_notification_outbox.docs[0]["status"] == "dead"


def test_notification_target_mismatch_is_marked_dead(caplog):
    import asyncio
    adapter = _MockAdapter(); store = make_store(); worker = WeComBotWorker(adapter, store, notification_chat_id="configured-chat")
    worker.notification_outbox.enqueue_markdown(notification_type="ci_notice", target_chat_id="unexpected-chat", markdown="private markdown", dedup_key="wrong-chat")
    assert asyncio.run(worker.deliver_one_notification()) is True
    assert adapter.send_markdown_calls == []
    assert store.wecom_notification_outbox.docs[0]["status"] == "dead"
    assert "unexpected-chat" not in caplog.text and "configured-chat" not in caplog.text and "private markdown" not in caplog.text


def test_missing_delivery_key_is_dead_lettered_without_send(caplog, monkeypatch):
    import asyncio
    from datetime import datetime, timezone

    adapter = _MockAdapter(); store = make_store(); worker = WeComBotWorker(adapter, store, notification_chat_id="configured-chat")
    now = datetime.now(timezone.utc)
    store.wecom_notification_outbox.insert_one({"_id": "broken-no-key", "status": "pending", "nextAttemptAt": now,
        "createdAt": now, "attemptCount": 0, "targetChatId": "configured-chat", "messageType": "markdown",
        "notificationType": "ci_notice", "payload": {"content": "private markdown"}})
    monkeypatch.setattr(worker.notification_outbox, "mark_sent", lambda **kwargs: (_ for _ in ()).throw(AssertionError("must not mark sent")))
    monkeypatch.setattr(worker.notification_outbox, "mark_failed", lambda **kwargs: (_ for _ in ()).throw(AssertionError("must not mark failed")))
    assert asyncio.run(worker.deliver_one_notification()) is True
    doc = store.wecom_notification_outbox.docs[0]
    assert adapter.send_markdown_calls == [] and doc["status"] == "dead" and doc["deadAt"] is not None
    assert doc["nextAttemptAt"] is None and doc["leaseToken"] is None and doc["leaseUntil"] is None
    assert doc["lastErrorType"] == "InvalidOutboxItem" and worker.notification_outbox.claim_next() is None
    assert "configured-chat" not in caplog.text and "private markdown" not in caplog.text


def test_notification_loop_redacts_unexpected_exception(caplog, monkeypatch):
    adapter = _MockAdapter()
    worker = WeComBotWorker(adapter, make_store(), notification_chat_id="configured-chat", notification_poll_seconds=1)

    async def fail_once():
        assert worker._stop_event is not None
        worker._stop_event.set()
        raise RuntimeError("mongodb://user:password@secret-host/db")

    monkeypatch.setattr(worker, "deliver_one_notification", fail_once)
    caplog.set_level(logging.ERROR, logger="ci_owner_agent.services.wecom_bot_worker")

    async def run():
        worker._stop_event = asyncio.Event()
        await worker._notification_loop()

    asyncio.run(run())
    assert "Notification delivery loop failed: RuntimeError" in caplog.text
    assert all(value not in caplog.text for value in ("mongodb://", "password", "secret-host", "/db", "Traceback"))
    relevant = [record for record in caplog.records if "Notification delivery loop failed" in record.getMessage()]
    assert len(relevant) == 1 and relevant[0].exc_info is None


def _inbound_chat(*, chat_id, chat_type, content="private callback content", sender="secret-userid"):
    return WeComInboundMessage(event_key="message:test", chat_id=chat_id, chat_type=chat_type,
                               sender_userid=sender, content=content)


def test_chat_discovery_disabled_does_not_log_chat_id(caplog):
    worker = WeComBotWorker(_MockAdapter(), make_store(), discover_chat_id=False)
    worker._maybe_log_discovered_chat_id(_inbound_chat(chat_id="secret-group-chat", chat_type="group"))
    assert "WeCom group chat discovery" not in caplog.text
    assert "secret-group-chat" not in caplog.text and "private callback content" not in caplog.text


def test_chat_discovery_logs_first_group_chat_only(caplog):
    worker = WeComBotWorker(_MockAdapter(), make_store(), discover_chat_id=True)
    worker._maybe_log_discovered_chat_id(_inbound_chat(chat_id="first-group-chat", chat_type="group"))
    worker._maybe_log_discovered_chat_id(_inbound_chat(chat_id="first-group-chat", chat_type="group"))
    worker._maybe_log_discovered_chat_id(_inbound_chat(chat_id="second-group-chat", chat_type="group"))
    assert caplog.text.count("WeCom group chat discovery") == 1
    assert "first-group-chat" in caplog.text and "second-group-chat" not in caplog.text
    assert "private callback content" not in caplog.text and "secret-userid" not in caplog.text
    assert worker._chat_id_discovered is True


def test_chat_discovery_ignores_direct_or_invalid_chat_metadata(caplog):
    worker = WeComBotWorker(_MockAdapter(), make_store(), discover_chat_id=True)
    for chat_id, chat_type in (("direct-userid", "single"), (None, "group"), ("", "group"), ("unknown-chat", "unknown")):
        worker._maybe_log_discovered_chat_id(_inbound_chat(chat_id=chat_id, chat_type=chat_type))
    assert "WeCom group chat discovery" not in caplog.text
    assert "direct-userid" not in caplog.text and worker._chat_id_discovered is False



def test_completed_card_event_replays_with_userids():
    """Completed card event replay should preserve userids via Worker handler."""
    import asyncio
    from ci_owner_agent.services.wecom_bot_models import WeComInboundMessage
    from ci_owner_agent.services.feedback_context_store import FeedbackContextStore
    from ci_owner_agent.schemas import CiResponsibilityNotice
    from tests.fx_code_test.test_notification_formatter import item, notice_payload

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
    from tests.fx_code_test.test_notification_formatter import item, notice_payload

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
    from tests.fx_code_test.test_notification_formatter import item, notice_payload

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
    assert "horizontal_content_list" in first_card

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


def test_worker_handles_nested_confirm_template_card_event():
    """Worker must handle real WeCom nested confirm callback."""
    import asyncio
    from ci_owner_agent.services.wecom_bot_models import WeComInboundMessage
    from ci_owner_agent.services.feedback_context_store import FeedbackContextStore
    from ci_owner_agent.schemas import CiResponsibilityNotice
    from tests.fx_code_test.test_notification_formatter import item, notice_payload

    adapter = _MockAdapter()
    store = make_store()
    worker = WeComBotWorker(adapter, store)

    # Create a notice and context
    notice = CiResponsibilityNotice.model_validate(notice_payload([item("\u5f20\u4e09")]))
    key = {"repo": notice.repo, "job": notice.job, "branch": notice.branch, "buildNumber": notice.buildNumber}
    store.notices.update_one(key, {"$set": {**key, "notice": notice.model_dump(mode="json")}}, upsert=True)
    context = FeedbackContextStore(store).get_or_create_for_notice(notice)

    # Create a pending via fixed command
    msg = WeComInboundMessage(
        event_key="msg:nested_confirm_setup",
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

    # Build a nested callback frame (real WeCom shape)
    frame = {
        "headers": {"req_id": "req-nested-confirm"},
        "body": {
            "aibotid": "bot-id",
            "chatid": "chat",
            "chattype": "group",
            "create_time": 1234567890,
            "from": {"userid": "zhangsan"},
            "msgid": "msg-nested-confirm",
            "msgtype": "event",
            "response_url": "https://example.invalid/",
            "event": {
                "eventtype": "template_card_event",
                "template_card_event": {
                    "event_key": "confirm",
                    "task_id": task_id,
                },
            },
        },
    }

    asyncio.run(worker.handle_template_card_event_frame(frame))

    # Must NOT send error text
    assert len(adapter.text_replies) == 0

    # Must update card once
    assert len(adapter.card_updates) == 1
    updated_card, userids = adapter.card_updates[0]
    assert userids is None

    # Updated card should indicate success
    title = str(updated_card.get("main_title", {}).get("title", ""))
    assert "\u63d0\u4ea4" in title or "\u5df2" in title or "\u6210\u529f" in title

    # Pending should no longer be pending
    pending = store.wecom_pending_feedback.find_one({"cardTaskId": task_id})
    assert pending is not None
    assert pending["status"] != "pending"

    # Operation should exist and be committed
    ops = list(store.feedback.find({}))
    assert len(ops) == 1
    assert ops[0]["isCommitted"] is True


def test_worker_handles_nested_cancel_template_card_event():
    """Worker must handle real WeCom nested cancel callback."""
    import asyncio
    from ci_owner_agent.services.wecom_bot_models import WeComInboundMessage
    from ci_owner_agent.services.feedback_context_store import FeedbackContextStore
    from ci_owner_agent.schemas import CiResponsibilityNotice
    from tests.fx_code_test.test_notification_formatter import item, notice_payload

    adapter = _MockAdapter()
    store = make_store()
    worker = WeComBotWorker(adapter, store)

    # Create a notice and context
    notice = CiResponsibilityNotice.model_validate(notice_payload([item("\u5f20\u4e09")]))
    key = {"repo": notice.repo, "job": notice.job, "branch": notice.branch, "buildNumber": notice.buildNumber}
    store.notices.update_one(key, {"$set": {**key, "notice": notice.model_dump(mode="json")}}, upsert=True)
    context = FeedbackContextStore(store).get_or_create_for_notice(notice)

    # Create a pending via fixed command
    msg = WeComInboundMessage(
        event_key="msg:nested_cancel_setup",
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

    # Build a nested cancel callback frame
    frame = {
        "headers": {"req_id": "req-nested-cancel"},
        "body": {
            "aibotid": "bot-id",
            "chatid": "chat",
            "chattype": "group",
            "create_time": 1234567890,
            "from": {"userid": "zhangsan"},
            "msgid": "msg-nested-cancel",
            "msgtype": "event",
            "response_url": "https://example.invalid/",
            "event": {
                "eventtype": "template_card_event",
                "template_card_event": {
                    "event_key": "cancel",
                    "task_id": task_id,
                },
            },
        },
    }

    asyncio.run(worker.handle_template_card_event_frame(frame))

    # Must NOT send error text
    assert len(adapter.text_replies) == 0

    # Must update card once
    assert len(adapter.card_updates) == 1
    updated_card, userids = adapter.card_updates[0]
    assert userids is None

    # Updated card should indicate cancellation
    title = str(updated_card.get("main_title", {}).get("title", ""))
    assert "\u53d6\u6d88" in title

    # Pending should be cancelled
    pending = store.wecom_pending_feedback.find_one({"cardTaskId": task_id})
    assert pending is not None
    assert pending["status"] == "cancelled"

    # No operation should be created
    ops = list(store.feedback.find({}))
    assert len(ops) == 0


def test_completed_card_replay_preserves_text_notice():
    """Completed card event replay must preserve text_notice card type."""
    import asyncio
    from ci_owner_agent.services.wecom_bot_models import WeComInboundMessage
    from ci_owner_agent.services.feedback_context_store import FeedbackContextStore
    from ci_owner_agent.schemas import CiResponsibilityNotice
    from tests.fx_code_test.test_notification_formatter import item, notice_payload

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
        event_key="msg:replay_card_type",
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

    # Authorized confirm click
    msg_id = "replay_card_type"
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
    assert service_calls == 1
    assert len(adapter.card_updates) == 1
    card1, userids1 = adapter.card_updates[0]
    assert card1["card_type"] == "text_notice"
    assert "button_list" not in card1
    assert card1["task_id"] == task_id

    # Second call (completed replay)
    adapter.card_updates.clear()
    asyncio.run(worker.handle_template_card_event_frame(frame))
    assert service_calls == 1
    assert len(adapter.card_updates) == 1
    card2, userids2 = adapter.card_updates[0]
    assert card2["card_type"] == "text_notice"
    assert "button_list" not in card2
    assert card2["task_id"] == task_id
