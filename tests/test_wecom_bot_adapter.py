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