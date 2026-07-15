from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any

from ci_owner_agent.services.wecom_bot_models import WeComInboundMessage, WeComMentionedUser

_MENTION_RE = re.compile(r"<@([^>]+)>")


def normalize_wecom_text_frame(
    frame: Mapping[str, Any],
) -> WeComInboundMessage:
    """Normalize the documented SDK frame."""

    body = _mapping(frame.get("body"))
    headers = _mapping(frame.get("headers"))
    text = _mapping(body.get("text"))
    sender = _mapping(body.get("from") or body.get("sender"))
    quote = _mapping(body.get("quote"))
    quote_text = _mapping(quote.get("text"))

    content = str(
        text.get("content")
        or body.get("content")
        or ""
    ).strip()



    userid = str(
        sender.get("userid")
        or sender.get("user_id")
        or body.get("userid")
        or ""
    ).strip()

    if not userid:
        raise ValueError("sender userid is missing")

    bot_userid = _string(
        body.get("aibotid")
        or body.get("bot_id")
    )

    mentioned = _mentioned_users(text, body, content)



    request_id = _string(
        headers.get("req_id")
        or headers.get("request_id")
    )
    message_id = _string(
        body.get("msgid")
        or body.get("message_id")
        or body.get("msg_id")
    )
    chat_id = _string(
        body.get("chatid")
        or body.get("chat_id")
    )
    chat_type = _string(
        body.get("chattype")
        or body.get("chat_type")
    )

    event_key = build_event_key(
        message_id=message_id,
        request_id=request_id,
        sender_userid=userid,
        chat_id=chat_id,
        content=content,
    )

    return WeComInboundMessage(
        event_key=event_key,
        request_id=request_id,
        message_id=message_id,
        chat_id=chat_id,
        chat_type=chat_type,
        sender_userid=userid,
        sender_name=_string(
            sender.get("name")
            or sender.get("display_name")
            or body.get("sender_name")
        ),
        content=content,
        bot_userid=bot_userid,
        mentioned_userids=[
            item.userid for item in mentioned
        ],
        mentioned_users=mentioned,
        quoted_message_id=_string(
            quote.get("msgid")
            or quote.get("message_id")
        ),
        quoted_content=_string(
            quote_text.get("content")
            or quote.get("content")
        ),
    )


def build_event_key(
    *, message_id: str | None, request_id: str | None, sender_userid: str, chat_id: str | None, content: str
) -> str:
    if message_id:
        return f"msg:{message_id}"
    if request_id:
        return f"req:{request_id}"
    payload = json.dumps(
        {"chat": chat_id or "", "content": " ".join(content.split()), "sender": sender_userid},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _mentioned_users(text: Mapping[str, Any], body: Mapping[str, Any], content: str) -> list[WeComMentionedUser]:
    raw = text.get("mentioned_list") or text.get("mentioned_users") or body.get("mentioned_list") or []
    result: list[WeComMentionedUser] = []
    seen: set[str] = set()
    if isinstance(raw, list):
        for item in raw:
            data = _mapping(item)
            userid = _string(data.get("userid") or data.get("user_id")) if data else _string(item)
            if userid and userid not in seen:
                seen.add(userid)
                result.append(WeComMentionedUser(userid=userid, display_name=_string(data.get("name") or data.get("display_name"))))
    for userid in _MENTION_RE.findall(content):
        userid = userid.strip()
        if userid and userid not in seen:
            seen.add(userid)
            result.append(WeComMentionedUser(userid=userid))
    return result


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _string(value: Any) -> str | None:
    result = str(value).strip() if value is not None else ""
    return result or None

