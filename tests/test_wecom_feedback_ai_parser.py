from __future__ import annotations

import json
import pytest
from unittest.mock import MagicMock

from ci_owner_agent.services.wecom_bot_models import WeComInboundMessage, WeComMentionedUser
from ci_owner_agent.services.wecom_feedback_ai_parser import (
    FakeWeComFeedbackAiParser,
    _validate_decision,
    _convert_decision,
    WeComFeedbackAiDecision,
)


def _message(content: str) -> WeComInboundMessage:
    return WeComInboundMessage(
        event_key="msg:1",
        sender_userid="reviewer",
        sender_name="王五",
        content=content,
        bot_userid="bot",
    )


# ---- FakeWeComFeedbackAiParser ----

def test_fake_parser_returns_unknown():
    parser = FakeWeComFeedbackAiParser()
    intent = parser.parse(_message("CI-7K3M9Q 第一条判断没问题"))
    assert intent.intent_type == "unknown"
    assert "自然语言解析暂不可用" in (intent.error or "")


# ---- _validate_decision ----

def test_validate_valid_json():
    raw = json.dumps({
        "intent_type": "create_feedback",
        "action": "confirm_owner",
        "feedback_code": "CI-7K3M9Q",
        "item_index": 1,
    })
    decision = _validate_decision(raw)
    assert decision.intent_type == "create_feedback"
    assert decision.action == "confirm_owner"
    assert decision.feedback_code == "CI-7K3M9Q"


def test_validate_strips_markdown_code_block():
    raw = "```json\n" + json.dumps({"intent_type": "help"}) + "\n```"
    decision = _validate_decision(raw)
    assert decision.intent_type == "help"


def test_validate_invalid_json_returns_unknown():
    decision = _validate_decision("not json")
    assert decision.intent_type == "unknown"
    assert decision.error is not None


def test_validate_extra_fields_rejected():
    raw = json.dumps({
        "intent_type": "create_feedback",
        "action": "confirm_owner",
        "feedback_code": "CI-7K3M9Q",
        "item_index": 1,
        "target_userid": "hacker",
        "confirmation_code": "ABCD",
        "operation_id": "evil",
    })
    decision = _validate_decision(raw)
    # With extra="forbid", extra fields cause validation failure
    assert decision.intent_type == "unknown"


# ---- _convert_decision ----

def test_convert_help():
    decision = WeComFeedbackAiDecision(intent_type="help")
    intent = _convert_decision(decision)
    assert intent.intent_type == "help"


def test_convert_unknown():
    decision = WeComFeedbackAiDecision(intent_type="unknown", error="测试错误")
    intent = _convert_decision(decision)
    assert intent.intent_type == "unknown"
    assert intent.error == "测试错误"


def test_convert_list_feedback():
    decision = WeComFeedbackAiDecision(
        intent_type="list_feedback",
        feedback_code="CI-7K3M9Q",
    )
    intent = _convert_decision(decision)
    assert intent.intent_type == "list_feedback"
    assert intent.feedback_code == "CI-7K3M9Q"


def test_convert_list_feedback_invalid_code():
    decision = WeComFeedbackAiDecision(
        intent_type="list_feedback",
        feedback_code="INVALID",
    )
    intent = _convert_decision(decision)
    assert intent.intent_type == "unknown"


def test_convert_confirm_owner():
    decision = WeComFeedbackAiDecision(
        intent_type="create_feedback",
        action="confirm_owner",
        feedback_code="CI-7K3M9Q",
        item_index=1,
    )
    intent = _convert_decision(decision)
    assert intent.intent_type == "create_feedback"
    assert intent.action == "confirm_owner"
    assert intent.feedback_code == "CI-7K3M9Q"
    assert intent.item_index == 1
    assert intent.target_userid is None
    assert intent.target_display_name is None


def test_convert_correct_owner():
    decision = WeComFeedbackAiDecision(
        intent_type="create_feedback",
        action="correct_owner",
        feedback_code="CI-7K3M9Q",
        item_index=1,
        target_display_name="AoranQin-秦奥然",
    )
    intent = _convert_decision(decision)
    assert intent.intent_type == "create_feedback"
    assert intent.action == "correct_owner"
    assert intent.target_userid == "aoranqin"
    assert intent.target_display_name == "AoranQin-秦奥然"


def test_convert_correct_owner_invalid_display_name():
    decision = WeComFeedbackAiDecision(
        intent_type="create_feedback",
        action="correct_owner",
        feedback_code="CI-7K3M9Q",
        item_index=1,
        target_display_name="aoran123-秦奥然",
    )
    intent = _convert_decision(decision)
    assert intent.intent_type == "unknown"


def test_convert_mark_flaky():
    decision = WeComFeedbackAiDecision(
        intent_type="create_feedback",
        action="mark_flaky",
        feedback_code="CI-7K3M9Q",
        item_index=2,
    )
    intent = _convert_decision(decision)
    assert intent.intent_type == "create_feedback"
    assert intent.action == "mark_flaky"
    assert intent.item_index == 2


def test_convert_mark_no_owner():
    decision = WeComFeedbackAiDecision(
        intent_type="create_feedback",
        action="mark_no_owner",
        feedback_code="CI-7K3M9Q",
        item_index=3,
    )
    intent = _convert_decision(decision)
    assert intent.intent_type == "create_feedback"
    assert intent.action == "mark_no_owner"


def test_convert_missing_item_index():
    decision = WeComFeedbackAiDecision(
        intent_type="create_feedback",
        action="confirm_owner",
        feedback_code="CI-7K3M9Q",
        item_index=None,
    )
    intent = _convert_decision(decision)
    assert intent.intent_type == "unknown"


def test_convert_invalid_action():
    decision = WeComFeedbackAiDecision(
        intent_type="create_feedback",
        action=None,
        feedback_code="CI-7K3M9Q",
        item_index=1,
    )
    intent = _convert_decision(decision)
    assert intent.intent_type == "unknown"


def test_convert_confirm_owner_with_target_rejected():
    decision = WeComFeedbackAiDecision(
        intent_type="create_feedback",
        action="confirm_owner",
        feedback_code="CI-7K3M9Q",
        item_index=1,
        target_display_name="AoranQin-秦奥然",
    )
    intent = _convert_decision(decision)
    assert intent.intent_type == "unknown"


def test_convert_invalid_feedback_code():
    decision = WeComFeedbackAiDecision(
        intent_type="create_feedback",
        action="confirm_owner",
        feedback_code="BAD",
        item_index=1,
    )
    intent = _convert_decision(decision)
    assert intent.intent_type == "unknown"


def test_convert_item_index_zero():
    decision = WeComFeedbackAiDecision(
        intent_type="create_feedback",
        action="confirm_owner",
        feedback_code="CI-7K3M9Q",
        item_index=0,
    )
    intent = _convert_decision(decision)
    assert intent.intent_type == "unknown"