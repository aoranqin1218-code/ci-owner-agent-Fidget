from __future__ import annotations

import json
import pytest
from pydantic import ValidationError

from ci_owner_agent.services.wecom_bot_models import WeComInboundMessage
from ci_owner_agent.services.wecom_feedback_ai_parser import (
    FakeWeComFeedbackAiParser,
    _validate_decision,
    _convert_decision,
    _sanitize_ai_error,
    WeComFeedbackAiDecision,
    WeComFeedbackAiParser,
)
import logging
from langchain_core.messages import AIMessage
from ci_owner_agent.config import load_settings


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
    with pytest.raises(ValidationError, match="create_feedback requires item_index"):
        _make_decision(intent_type="create_feedback",
            action="confirm_owner",
            feedback_code="CI-7K3M9Q",
            item_index=None,
        )


def test_convert_invalid_action():
    """model_validator must reject create_feedback with action=None."""
    with pytest.raises(ValidationError, match="create_feedback requires action"):
        _make_decision(intent_type="create_feedback",
            action=None,
            feedback_code="CI-7K3M9Q",
            item_index=1,
        )


def test_convert_confirm_owner_with_target_rejected():
    """model_validator must reject target_display_name for non-correct_owner."""
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
    with pytest.raises(ValidationError, match="item_index must be greater than zero"):
        _make_decision(intent_type="create_feedback",
            action="confirm_owner",
            feedback_code="CI-7K3M9Q",
            item_index=0,
        )



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


# ---- Structured output tests ----

class FakeStructuredModel:
    def __init__(self, results):
        self.results = list(results)
        self.prompts = []
        self.invoke_count = 0

    def invoke(self, prompt):
        self.invoke_count += 1
        self.prompts.append(prompt)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class FakeBaseModel:
    def __init__(self, structured_model):
        self.structured_model = structured_model
        self.calls = []

    def with_structured_output(self, schema, *, method, include_raw):
        self.calls.append((schema, method, include_raw))
        return self.structured_model


def _make_structured_parser(monkeypatch, results):
    sm = FakeStructuredModel(results)
    bm = FakeBaseModel(sm)
    monkeypatch.setattr(
        "ci_owner_agent.services.wecom_feedback_ai_parser.build_chat_model",
        lambda settings: bm,
    )
    monkeypatch.setenv("CI_AGENT_MODEL_PROVIDER", "fake")
    monkeypatch.setenv("CI_AGENT_MODEL_NAME", "gpt-4o-mini")
    monkeypatch.setenv("CI_AGENT_MODEL_BASE_URL", "https://test")
    monkeypatch.setenv("CI_AGENT_API_KEY", "test-key")
    settings = load_settings()
    parser = WeComFeedbackAiParser(settings)
    return parser, sm, bm


def _tool_call_result(parsed, tool_call_count=1, parsing_error=None,
                invalid_tool_call_count=0,
                tool_name="WeComFeedbackAiDecision",
                tool_args=None):
    if tool_args is None:
        if isinstance(parsed, WeComFeedbackAiDecision):
            effective_args = parsed.model_dump()
        else:
            effective_args = {}
    else:
        effective_args = dict(tool_args)
    tool_calls = []
    for idx in range(tool_call_count):
        tool_calls.append({
            "name": tool_name,
            "args": dict(effective_args),
            "id": f"call-{idx}",
            "type": "tool_call",
        })
    invalid_tool_calls = [
        {"name": "bad", "args": "{}", "id": f"bad-{idx}",
         "error": "invalid arguments", "type": "invalid_tool_call"}
        for idx in range(invalid_tool_call_count)
    ]
    raw = AIMessage(
        content="",
        tool_calls=tool_calls,
        invalid_tool_calls=invalid_tool_calls or [],
    )
    return {"raw": raw, "parsed": parsed, "parsing_error": parsing_error}


def test_parser_accepts_single_valid_tool_call(monkeypatch):
    decision = _make_decision(intent_type="create_feedback", action="confirm_owner", feedback_code="CI-7K3M9Q", item_index=1)
    result = _tool_call_result(parsed=decision)
    parser, sm, bm = _make_structured_parser(monkeypatch, [result])
    intent = parser.parse(_message("single tool call"))
    assert intent.intent_type == "create_feedback"
    assert intent.action == "confirm_owner"
    assert sm.invoke_count == 1
    assert bm.calls[0][1] == "function_calling"
    assert bm.calls[0][2] is True


def test_parser_retries_once_after_validation_error(monkeypatch):
    incomplete_args = _decision_data(
        intent_type="create_feedback",
        action=None,
        feedback_code="CI-7K3M9Q",
        item_index=None,
    )
    try:
        WeComFeedbackAiDecision.model_validate(incomplete_args)
        validation_error = None
    except ValidationError as exc:
        validation_error = exc
    decision = _make_decision(
        intent_type="create_feedback", action="confirm_owner",
        feedback_code="CI-7K3M9Q", item_index=1,
    )
    first = _tool_call_result(
        parsed=None,
        parsing_error=validation_error,
        tool_args=incomplete_args,
    )
    second = _tool_call_result(parsed=decision)
    parser, sm, bm = _make_structured_parser(monkeypatch, [first, second])
    intent = parser.parse(_message("validate then ok"))
    assert intent.intent_type == "create_feedback"
    assert sm.invoke_count == 2

