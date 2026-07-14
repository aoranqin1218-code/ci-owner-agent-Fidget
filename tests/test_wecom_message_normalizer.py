from __future__ import annotations

from ci_owner_agent.services.wecom_message_normalizer import normalize_wecom_text_frame


def test_normalizes_official_frame_and_compatible_mentions():
    frame = {
        "headers": {"req_id": "req-1"},
        "body": {
            "msgid": "msg-1",
            "aibotid": "bot-1",
            "chatid": "chat-1",
            "chattype": "group",
            "from": {"userid": "wangwu", "name": "王五"},
            "msgtype": "text",
            "text": {"content": "<@bot-1> hello <@lisi>", "mentioned_list": [{"userid": "lisi", "name": "李四"}]},
            "quote": {"msgid": "quoted-1", "msgtype": "text", "text": {"content": "old"}},
        },
    }
    result = normalize_wecom_text_frame(frame)
    assert result.event_key == "msg:msg-1"
    assert result.sender_userid == "wangwu"
    assert result.chat_id == "chat-1"
    assert result.mentioned_userids == ["lisi", "bot-1"]
    assert result.quoted_content == "old"


def test_optional_fields_missing_is_stable():
    frame = {"body": {"from": {"userid": "u1"}, "text": {"content": " hello  world "}}}
    first = normalize_wecom_text_frame(frame)
    second = normalize_wecom_text_frame(frame)
    assert first.event_key == second.event_key
    assert first.request_id is None
    assert first.chat_id is None

