from __future__ import annotations

import json
from dataclasses import replace

from ci_owner_agent.config import load_settings
from ci_owner_agent.schemas import FailureFact
from ci_owner_agent.services.failure_fact_compare_ai import compare_failure_facts_with_ai


def make_settings(**overrides):
    base = replace(
        load_settings(),
        ai_history_compare_enabled=True,
        model_provider="doubao",
        model_name="test-model",
        model_base_url="https://example.test/v1",
        api_key="test-key",
        ai_failure_fact_min_confidence=0.7,
        ai_history_compare_threshold=0.9,
    )
    return replace(base, **overrides)


def make_fact(**overrides) -> FailureFact:
    data = {
        "signatureKey": "typescript_compile_error|TS2305|src/index.ts|classifyErrorMessage",
        "historyEligible": True,
        "isGenericWrapper": False,
        "failureKind": "typescript_compile_error",
        "errorCode": "TS2305",
        "filePath": "src/index.ts",
        "symbol": "classifyErrorMessage",
        "message": "Module './errors' has no exported member 'classifyErrorMessage'",
        "rootCauseSummary": "missing export",
        "confidence": 0.95,
    }
    data.update(overrides)
    return FailureFact.model_validate(data)


def _compare(monkeypatch, payload, *, settings=None, current_fact=None, historical_fact=None):
    class FakeModel:
        def invoke(self, prompt):
            return json.dumps(payload, ensure_ascii=False) if not isinstance(payload, str) else payload

    monkeypatch.setattr("ci_owner_agent.services.failure_fact_compare_ai.build_chat_model", lambda settings: FakeModel())
    return compare_failure_facts_with_ai(
        settings=settings or make_settings(),
        current_fact=current_fact or make_fact(),
        historical_fact=historical_fact or make_fact(),
    )


def test_compare_disabled():
    result = compare_failure_facts_with_ai(
        settings=make_settings(ai_history_compare_enabled=False),
        current_fact=make_fact(),
        historical_fact=make_fact(),
    )
    assert result.sameFailure is False
    assert result.relationship == "unclear"
    assert "disabled" in result.reason


def test_compare_fake_provider_case_insensitive(monkeypatch):
    def fail_model(settings):
        raise AssertionError("model should not be called")

    monkeypatch.setattr("ci_owner_agent.services.failure_fact_compare_ai.build_chat_model", fail_model)
    result = compare_failure_facts_with_ai(
        settings=make_settings(model_provider="FAKE"),
        current_fact=make_fact(),
        historical_fact=make_fact(),
    )
    assert result.sameFailure is False
    assert "fake provider" in result.reason


def test_compare_blocks_current_generic_wrapper(monkeypatch):
    monkeypatch.setattr("ci_owner_agent.services.failure_fact_compare_ai.build_chat_model", lambda settings: (_ for _ in ()).throw(AssertionError()))
    result = compare_failure_facts_with_ai(
        settings=make_settings(),
        current_fact=make_fact(historyEligible=False, isGenericWrapper=True),
        historical_fact=make_fact(),
    )
    assert result.relationship == "blocked_by_generic_wrapper"


def test_compare_blocks_historical_generic_wrapper(monkeypatch):
    monkeypatch.setattr("ci_owner_agent.services.failure_fact_compare_ai.build_chat_model", lambda settings: (_ for _ in ()).throw(AssertionError()))
    result = compare_failure_facts_with_ai(
        settings=make_settings(),
        current_fact=make_fact(),
        historical_fact=make_fact(isGenericWrapper=True),
    )
    assert result.relationship == "blocked_by_generic_wrapper"


def test_compare_blocks_low_confidence(monkeypatch):
    monkeypatch.setattr("ci_owner_agent.services.failure_fact_compare_ai.build_chat_model", lambda settings: (_ for _ in ()).throw(AssertionError()))
    result = compare_failure_facts_with_ai(
        settings=make_settings(ai_failure_fact_min_confidence=0.7),
        current_fact=make_fact(confidence=0.2),
        historical_fact=make_fact(),
    )
    assert result.relationship == "blocked_by_low_confidence"


def test_compare_same_root_cause(monkeypatch):
    result = _compare(
        monkeypatch,
        {
            "sameFailure": True,
            "confidence": 0.95,
            "relationship": "same_root_cause",
            "samePoints": ["same TS2305 symbol"],
            "differentPoints": [],
            "reason": "same root cause",
        },
    )
    assert result.sameFailure is True
    assert result.relationship == "same_root_cause"


def test_compare_below_threshold_forces_false(monkeypatch):
    result = _compare(
        monkeypatch,
        {
            "sameFailure": True,
            "confidence": 0.7,
            "relationship": "same_root_cause",
            "samePoints": ["same TS2305"],
            "differentPoints": [],
            "reason": "looks same",
        },
        settings=make_settings(ai_history_compare_threshold=0.9),
    )
    assert result.sameFailure is False
    assert result.confidence == 0.7
    assert result.relationship == "unclear"
    assert "threshold" in result.reason


def test_compare_different_root_cause(monkeypatch):
    etarget = make_fact(
        signatureKey="npm_dependency_resolution_error|ETARGET|@ai-sdk/provider|99.0.0-nonexistent",
        failureKind="npm_dependency_resolution_error",
        errorCode="ETARGET",
        packageName="@ai-sdk/provider",
        filePath=None,
        symbol=None,
        message="No matching version found for @ai-sdk/provider@99.0.0-nonexistent",
        rootCauseSummary="bad dependency version",
    )
    result = _compare(
        monkeypatch,
        {
            "sameFailure": False,
            "confidence": 0.96,
            "relationship": "different_root_cause",
            "samePoints": ["都发生在 Docker build 中"],
            "differentPoints": ["TS2305 vs ETARGET"],
            "reason": "different inner root cause",
        },
        current_fact=etarget,
        historical_fact=make_fact(),
    )
    assert result.sameFailure is False
    assert result.relationship == "different_root_cause"


def test_compare_invalid_json(monkeypatch):
    result = _compare(monkeypatch, "not json")
    assert result.sameFailure is False
    assert result.relationship == "unclear"
    assert "invalid JSON" in result.reason


def test_compare_model_returns_invalid_schema(monkeypatch):
    result = _compare(monkeypatch, {"sameFailure": True, "confidence": 0.9, "relationship": "same_root_cause", "extra": "bad"})
    assert result.sameFailure is False
    assert result.relationship == "unclear"
    assert "validation" in result.reason or "failed" in result.reason
