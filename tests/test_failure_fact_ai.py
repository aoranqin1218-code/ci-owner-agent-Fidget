from __future__ import annotations

import json
from dataclasses import replace

from ci_owner_agent.config import load_settings
from ci_owner_agent.services.failure_fact_ai import extract_failure_facts_with_ai


def _settings(**overrides):
    base = replace(
        load_settings(),
        ai_failure_facts_enabled=True,
        model_provider="doubao",
        model_name="test-model",
        model_base_url="https://example.test/v1",
        api_key="test-key",
        ai_failure_fact_min_confidence=0.7,
    )
    return replace(base, **overrides)


def _extract(settings, monkeypatch, payload):
    class FakeModel:
        def invoke(self, prompt):
            return json.dumps(payload, ensure_ascii=False)

    monkeypatch.setattr("ci_owner_agent.services.failure_fact_ai.build_chat_model", lambda settings: FakeModel())
    return extract_failure_facts_with_ai(
        settings=settings,
        job="services/fx-code-unittest",
        build_number=7,
        build_url="local://job/7",
        branch="dev",
        log_excerpt="log",
        changed_files=[],
        commits=[],
    )


def test_extract_failure_facts_disabled():
    result = extract_failure_facts_with_ai(
        settings=_settings(ai_failure_facts_enabled=False),
        job="job",
        build_number=1,
        build_url="url",
        branch=None,
        log_excerpt="log",
        changed_files=[],
        commits=[],
    )
    assert result.ok is False
    assert result.warning == "AI failure facts disabled"


def test_extract_failure_facts_fake_provider_disabled():
    result = extract_failure_facts_with_ai(
        settings=_settings(model_provider="fake"),
        job="job",
        build_number=1,
        build_url="url",
        branch=None,
        log_excerpt="log",
        changed_files=[],
        commits=[],
    )
    assert result.ok is False
    assert result.warning == "AI failure facts disabled for fake provider"


def test_extract_failure_facts_fake_provider_case_insensitive():
    result = extract_failure_facts_with_ai(
        settings=_settings(model_provider="FAKE"),
        job="job",
        build_number=1,
        build_url="url",
        branch=None,
        log_excerpt="log",
        changed_files=[],
        commits=[],
    )
    assert result.ok is False
    assert result.warning == "AI failure facts disabled for fake provider"


def test_extract_failure_facts_model_returns_ok_false(monkeypatch):
    result = _extract(
        _settings(),
        monkeypatch,
        {"ok": False, "facts": [], "warning": "cannot extract"},
    )
    assert result.ok is False
    assert result.facts == []
    assert "cannot extract" in result.warning


def test_extract_failure_facts_ts2305(monkeypatch):
    result = _extract(
        _settings(),
        monkeypatch,
        {
            "ok": True,
            "facts": [
                {
                    "schemaVersion": 1,
                    "signatureKey": "typescript_compile_error|TS2305|packages/fxp-ai/src/index.ts|classifyErrorMessage",
                    "historyEligible": True,
                    "isGenericWrapper": False,
                    "failureKind": "typescript_compile_error",
                    "phase": "nx:build",
                    "command": "npm run nx:build",
                    "errorCode": "TS2305",
                    "errorType": "TypeScriptCompileError",
                    "packageName": "@fx/ai",
                    "filePath": "packages/fxp-ai/src/index.ts",
                    "symbol": "classifyErrorMessage",
                    "message": "Module './errors' has no exported member 'classifyErrorMessage'",
                    "rootCauseSummary": "errors/index.ts no longer exports classifyErrorMessage",
                    "evidenceLines": ["src/index.ts(10,27): error TS2305"],
                    "confidence": 0.92,
                }
            ],
            "warning": None,
        },
    )
    assert result.ok is True
    assert result.facts[0].failureKind == "typescript_compile_error"
    assert result.facts[0].errorCode == "TS2305"
    assert result.facts[0].historyEligible is True
    assert result.facts[0].isGenericWrapper is False
    assert result.facts[0].factId.startswith("fact-")


def test_extract_failure_facts_npm_etarget(monkeypatch):
    result = _extract(
        _settings(),
        monkeypatch,
        {
            "ok": True,
            "facts": [
                {
                    "signatureKey": "npm_dependency_resolution_error|ETARGET|@ai-sdk/provider|99.0.0-nonexistent",
                    "historyEligible": True,
                    "failureKind": "npm_dependency_resolution_error",
                    "errorCode": "ETARGET",
                    "errorType": "NpmDependencyResolutionError",
                    "packageName": "@ai-sdk/provider",
                    "message": "No matching version found for @ai-sdk/provider@99.0.0-nonexistent",
                    "rootCauseSummary": "package.json or lockfile references a nonexistent version",
                    "confidence": 0.95,
                }
            ],
        },
    )
    assert result.ok is True
    assert result.facts[0].packageName == "@ai-sdk/provider"


def test_extract_failure_facts_generic_wrapper(monkeypatch):
    result = _extract(
        _settings(ai_failure_fact_min_confidence=0.1),
        monkeypatch,
        {
            "ok": True,
            "facts": [
                {
                    "signatureKey": "generic_wrapper|docker",
                    "historyEligible": False,
                    "isGenericWrapper": True,
                    "failureKind": "generic_wrapper",
                    "message": "ERROR: process did not complete successfully",
                    "rootCauseSummary": "只有外层 wrapper，无法提取稳定内层失败事实",
                    "confidence": 0.4,
                }
            ],
        },
    )
    assert result.ok is True
    assert result.facts[0].historyEligible is False
    assert result.facts[0].isGenericWrapper is True


def test_extract_failure_facts_invalid_json(monkeypatch):
    class FakeModel:
        def invoke(self, prompt):
            return "not json"

    monkeypatch.setattr("ci_owner_agent.services.failure_fact_ai.build_chat_model", lambda settings: FakeModel())
    result = extract_failure_facts_with_ai(
        settings=_settings(),
        job="job",
        build_number=1,
        build_url="url",
        branch=None,
        log_excerpt="log",
        changed_files=[],
        commits=[],
    )
    assert result.ok is False
    assert "invalid JSON" in result.warning


def test_extract_failure_facts_filters_low_confidence(monkeypatch):
    result = _extract(
        _settings(ai_failure_fact_min_confidence=0.7),
        monkeypatch,
        {
            "ok": True,
            "facts": [
                {
                    "signatureKey": "typescript_compile_error|TS2305|x",
                    "historyEligible": True,
                    "failureKind": "typescript_compile_error",
                    "message": "x",
                    "rootCauseSummary": "x",
                    "confidence": 0.2,
                }
            ],
        },
    )
    assert result.ok is True
    assert result.facts == []
