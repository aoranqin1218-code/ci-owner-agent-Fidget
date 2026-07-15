from __future__ import annotations

import json
import pytest
from unittest.mock import MagicMock

from ci_owner_agent.services.wecom_bot_models import WeComInboundMessage, WeComMentionedUser, _unknown_decision, _unknown_decision
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



def _decision_data(
    *,
    intent_type,
    action=None,
    feedback_code=None,
    item_index=None,
    target_display_name=None,
    note=None,
    error=None,
):
    return {
        'intent_type': intent_type,
        'action': action,
        'feedback_code': feedback_code,
        'item_index': item_index,
        'target_display_name': target_display_name,
        'note': note,
        'error': error,
    }


def _make_decision(**kw):
    return WeComFeedbackAiDecision.model_validate(_decision_data(**kw))




def _decision_data(
    *,
    intent_type,
    action=None,
    feedback_code=None,
    item_index=None,
    target_display_name=None,
    note=None,
    error=None,
):
    return {
        "intent_type": intent_type,
        "action": action,
        "feedback_code": feedback_code,
        "item_index": item_index,
        "target_display_name": target_display_name,
        "note": note,
        "error": error,
    }


def _make_decision(**kw):
    return WeComFeedbackAiDecision.model_validate(_decision_data(**kw))


def test_fake_parser_returns_unknown():
    parser = FakeWeComFeedbackAiParser()
    intent = parser.parse(_message("CI-7K3M9Q 第一条判断没问题"))
    assert intent.intent_type == "unknown"
    assert "自然语言解析暂不可用" in (intent.error or "")


# ---- _validate_decision ----

def test_validate_valid_json():
    raw = json.dumps(_decision_data(
        intent_type="create_feedback",
        action="confirm_owner",
        feedback_code="CI-7K3M9Q",
        item_index=1,
    ))
    decision = _validate_decision(raw)
    assert decision.intent_type == "create_feedback"
    assert decision.action == "confirm_owner"
    assert decision.feedback_code == "CI-7K3M9Q"


def test_validate_strips_markdown_code_block():
    raw = "```json\n" + json.dumps(_decision_data(intent_type="help")) + "\n```"
    decision = _validate_decision(raw)
    assert decision.intent_type == "help"


def test_validate_invalid_json_returns_unknown():
    decision = _validate_decision("not json")
    assert decision.intent_type == "unknown"
    assert decision.error is not None


def test_validate_extra_fields_rejected():
    data = _decision_data(
        intent_type="create_feedback",
        action="confirm_owner",
        feedback_code="CI-7K3M9Q",
        item_index=1,
    )
    data["target_userid"] = "hacker"
    data["confirmation_code"] = "ABCD"
    data["operation_id"] = "evil"
    raw = json.dumps(data)
    decision = _validate_decision(raw)
    # With extra="forbid", extra fields cause validation failure
    assert decision.intent_type == "unknown"


# ---- _convert_decision ----

def test_convert_help():
    decision = _make_decision(intent_type="help")
    intent = _convert_decision(decision)
    assert intent.intent_type == "help"


def test_convert_unknown():
    decision = _make_decision(intent_type="unknown", error="测试错误")
    intent = _convert_decision(decision)
    assert intent.intent_type == "unknown"
    assert intent.error == "测试错误"


def test_convert_list_feedback():
    decision = _make_decision(intent_type="list_feedback",
        feedback_code="CI-7K3M9Q",
    )
    intent = _convert_decision(decision)
    assert intent.intent_type == "list_feedback"
    assert intent.feedback_code == "CI-7K3M9Q"


def test_convert_list_feedback_invalid_code():
    decision = _make_decision(intent_type="list_feedback",
        feedback_code="INVALID",
    )
    intent = _convert_decision(decision)
    assert intent.intent_type == "unknown"