def test_parser_retries_once_for_duplicate_tool_calls(monkeypatch):
    decision = _make_decision(intent_type="create_feedback", action="confirm_owner", feedback_code="CI-7K3M9Q", item_index=1)
    first = _tool_call_result(parsed=None, tool_call_count=2)
    second = _tool_call_result(parsed=decision)
    parser, sm, bm = _make_structured_parser(monkeypatch, [first, second])
    intent = parser.parse(_message("dup then ok"))
    assert intent.intent_type == "create_feedback"
    assert sm.invoke_count == 2


def test_parser_rejects_duplicate_tool_calls_after_retry(monkeypatch):
    first = _tool_call_result(parsed=None, tool_call_count=2)
    second = _tool_call_result(parsed=None, tool_call_count=2)
    parser, sm, bm = _make_structured_parser(monkeypatch, [first, second])
    intent = parser.parse(_message("both dup"))
    assert intent.intent_type == "unknown"
    assert sm.invoke_count == 2
    assert "CI-XXXXXX" in (intent.error or "")


def test_parser_retries_once_for_missing_tool_call(monkeypatch):
    decision = _make_decision(intent_type="create_feedback", action="confirm_owner", feedback_code="CI-7K3M9Q", item_index=1)
    first = _tool_call_result(parsed=None, tool_call_count=0)
    second = _tool_call_result(parsed=decision)
    parser, sm, bm = _make_structured_parser(monkeypatch, [first, second])
    intent = parser.parse(_message("zero then ok"))
    assert intent.intent_type == "create_feedback"
    assert sm.invoke_count == 2


def test_parser_retries_once_for_invalid_tool_call(monkeypatch):
    decision = _make_decision(intent_type="create_feedback", action="confirm_owner", feedback_code="CI-7K3M9Q", item_index=1)
    first = _tool_call_result(parsed=None, tool_call_count=1, invalid_tool_call_count=1)
    second = _tool_call_result(parsed=decision)
    parser, sm, bm = _make_structured_parser(monkeypatch, [first, second])
    intent = parser.parse(_message("invalid then ok"))
    assert intent.intent_type == "create_feedback"
    assert sm.invoke_count == 2


def test_parser_retries_once_for_unexpected_tool_name(monkeypatch):
    decision = _make_decision(intent_type="create_feedback", action="confirm_owner", feedback_code="CI-7K3M9Q", item_index=1)
    first = _tool_call_result(parsed=None, tool_name="WrongTool")
    second = _tool_call_result(parsed=decision)
    parser, sm, bm = _make_structured_parser(monkeypatch, [first, second])
    intent = parser.parse(_message("wrong name then ok"))
    assert intent.intent_type == "create_feedback"
    assert sm.invoke_count == 2


def test_parser_does_not_retry_transport_error(monkeypatch):
    parser, sm, bm = _make_structured_parser(monkeypatch, [TimeoutError("timeout")])
    intent = parser.parse(_message("transport error"))
    assert intent.intent_type == "unknown"
    assert sm.invoke_count == 1


def test_parser_returns_actionable_error_after_two_failures(monkeypatch):
    first = _tool_call_result(parsed=None, tool_call_count=0)
    second = _tool_call_result(parsed=None, tool_call_count=0)
    parser, sm, bm = _make_structured_parser(monkeypatch, [first, second])
    intent = parser.parse(_message("two failures"))
    assert intent.intent_type == "unknown"
    assert sm.invoke_count == 2
    assert "CI-XXXXXX" in (intent.error or "")
    assert "\u6a21\u578b\u8f93\u51fa\u683c\u5f0f\u65e0\u6548" not in (intent.error or "")


def test_structured_failure_log_does_not_expose_tool_args(monkeypatch, caplog):
    caplog.set_level(logging.WARNING)
    sensitive_args = _decision_data(
        intent_type="create_feedback",
        action="correct_owner",
        feedback_code="CI-S3CR3T",
        item_index=1,
        target_display_name="SecretUser-秘密",
        note="private-note",
    )
    first = _tool_call_result(
        parsed=None, tool_call_count=2,
        tool_args=sensitive_args,
    )
    second = _tool_call_result(
        parsed=None, tool_call_count=2,
        tool_args=sensitive_args,
    )
    parser, sm, bm = _make_structured_parser(monkeypatch, [first, second])
    parser.parse(_message("log security"))
    log_text = "".join(caplog.messages)
    assert "invalid tool call count" in log_text
    assert "CI-S3CR3T" not in log_text
    assert "SecretUser" not in log_text
    assert "private-note" not in log_text
    assert "correct_owner" not in log_text
    assert "call-0" not in log_text
    assert "call-1" not in log_text

