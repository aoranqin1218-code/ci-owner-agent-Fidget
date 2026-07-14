from __future__ import annotations

import asyncio
import datetime as dt
from types import SimpleNamespace

import pytest

from ci_owner_agent.services.wecom_bot_adapter import is_fatal_sdk_error
from ci_owner_agent.services.wecom_bot_worker import WeComBotWorker
from ci_owner_agent.services.pending_feedback_store import WeComEventStore
from tests.test_history_store import make_store


class FakeAdapter:
    def __init__(self):
        self.handler = None
        self.replies = []
        self.started = False
        self.stopped = False
        self.fatal_error_handler = None

    def set_text_handler(self, handler):
        self.handler = handler

    def set_fatal_error_handler(self, handler):
        self.fatal_error_handler = handler

    def emit_sdk_error(self, error):
        if is_fatal_sdk_error(error):
            self.fatal_error_handler(error)

    async def start(self):
        self.started = True

    async def stop(self):
        self.stopped = True

    async def reply(self, frame, text):
        self.replies.append(text)


def _frame(msgid="m1", content="帮助"):
    return {"headers": {"req_id": "r1"}, "body": {"msgid": msgid, "chatid": "c", "from": {"userid": "u"}, "text": {"content": content}}}


def _event_message(event_key="event:m1"):
    return SimpleNamespace(
        event_key=event_key,
        message_id="m1",
        request_id="r1",
        chat_id="c",
        sender_userid="u",
    )


def test_failed_event_can_be_reclaimed():
    store = make_store()
    events = WeComEventStore(store)
    message = _event_message()
    _, first = events.claim(message)
    first = dict(first)
    events.mark_failed(message.event_key, first["claimToken"], "temporary failure")

    status, event = events.claim(message)

    assert status == "claimed"
    assert event["attemptCount"] == 2
    assert event["status"] == "processing"


def test_expired_processing_event_can_be_reclaimed():
    store = make_store()
    events = WeComEventStore(store)
    message = _event_message()
    assert events.claim(message)[0] == "claimed"
    store.wecom_bot_events.docs[0]["leaseUntil"] = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1)

    status, event = events.claim(message)

    assert status == "claimed"
    assert event["attemptCount"] == 2


def test_unexpired_processing_event_is_not_processed_twice():
    store = make_store()
    events = WeComEventStore(store)
    message = _event_message()
    assert events.claim(message)[0] == "claimed"

    status, event = events.claim(message)

    assert status == "processing"
    assert event["attemptCount"] == 1


def test_old_claim_token_cannot_complete_or_fail_reclaimed_event():
    store = make_store()
    events = WeComEventStore(store)
    message = _event_message()
    _, first = events.claim(message)
    first = dict(first)
    store.wecom_bot_events.docs[0]["leaseUntil"] = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1)
    _, second = events.claim(message)

    assert first["claimToken"] != second["claimToken"]
    assert events.mark_completed(message.event_key, first["claimToken"], "old") is False
    assert events.mark_failed(message.event_key, first["claimToken"], "old") is False
    assert store.wecom_bot_events.docs[0]["claimToken"] == second["claimToken"]
    assert events.mark_completed(message.event_key, second["claimToken"], "new") is True
    assert store.wecom_bot_events.docs[0]["replyText"] == "new"


def test_completed_duplicate_event_replays_saved_reply():
    async def run():
        adapter = FakeAdapter()
        worker = WeComBotWorker(adapter, make_store())
        await worker.handle_frame(_frame())
        await worker.handle_frame(_frame())
        assert len(adapter.replies) == 2
        assert "群内反馈命令" in adapter.replies[0]
        assert adapter.replies[1] == adapter.replies[0]
    asyncio.run(run())


def test_bad_message_does_not_escape_and_still_replies():
    async def run():
        adapter = FakeAdapter()
        worker = WeComBotWorker(adapter, make_store())
        await worker.handle_frame({"body": {"text": {"content": "帮助"}}})
        assert len(adapter.replies) == 1
        assert "消息处理失败" in adapter.replies[0]
    asyncio.run(run())


def test_worker_marks_event_failed_when_business_processing_fails():
    async def run():
        adapter = FakeAdapter()
        store = make_store()
        worker = WeComBotWorker(adapter, store)

        def fail(_message):
            raise RuntimeError("temporary failure")

        worker.service.handle = fail
        await worker.handle_frame(_frame())

        assert store.wecom_bot_events.docs[0]["status"] == "failed"
        assert "消息处理失败" in adapter.replies[0]

    asyncio.run(run())


def test_retry_after_failure_does_not_duplicate_feedback():
    async def run():
        adapter = FakeAdapter()
        store = make_store()
        worker = WeComBotWorker(adapter, store)
        calls = 0

        def fail_once_then_succeed(_message):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("temporary failure")
            return "反馈已提交。"

        worker.service.handle = fail_once_then_succeed
        frame = _frame(content="确认 ABCD")
        await worker.handle_frame(frame)
        await worker.handle_frame(frame)
        await worker.handle_frame(frame)

        assert calls == 2
        assert adapter.replies[-1] == "反馈已提交。"
        assert store.wecom_bot_events.docs[0]["status"] == "completed"

    asyncio.run(run())


def test_run_stops_adapter_cleanly():
    async def run():
        adapter = FakeAdapter()
        worker = WeComBotWorker(adapter, make_store())
        task = asyncio.create_task(worker._run())
        await asyncio.sleep(0)
        worker.stop()
        await task
        assert adapter.started and adapter.stopped
    asyncio.run(run())


def _assert_fatal_error_stops_worker(error):
    async def run():
        adapter = FakeAdapter()
        worker = WeComBotWorker(adapter, make_store())
        task = asyncio.create_task(worker._run())
        await asyncio.sleep(0)
        adapter.emit_sdk_error(error)
        with pytest.raises(RuntimeError, match="fatal SDK error"):
            await asyncio.wait_for(task, timeout=1)
        assert adapter.stopped is True

    asyncio.run(run())


def test_authentication_failure_stops_worker_and_propagates_error():
    _assert_fatal_error_stops_worker(RuntimeError("Authentication failed: invalid credential (code: 40001)"))


def test_reconnect_exhaustion_stops_worker_and_propagates_error():
    _assert_fatal_error_stops_worker(RuntimeError("Max reconnect attempts exceeded"))


def test_recoverable_sdk_error_does_not_stop_worker():
    async def run():
        adapter = FakeAdapter()
        worker = WeComBotWorker(adapter, make_store())
        task = asyncio.create_task(worker._run())
        await asyncio.sleep(0)
        adapter.emit_sdk_error(RuntimeError("temporary network error"))
        await asyncio.sleep(0)
        assert task.done() is False
        worker.stop()
        await task
        assert adapter.stopped is True

    asyncio.run(run())


def test_fatal_error_before_run_still_stops_and_propagates():
    async def run():
        adapter = FakeAdapter()
        worker = WeComBotWorker(adapter, make_store())
        adapter.emit_sdk_error(RuntimeError("Authentication failed"))
        with pytest.raises(RuntimeError, match="authentication failed"):
            await worker._run()
        assert adapter.started is False
        assert adapter.stopped is True

    asyncio.run(run())
