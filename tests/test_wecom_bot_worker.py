from __future__ import annotations

import asyncio

from ci_owner_agent.services.wecom_bot_worker import WeComBotWorker
from tests.test_history_store import make_store


class FakeAdapter:
    def __init__(self):
        self.handler = None
        self.replies = []
        self.started = False
        self.stopped = False

    def set_text_handler(self, handler):
        self.handler = handler

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

