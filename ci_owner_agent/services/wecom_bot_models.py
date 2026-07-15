from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WeComMentionedUser(_StrictModel):
    userid: str
    display_name: str | None = None


class WeComInboundMessage(_StrictModel):
    event_key: str
    request_id: str | None = None
    message_id: str | None = None
    chat_id: str | None = None
    chat_type: str | None = None
    sender_userid: str
    sender_name: str | None = None
    content: str
    bot_userid: str | None = None
    mentioned_userids: list[str] = Field(default_factory=list)
    mentioned_users: list[WeComMentionedUser] = Field(default_factory=list)
    quoted_message_id: str | None = None
    quoted_content: str | None = None


class WeComTemplateCardEvent(_StrictModel):
    event_key: str
    request_id: str | None = None
    message_id: str | None = None
    sender_userid: str
    chat_id: str | None = None
    chat_type: str | None = None
    task_id: str
    button_key: Literal["confirm", "cancel", "unknown"]


class ParsedFeedbackIntent(_StrictModel):
    intent_type: Literal[
        "create_feedback", "confirm_pending", "cancel_pending", "list_feedback", "help", "unknown"
    ]
    action: Literal["confirm_owner", "correct_owner", "mark_flaky", "mark_no_owner"] | None = None
    feedback_code: str | None = None
    item_index: int | None = None
    target_userid: str | None = None
    target_display_name: str | None = None
    confirmation_code: str | None = None
    note: str | None = None
    error: str | None = None


class WeComFeedbackAiDecision(_StrictModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    intent_type: Literal[
        "create_feedback",
        "list_feedback",
        "help",
        "unknown",
    ]
    action: Literal[
        "confirm_owner",
        "correct_owner",
        "mark_flaky",
        "mark_no_owner",
    ] | None = None
    feedback_code: str | None = None
    item_index: int | None = None
    target_display_name: str | None = None
    note: str | None = None
    error: str | None = None


class WeComBotReply(_StrictModel):
    reply_type: Literal["text", "template_card"]
    text: str | None = None
    template_card: dict[str, Any] | None = None
