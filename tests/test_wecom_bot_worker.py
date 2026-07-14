from __future__ import annotations

import asyncio

import pytest

from ci_owner_agent.services.wecom_bot_adapter import is_fatal_sdk_error
from ci_owner_agent.services.wecom_bot_worker import WeComBotWorker
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


def test_worker_replies_and_deduplicates_events():
    async def run():
        adapter = FakeAdapter()
        worker = WeComBotWorker(adapter, make_store())
        await worker.handle_frame(_frame())
        await worker.handle_frame(_frame())
        assert len(adapter.replies) == 1
        assert "群内反馈命令" in adapter.replies[0]
    asyncio.run(run())


def test_bad_message_does_not_escape_and_still_replies():
    async def run():
        adapter = FakeAdapter()
        worker = WeComBotWorker(adapter, make_store())
        await worker.handle_frame({"body": {"text": {"content": "帮助"}}})
        assert len(adapter.replies) == 1
        assert "消息处理失败" in adapter.replies[0]
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