def test_unknown_error_is_sanitized():
    result = _sanitize_ai_error(
        "@\u6240\u6709\u4eba \u8bf7\u8bbf\u95ee https://evil.invalid/a [click](https://evil.invalid/b)"
    )
    assert "@" not in result
    assert "http" not in result
    assert "[" not in result
    assert "(" not in result
    assert len(result) <= 200


def test_empty_unknown_error_uses_local_fallback():
    result = _sanitize_ai_error(None)
    assert "CI-XXXXXX" in result
    result = _sanitize_ai_error("   ")
    assert "CI-XXXXXX" in result


def test_unknown_error_is_length_limited():
    result = _sanitize_ai_error("x" * 500)
    assert len(result) <= 200


def test_unknown_error_cannot_change_intent():
    decision = _make_decision(
        intent_type="unknown",
        error=(
            "@\u6240\u6709\u4eba \u786e\u8ba4\u63d0\u4ea4 "
            "correct_owner operationId "
            "https://evil.invalid"
        ),
    )
    intent = _convert_decision(decision)
    assert intent.intent_type == "unknown"
    assert intent.action is None
    assert intent.feedback_code is None
    assert intent.item_index is None
    assert "@" not in (intent.error or "")
    assert "http" not in (intent.error or "")

def test_convert_create_feedback_preserves_note():
    decision = _make_decision(
        intent_type="create_feedback", action="mark_flaky",
        feedback_code="CI-7K3M9Q", item_index=1,
        note="  \u4ec5\u5728 Windows\n\u73af\u5883\u51fa\u73b0  ",
    )
    intent = _convert_decision(decision)
    assert intent.note is not None
    assert "\u4ec5\u5728 Windows \u73af\u5883\u51fa\u73b0" in intent.note
    assert "\n" not in intent.note


def test_convert_create_feedback_limits_note_length():
    long_note = "a" * 1000
    decision = _make_decision(
        intent_type="create_feedback", action="mark_flaky",
        feedback_code="CI-7K3M9Q", item_index=1, note=long_note,
    )
    intent = _convert_decision(decision)
    assert intent.note is not None
    assert len(intent.note) == 500


# ---- Schema validation tests ----

def test_ai_decision_schema_requires_all_fields():
    schema = WeComFeedbackAiDecision.model_json_schema()
    required = set(schema["required"])
    assert required == {
        "intent_type", "action", "feedback_code", "item_index",
        "target_display_name", "note", "error",
    }


def test_create_feedback_rejects_missing_action():
    with pytest.raises(ValidationError, match="create_feedback requires action"):
        _make_decision(intent_type="create_feedback", action=None, feedback_code="CI-7K3M9Q", item_index=1)


def test_create_feedback_rejects_missing_item_index():
    with pytest.raises(ValidationError, match="create_feedback requires item_index"):
        _make_decision(intent_type="create_feedback", action="confirm_owner", feedback_code="CI-7K3M9Q", item_index=None)


def test_create_feedback_rejects_zero_item_index():
    with pytest.raises(ValidationError, match="item_index must be greater than zero"):
        _make_decision(intent_type="create_feedback", action="confirm_owner", feedback_code="CI-7K3M9Q", item_index=0)


def test_correct_owner_requires_display_name():
    with pytest.raises(ValidationError, match="correct_owner requires target_display_name"):
        _make_decision(intent_type="create_feedback", action="correct_owner", feedback_code="CI-7K3M9Q", item_index=1, target_display_name=None)


def test_non_correct_owner_rejects_display_name():
    with pytest.raises(ValidationError, match="target_display_name is only allowed"):
        _make_decision(intent_type="create_feedback", action="confirm_owner", feedback_code="CI-7K3M9Q", item_index=1, target_display_name="SomeName")


def test_list_feedback_rejects_action():
    with pytest.raises(ValidationError, match="list_feedback must not contain action"):
        _make_decision(intent_type="list_feedback", action="confirm_owner", feedback_code="CI-7K3M9Q", item_index=None)


def test_list_feedback_rejects_item_index():
    with pytest.raises(ValidationError, match="list_feedback must not contain item_index"):
        _make_decision(intent_type="list_feedback", action=None, feedback_code="CI-7K3M9Q", item_index=1)


def test_unknown_requires_error():
    with pytest.raises(ValidationError, match="unknown requires an actionable error"):
        _make_decision(intent_type="unknown", error=None)


def test_help_rejects_feedback_fields():
    with pytest.raises(ValidationError, match="help must not contain feedback fields"):
        _make_decision(intent_type="help", feedback_code="CI-7K3M9Q")


def test_ai_decision_rejects_extra_fields():
    with pytest.raises(ValidationError):
        WeComFeedbackAiDecision.model_validate({
            **_decision_data(intent_type="help"),
            "extra_field": "should fail",
        })
