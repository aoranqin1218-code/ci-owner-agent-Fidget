from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


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
    ] = Field(
        description=(
            "识别结果。创建反馈使用 create_feedback；"
            "查询反馈使用 list_feedback；"
            "帮助使用 help；无法可靠识别使用 unknown。"
        )
    )

    action: Literal[
        "confirm_owner",
        "correct_owner",
        "mark_flaky",
        "mark_no_owner",
    ] | None = Field(
        ...,
        description=(
            "create_feedback 的动作。"
            "判断正确=confirm_owner；"
            "修改责任人=correct_owner；"
            "偶发问题=mark_flaky；"
            "无法定责=mark_no_owner；"
            "其他意图必须为 null。"
        ),
    )

    feedback_code: str | None = Field(
        ...,
        description=(
            "CI 反馈码，例如 CI-PQ6RQ6。"
            "create_feedback 和 list_feedback 必须提供。"
        ),
    )

    item_index: int | None = Field(
        ...,
        description=(
            "责任项序号，从 1 开始。"
            "create_feedback 必须提供 JSON integer；"
            "不得使用字符串、小数或布尔值；"
            "其他意图必须为 null。"
        ),
    )

    target_display_name: str | None = Field(
        ...,
        description=(
            "仅 correct_owner 使用。"
            "复制 @ 后的展示名，不包含 @，"
            "例如 AoranQin-秦奥然；"
            "其他动作必须为 null。"
        ),
    )

    note: str | None = Field(
        ...,
        description="用户提供的可选备注，没有时为 null。",
    )

    error: str | None = Field(
        ...,
        description=(
            "仅 unknown 使用的可操作错误提示；"
            "其他意图必须为 null。"
        ),
    )

    @model_validator(mode="after")
    def validate_intent_contract(self) -> "WeComFeedbackAiDecision":
        if self.intent_type == "create_feedback":
            if not self.feedback_code:
                raise ValueError("create_feedback requires feedback_code")

            if self.action is None:
                raise ValueError("create_feedback requires action")

            if self.item_index is None:
                raise ValueError("create_feedback requires item_index")

            if self.item_index < 1:
                raise ValueError("item_index must be greater than zero")

            if self.error is not None:
                raise ValueError("create_feedback must not contain error")

            if self.action == "correct_owner":
                if not self.target_display_name:
                    raise ValueError("correct_owner requires target_display_name")
            elif self.target_display_name is not None:
                raise ValueError("target_display_name is only allowed for correct_owner")

        elif self.intent_type == "list_feedback":
            if not self.feedback_code:
                raise ValueError("list_feedback requires feedback_code")

            if self.action is not None:
                raise ValueError("list_feedback must not contain action")

            if self.item_index is not None:
                raise ValueError("list_feedback must not contain item_index")

            if self.target_display_name is not None:
                raise ValueError("list_feedback must not contain target_display_name")

            if self.error is not None:
                raise ValueError("list_feedback must not contain error")

        elif self.intent_type == "help":
            if any(
                value is not None
                for value in (
                    self.action,
                    self.feedback_code,
                    self.item_index,
                    self.target_display_name,
                    self.note,
                    self.error,
                )
            ):
                raise ValueError("help must not contain feedback fields")

        elif self.intent_type == "unknown":
            if self.action is not None:
                raise ValueError("unknown must not contain action")

            if self.item_index is not None:
                raise ValueError("unknown must not contain item_index")

            if self.target_display_name is not None:
                raise ValueError("unknown must not contain target_display_name")

            if self.note is not None:
                raise ValueError("unknown must not contain note")

            if not str(self.error or "").strip():
                raise ValueError("unknown requires an actionable error")

        return self


class WeComBotReply(_StrictModel):
    reply_type: Literal["text", "template_card"]
    text: str | None = None
    template_card: dict[str, Any] | None = None


def _unknown_decision(
    error: str,
    *,
    feedback_code: str | None = None,
) -> WeComFeedbackAiDecision:
    return WeComFeedbackAiDecision(
        intent_type="unknown",
        action=None,
        feedback_code=feedback_code,
        item_index=None,
        target_display_name=None,
        note=None,
        error=error,
    )
