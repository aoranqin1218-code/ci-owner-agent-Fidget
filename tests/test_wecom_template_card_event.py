from __future__ import annotations

import pytest

from ci_owner_agent.services.wecom_bot_models import WeComTemplateCardEvent
from ci_owner_agent.services.wecom_message_normalizer import normalize_wecom_template_card_event


def _frame(
    msgid="msg:123",
    req_id="req:456",
    userid="wangwu",
    task_id="ci-feedback-abc123",
    event_key="confirm",
    eventtype="template_card_event",
):
    return {
        "headers": {"req_id": req_id},
        "body": {
            "msgid": msgid,
            "chatid": "chat_xxx",
            "chattype": "group",
            "from": {"userid": userid},
            "event": {
                "eventtype": eventtype,
                "task_id": task_id,
                "event_key": event_key,
            },
        },
    }


def test_normalize_template_card_event():
    frame = _frame()
    event = normalize_wecom_template_card_event(frame)
    assert isinstance(event, WeComTemplateCardEvent)
    assert event.event_key == "wecom-card:msg:123"
    assert event.message_id == "msg:123"
    assert event.request_id == "req:456"
    assert event.sender_userid == "wangwu"
    assert event.chat_id == "chat_xxx"
    assert event.chat_type == "group"
    assert event.task_id == "ci-feedback-abc123"
    assert event.button_key == "confirm"


def test_normalize_template_card_event_cancel():
    frame = _frame(event_key="cancel")
    event = normalize_wecom_template_card_event(frame)
    assert event.button_key == "cancel"


def test_normalize_template_card_event_unknown_key():
    frame = _frame(event_key="some_other_key")
    import pytest
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        normalize_wecom_template_card_event(frame)


def test_normalize_template_card_event_missing_sender():
    frame = _frame()
    frame["body"]["from"] = {}
    with pytest.raises(ValueError, match="sender userid is missing"):
        normalize_wecom_template_card_event(frame)


def test_normalize_template_card_event_wrong_event_type():
    frame = _frame(eventtype="message.text")
    with pytest.raises(ValueError, match="unexpected event type"):
        normalize_wecom_template_card_event(frame)


def test_event_key_not_from_event_key():
    """The event_key for dedup must NOT be the button key."""
    frame = _frame(event_key="confirm", msgid="msg:unique")
    event = normalize_wecom_template_card_event(frame)
    assert event.event_key == "wecom-card:msg:unique"
    assert event.event_key != "confirm"
    assert event.button_key == "confirm"


def test_different_msgids_different_event_keys():
    e1 = normalize_wecom_template_card_event(_frame(msgid="msg:1", event_key="confirm"))
    e2 = normalize_wecom_template_card_event(_frame(msgid="msg:2", event_key="cancel"))
    assert e1.event_key != e2.event_key


def test_same_msgid_same_event_key():
    e1 = normalize_wecom_template_card_event(_frame(msgid="msg:same", event_key="confirm"))
    e2 = normalize_wecom_template_card_event(_frame(msgid="msg:same", event_key="cancel"))
    assert e1.event_key == e2.event_key