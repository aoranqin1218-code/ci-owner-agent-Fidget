from __future__ import annotations

import asyncio
import sys
import types

from ci_owner_agent.services.wecom_bot_adapter import WeComSdkAdapter


class FakeOptions:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class FakeClient:
    def __init__(self, options):
        self.options = options
        self.handlers = {}
        self.connected = False
        self.disconnected = False
        self.replies = []

    def on(self, event, handler):
        self.handlers[event] = handler

    async def connect(self):
        self.connected = True

    def disconnect(self):
        self.disconnected = True

    async def reply_stream(self, frame, stream_id, content, finish):
        self.replies.append((frame, stream_id, content, finish))


def test_sdk_adapter_registers_text_event_and_replies(monkeypatch):
    fake_module = types.SimpleNamespace(
        WSClient=FakeClient,
        WSClientOptions=FakeOptions,
        generate_req_id=lambda prefix: f"{prefix}-id",
    )
    monkeypatch.setitem(sys.modules, "aibot", fake_module)

    async def run():
        adapter = WeComSdkAdapter("bot-placeholder", "secret-placeholder")
        received = []

        async def handler(frame):
            received.append(frame)

        adapter.set_text_handler(handler)
        frame = {"headers": {"req_id": "request-placeholder"}, "body": {"text": {"content": "帮助"}}}
        await adapter.start()
        await adapter._client.handlers["message.text"](frame)
        await adapter.reply(frame, "reply")
        await adapter.stop()

        assert received == [frame]
        assert adapter._client.connected and adapter._client.disconnected
        assert adapter._client.replies[0][1:] == ("stream-id", "reply", True)

    asyncio.run(run())
