from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from collections.abc import Mapping

from ci_owner_agent.services.wecom_bot_adapter import (
    WeComBotAdapterProtocol,
    WeComSdkAdapter,
    is_fatal_sdk_error,
    fatal_sdk_error_reason,
)
from ci_owner_agent.services.wecom_bot_adapter import _SdkLogger


class FakeAdapter:
    """Minimal fake adapter for testing protocol compliance."""

    def __init__(self):
        self.text_handler = None
        self.card_handler = None
        self.fatal_handler = None
        self.text_replies = []
        self.card_replies = []
        self.card_updates = []

    def set_text_handler(self, handler):
        self.text_handler = handler

    def set_template_card_event_handler(self, handler):
        self.card_handler = handler

    def set_fatal_error_handler(self, handler):
        self.fatal_handler = handler

    async def start(self):
        pass

    async def stop(self):
        pass

    async def reply_text(self, frame, text):
        self.text_replies.append(text)

    async def reply_template_card(self, frame, template_card):
        self.card_replies.append(template_card)

    async def update_template_card(self, frame, template_card, userids=None):
        self.card_updates.append((template_card, userids))


def test_fake_adapter_implements_protocol():
    adapter = FakeAdapter()
    # Protocol uses structural subtyping, not isinstance


def test_adapter_protocol_has_template_card_methods():
    """Protocol should include template card methods."""
    methods = dir(WeComBotAdapterProtocol)
    assert "set_template_card_event_handler" in methods
    assert "reply_template_card" in methods
    assert "update_template_card" in methods


def test_fake_adapter_card_methods():
    adapter = FakeAdapter()
    frame = {}
    card = {"card_type": "button_interaction", "task_id": "test"}
    import asyncio
    asyncio.run(adapter.reply_template_card(frame, card))
    assert len(adapter.card_replies) == 1
    assert adapter.card_replies[0]["card_type"] == "button_interaction"

    asyncio.run(adapter.update_template_card(frame, card, userids=["user1"]))
    assert len(adapter.card_updates) == 1
    assert adapter.card_updates[0][1] == ["user1"]


def test_is_fatal_sdk_error():
    assert is_fatal_sdk_error(RuntimeError("authentication failed")) is True
    assert is_fatal_sdk_error(RuntimeError("max reconnect attempts exceeded")) is True
    assert is_fatal_sdk_error(RuntimeError("network timeout")) is False


def test_fatal_sdk_error_reason():
    assert fatal_sdk_error_reason(RuntimeError("authentication failed")) == "authentication failed"
    assert fatal_sdk_error_reason(RuntimeError("max reconnect attempts exceeded")) == "maximum reconnect attempts exceeded"
    assert fatal_sdk_error_reason(RuntimeError("unknown")) == "unrecoverable SDK error"

def test_sdk_adapter_update_template_card_uses_official_keyword_names():
    import asyncio
    from unittest.mock import AsyncMock, MagicMock
    adapter = object.__new__(WeComSdkAdapter)
    client = MagicMock()
    client.update_template_card = AsyncMock()
    adapter._client = client
    frame = {"headers": {"req_id": "req-1"}}
    card = {"card_type": "button_interaction", "task_id": "task-1"}
    asyncio.run(adapter.update_template_card(frame, card, ["lisi"]))
    client.update_template_card.assert_awaited_once_with(
        frame=frame,
        template_card=card,
        userids=["lisi"],
    )

def test_sdk_adapter_update_template_card_omits_userids_when_none():
    import asyncio
    from unittest.mock import AsyncMock, MagicMock
    adapter = object.__new__(WeComSdkAdapter)
    client = MagicMock()
    client.update_template_card = AsyncMock()
    adapter._client = client
    frame = {"headers": {"req_id": "req-2"}}
    card = {"card_type": "button_interaction", "task_id": "task-2"}
    asyncio.run(adapter.update_template_card(frame, card, None))
    client.update_template_card.assert_awaited_once_with(
        frame=frame,
        template_card=card,
    )
    call_kwargs = client.update_template_card.call_args.kwargs
    assert "card" not in call_kwargs


def test_sdk_adapter_reply_template_card_uses_two_arguments():
    """WeComSdkAdapter.reply_template_card() must only take frame and template_card."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    adapter = object.__new__(WeComSdkAdapter)
    client = MagicMock()
    client.reply_template_card = AsyncMock()
    adapter._client = client

    frame = {"headers": {"req_id": "req-card"}}
    card = {
        "card_type": "button_interaction",
        "task_id": "task-card",
    }

    asyncio.run(adapter.reply_template_card(frame, card))

    client.reply_template_card.assert_awaited_once_with(
        frame,
        card,
    )


def test_send_markdown_waits_for_authentication_and_uses_proactive_message():
    import asyncio
    adapter = object.__new__(WeComSdkAdapter)
    adapter._client = MagicMock()
    adapter._client.send_message = AsyncMock(return_value={"errcode": 0})
    adapter._authenticated_event = asyncio.Event()
    adapter._authentication_timeout_seconds = 1

    async def run():
        task = asyncio.create_task(adapter.send_markdown("chat", "hello"))
        await asyncio.sleep(0)
        adapter._client.send_message.assert_not_awaited()
        adapter._on_authenticated()
        await task

    asyncio.run(run())
    adapter._client.send_message.assert_awaited_once_with(
        "chat", {"msgtype": "markdown", "markdown": {"content": "hello"}}
    )


def test_send_markdown_rejects_nonzero_ack_without_body():
    import asyncio
    adapter = object.__new__(WeComSdkAdapter)
    adapter._client = MagicMock()
    adapter._client.send_message = AsyncMock(return_value={"errcode": 93000, "errmsg": "bad"})
    adapter._authenticated_event = asyncio.Event(); adapter._authenticated_event.set()
    adapter._authentication_timeout_seconds = 1
    with pytest.raises(RuntimeError, match="errcode=93000") as exc:
        asyncio.run(adapter.send_markdown("chat", "private markdown"))
    assert "private markdown" not in str(exc.value)


def test_sdk_logger_does_not_forward_sensitive_messages(caplog):
    message = 'Received push message: {"chatid":"secret-chat","content":"private-message","response_url":"https://secret.example"}'
    logger = _SdkLogger()
    logger.debug(message); logger.info(message); logger.warn(message); logger.error(message)
    assert "secret-chat" not in caplog.text and "private-message" not in caplog.text
    assert "response_url" not in caplog.text and "secret.example" not in caplog.text
    assert "WeCom SDK warning" in caplog.text and "WeCom SDK error" in caplog.text
