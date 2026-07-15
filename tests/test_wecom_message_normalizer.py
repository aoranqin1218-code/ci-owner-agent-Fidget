from __future__ import annotations

import pytest

from ci_owner_agent.services.wecom_bot_models import WeComInboundMessage, WeComTemplateCardEvent
from ci_owner_agent.services.wecom_message_normalizer import (
    build_event_key,
    normalize_wecom_text_frame,
    normalize_wecom_template_card_event,
)


def test_text_frame_normalization():
    frame = {
        "headers": {"req_id": "req-1"},
        "body": {
            "msgid": "msg-1",
            "chatid": "chat-1",
            "chattype": "group",
            "from": {"userid": "wangwu", "name": "王五"},
            "text": {"content": "CI-7K3M9Q 1 判断正确", "mentioned_list": ["bot"]},
            "aibotid": "bot",
        },
    }
    msg = normalize_wecom_text_frame(frame)
    assert isinstance(msg, WeComInboundMessage)
    assert msg.event_key == "msg:msg-1"
    assert msg.sender_userid == "wangwu"
    assert msg.sender_name == "王五"
    assert msg.content == "CI-7K3M9Q 1 判断正确"
    assert msg.bot_userid == "bot"
    assert msg.chat_id == "chat-1"


def test_text_frame_missing_sender():
    frame = {"headers": {}, "body": {"text": {"content": "hello"}}}
    with pytest.raises(ValueError, match="sender userid is missing"):
        normalize_wecom_text_frame(frame)


def test_build_event_key_with_message_id():
    key = build_event_key(message_id="msg-1", request_id=None, sender_userid="u", chat_id="c", content="hello")
    assert key == "msg:msg-1"


def test_build_event_key_with_request_id():
    key = build_event_key(message_id=None, request_id="req-1", sender_userid="u", chat_id="c", content="hello")
    assert key == "req:req-1"


def test_build_event_key_fallback():
    key = build_event_key(message_id=None, request_id=None, sender_userid="wangwu", chat_id="chat-1", content="hello world")
    assert key.startswith("sha256:")


# ---- template card event ----

def _card_frame(msgid="msg:card1", userid="wangwu", task_id="ci-feedback-abc", event_key="confirm"):
    return {
        "headers": {"req_id": "req:card1"},
        "body": {
            "msgid": msgid,
            "chatid": "chat_1",
            "chattype": "group",
            "from": {"userid": userid},
            "event": {
                "eventtype": "template_card_event",
                "task_id": task_id,
                "event_key": event_key,
            },
        },
    }


def test_template_card_event_normalization():
    event = normalize_wecom_template_card_event(_card_frame())
    assert isinstance(event, WeComTemplateCardEvent)
    assert event.event_key == "wecom-card:msg:card1"
    assert event.sender_userid == "wangwu"
    assert event.task_id == "ci-feedback-abc"
    assert event.button_key == "confirm"


def test_template_card_event_cancel():
    event = normalize_wecom_template_card_event(_card_frame(event_key="cancel"))
    assert event.button_key == "cancel"