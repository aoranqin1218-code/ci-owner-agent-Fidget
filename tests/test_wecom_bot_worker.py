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
    """Completed card event replay should preserve userids."""
    import asyncio
    from ci_owner_agent.services.wecom_bot_models import WeComTemplateCardEvent, WeComInboundMessage, WeComBotReply
    from ci_owner_agent.services.wecom_feedback_service import WeComFeedbackService
    from ci_owner_agent.services.feedback_context_store import FeedbackContextStore
    from ci_owner_agent.schemas import CiResponsibilityNotice
    from tests.test_notification_formatter import item, notice_payload
    from ci_owner_agent.services.wecom_bot_worker import normalize_wecom_template_card_event

    adapter = _MockAdapter()
    store = make_store()
    worker = WeComBotWorker(adapter, store)

    # Create a notice and context
    notice = CiResponsibilityNotice.model_validate(notice_payload([item("张三")]))
    key = {"repo": notice.repo, "job": notice.job, "branch": notice.branch, "buildNumber": notice.buildNumber}
    store.notices.update_one(key, {"$set": {**key, "notice": notice.model_dump(mode="json")}}, upsert=True)
    context = FeedbackContextStore(store).get_or_create_for_notice(notice)

    # Create a pending via service
    service = WeComFeedbackService(store)
    msg = WeComInboundMessage(
        event_key="msg:card_replay",
        chat_id="chat",
        sender_userid="wangwu",
        sender_name="王五",
        content=f"{context['code']} 1 判断正确",
        mentioned_userids=[],
        mentioned_users=[],
    )
    reply = service.handle_text(msg)
    assert reply.reply_type == "template_card"
    task_id = reply.template_card["task_id"]

    # Non-originator click - forbidden card with userids
    event = WeComTemplateCardEvent(
        event_key="wecom-card:forbidden_replay",
        message_id="forbidden_replay",
        sender_userid="lisi",
        chat_id="chat",
        task_id=task_id,
        button_key="confirm",
    )
    bot_reply = service.handle_template_card_event(event)
    assert bot_reply.reply_type == "template_card"
    card_data = dict(bot_reply.template_card or {})
    userids = card_data.pop("userids", None)
    assert userids == ["lisi"]

    # Simulate the worker's mark_completed_with_response to save payload
    # First claim the event through the event store (as worker does)
    claim_status, ev = worker.events.claim(event)
    assert claim_status == "claimed"
    claim_token = ev["claimToken"]

    completed = worker.events.mark_completed_with_response(
        event.event_key,
        claim_token,
        "update_template_card",
        {"template_card": card_data, "userids": userids},
    )
    assert completed

    # Verify stored payload has userids
    ev_doc = store.wecom_bot_events.find_one({"eventKey": "wecom-card:forbidden_replay"})
    assert ev_doc is not None
    assert ev_doc["responsePayload"]["userids"] == ["lisi"]
    assert ev_doc["responsePayload"]["template_card"] is not None


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
