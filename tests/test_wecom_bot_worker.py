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