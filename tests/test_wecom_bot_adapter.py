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


class DelayedCloseFakeClient(FakeClient):
    def disconnect(self):
        self.disconnected = True
        asyncio.create_task(self._close_later())

    async def _close_later(self):
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.connected = False

    @property
    def is_connected(self):
        return self.connected


class NeverCloseFakeClient(DelayedCloseFakeClient):
    def disconnect(self):
        self.disconnected = True


def _install_sdk(monkeypatch, client_class=FakeClient):
    fake_module = types.SimpleNamespace(
        WSClient=client_class,
        WSClientOptions=FakeOptions,
        generate_req_id=lambda prefix: f"{prefix}-id",
    )
    monkeypatch.setitem(sys.modules, "aibot", fake_module)


def test_sdk_adapter_registers_text_event_and_replies(monkeypatch):
    _install_sdk(monkeypatch)

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


def test_adapter_reports_only_fatal_sdk_errors(monkeypatch):
    _install_sdk(monkeypatch)
    adapter = WeComSdkAdapter("bot-placeholder", "secret-placeholder")
    errors = []
    adapter.set_fatal_error_handler(errors.append)

    adapter._client.handlers["error"](RuntimeError("temporary network error"))
    assert errors == []
    fatal = RuntimeError("Max reconnect attempts exceeded")
    adapter._client.handlers["error"](fatal)
    assert errors == [fatal]


def test_adapter_stop_waits_until_websocket_is_closed(monkeypatch):
    _install_sdk(monkeypatch, DelayedCloseFakeClient)

    async def run():
        adapter = WeComSdkAdapter("bot-placeholder", "secret-placeholder", close_timeout_seconds=1)
        await adapter.start()
        await adapter.stop()
        assert adapter._client.is_connected is False

    asyncio.run(run())


def test_adapter_stop_times_out_instead_of_hanging_forever(monkeypatch, caplog):
    _install_sdk(monkeypatch, NeverCloseFakeClient)

    async def run():
        adapter = WeComSdkAdapter("bot-placeholder", "secret-placeholder", close_timeout_seconds=0.01)
        await adapter.start()
        await asyncio.wait_for(adapter.stop(), timeout=0.5)
        assert adapter._client.is_connected is True

    asyncio.run(run())
    assert "Timed out waiting for WeCom WebSocket to close" in caplog.text