def test_convert_confirm_owner():
    decision = _make_decision(intent_type="create_feedback",
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
    decision = _make_decision(intent_type="create_feedback",
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
    decision = _make_decision(intent_type="create_feedback",
        action="correct_owner",
        feedback_code="CI-7K3M9Q",
        item_index=1,
        target_display_name="aoran123-秦奥然",
    )
    intent = _convert_decision(decision)
    assert intent.intent_type == "unknown"


def test_convert_mark_flaky():
    decision = _make_decision(intent_type="create_feedback",
        action="mark_flaky",
        feedback_code="CI-7K3M9Q",
        item_index=2,
    )
    intent = _convert_decision(decision)
    assert intent.intent_type == "create_feedback"
    assert intent.action == "mark_flaky"
    assert intent.item_index == 2


def test_convert_mark_no_owner():
    decision = _make_decision(intent_type="create_feedback",
        action="mark_no_owner",
        feedback_code="CI-7K3M9Q",
        item_index=3,
    )
    intent = _convert_decision(decision)
    assert intent.intent_type == "create_feedback"
    assert intent.action == "mark_no_owner"


def test_convert_missing_item_index():
    """model_validator must reject create_feedback with item_index=None."""
    from pydantic import ValidationError
    with pytest.raises(ValidationError, match="create_feedback requires item_index"):
        _make_decision(intent_type="create_feedback",
            action="confirm_owner",
            feedback_code="CI-7K3M9Q",
            item_index=None,
        )


def test_convert_invalid_action():
    """model_validator must reject create_feedback with action=None."""
    from pydantic import ValidationError
    with pytest.raises(ValidationError, match="create_feedback requires action"):
        _make_decision(intent_type="create_feedback",
            action=None,
            feedback_code="CI-7K3M9Q",
            item_index=1,
        )


def test_convert_confirm_owner_with_target_rejected():
    """model_validator must reject target_display_name for non-correct_owner."""
    from pydantic import ValidationError
    with pytest.raises(ValidationError, match="target_display_name is only allowed"):
        _make_decision(intent_type="create_feedback",
            action="confirm_owner",
            feedback_code="CI-7K3M9Q",
            item_index=1,
            target_display_name="AoranQin-",
        )


def test_convert_invalid_feedback_code():
    decision = _make_decision(intent_type="create_feedback",
        action="confirm_owner",
        feedback_code="BAD",
        item_index=1,
    )
    intent = _convert_decision(decision)
    assert intent.intent_type == "unknown"


def test_convert_item_index_zero():
    """model_validator must reject item_index=0."""
    from pydantic import ValidationError
    with pytest.raises(ValidationError, match="item_index must be greater than zero"):
        _make_decision(intent_type="create_feedback",
            action="confirm_owner",
            feedback_code="CI-7K3M9Q",
            item_index=0,
        )

from pydantic import ValidationError


def test_ai_decision_accepts_integer_item_index():
    """WeComFeedbackAiDecision must accept integer item_index."""
    decision = WeComFeedbackAiDecision.model_validate(_decision_data(
        intent_type="create_feedback",
        action="confirm_owner",
        feedback_code="CI-7K3M9Q",
        item_index=1,
    ))
    assert decision.item_index == 1
    assert type(decision.item_index) is int


def test_ai_decision_rejects_string_item_index():
    """WeComFeedbackAiDecision must reject string item_index."""
    with pytest.raises(ValidationError):
        WeComFeedbackAiDecision.model_validate({
            "intent_type": "create_feedback",
            "action": "confirm_owner",
            "feedback_code": "CI-7K3M9Q",
            "item_index": "1",
        })


def test_ai_decision_rejects_float_item_index():
    """WeComFeedbackAiDecision must reject float item_index."""
    with pytest.raises(ValidationError):
        WeComFeedbackAiDecision.model_validate({
            "intent_type": "create_feedback",
            "action": "confirm_owner",
            "feedback_code": "CI-7K3M9Q",
            "item_index": 1.0,
        })


def test_ai_decision_rejects_boolean_item_index():
    """WeComFeedbackAiDecision must reject boolean item_index."""
    with pytest.raises(ValidationError):
        WeComFeedbackAiDecision.model_validate({
            "intent_type": "create_feedback",
            "action": "confirm_owner",
            "feedback_code": "CI-7K3M9Q",
            "item_index": True,
        })


def test_validate_decision_returns_unknown_for_string_item_index():
    """_validate_decision must return unknown for string item_index."""
    data = _decision_data(intent_type='create_feedback', action='confirm_owner', feedback_code='CI-7K3M9Q', item_index='1')
    decision = _validate_decision(json.dumps(data))
    assert decision.intent_type == "unknown"


def test_validate_decision_returns_unknown_for_float_item_index():
    """_validate_decision must return unknown for float item_index."""
    data = _decision_data(intent_type='create_feedback', action='confirm_owner', feedback_code='CI-7K3M9Q', item_index=1.0)
    decision = _validate_decision(json.dumps(data))
    assert decision.intent_type == "unknown"
