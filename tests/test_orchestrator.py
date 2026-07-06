from __future__ import annotations

from dataclasses import replace

from ci_owner_agent.orchestrator import _with_precomputed_failure_context
from ci_owner_agent.schemas import FailureFact, FailureFactExtractionResult
from tests.test_langchain_agent import make_lc_context


class NoSummaryProvider:
    def find_test_failure_summaries(self, tail_lines=500, max_chunks=5):
        return {"chunks": [], "warning": "test failure summaries unavailable; no Mocha/Japa failure block found"}

    def find_focused_failure_chunks(self, tail_lines=500, max_chunks=1):
        return {"chunks": [{"content": "src/index.ts(10,27): error TS2305"}]}


class SummaryProvider:
    def find_test_failure_summaries(self, tail_lines=500, max_chunks=5):
        return {
            "chunks": [
                {
                    "chunkIndex": 0,
                    "schemaVersion": 3,
                    "chunkSource": "local_test_failure_summary",
                    "content": "1) Some test\nError: boom",
                    "signature": {"signatureKey": "sig"},
                    "signatureHash": "hash",
                }
            ]
        }

    def find_focused_failure_chunks(self, tail_lines=500, max_chunks=1):
        raise AssertionError("focused chunks should not be read when summaries exist")


def test_orchestrator_extracts_ai_failure_facts_when_no_summaries(monkeypatch, repo_cache, sample_repo, logs):
    context = make_lc_context(repo_cache, sample_repo, logs)
    settings = replace(context.settings, ai_failure_facts_enabled=True, history_enabled=False)
    context = replace(context, settings=settings, log_provider=NoSummaryProvider())
    calls = {}

    def fake_extract(**kwargs):
        calls.update(kwargs)
        return FailureFactExtractionResult(
            ok=True,
            facts=[
                FailureFact(
                    signatureKey="typescript_compile_error|TS2305|src/index.ts|classifyErrorMessage",
                    historyEligible=True,
                    failureKind="typescript_compile_error",
                    errorCode="TS2305",
                    message="missing export",
                    rootCauseSummary="missing export",
                    confidence=0.9,
                )
            ],
        )

    monkeypatch.setattr("ci_owner_agent.orchestrator.extract_failure_facts_with_ai", fake_extract)

    result = _with_precomputed_failure_context(context)

    assert calls["log_excerpt"] == "src/index.ts(10,27): error TS2305"
    assert result.failure_facts["ok"] is True
    assert result.failure_facts["facts"][0]["errorCode"] == "TS2305"


def test_orchestrator_does_not_extract_ai_facts_when_summaries_exist(monkeypatch, repo_cache, sample_repo, logs):
    context = make_lc_context(repo_cache, sample_repo, logs)
    settings = replace(context.settings, ai_failure_facts_enabled=True, history_enabled=False)
    context = replace(context, settings=settings, log_provider=SummaryProvider())

    def fail_extract(**kwargs):
        raise AssertionError("AI failure facts should not be extracted when deterministic summaries exist")

    monkeypatch.setattr("ci_owner_agent.orchestrator.extract_failure_facts_with_ai", fail_extract)

    result = _with_precomputed_failure_context(context)

    assert result.failure_facts is None
    assert result.failure_summaries["chunks"]


def test_orchestrator_runs_ai_history_precheck_when_no_summaries_and_facts(monkeypatch, repo_cache, sample_repo, logs):
    context = make_lc_context(repo_cache, sample_repo, logs)
    settings = replace(
        context.settings,
        ai_failure_facts_enabled=True,
        ai_history_compare_enabled=True,
        history_enabled=True,
    )
    context = replace(context, settings=settings, log_provider=NoSummaryProvider())
    fact = FailureFact(
        signatureKey="typescript_compile_error|TS2305|src/index.ts|classifyErrorMessage",
        historyEligible=True,
        failureKind="typescript_compile_error",
        errorCode="TS2305",
        message="missing export",
        rootCauseSummary="missing export",
        confidence=0.9,
    )
    calls = {}

    monkeypatch.setattr(
        "ci_owner_agent.orchestrator.extract_failure_facts_with_ai",
        lambda **kwargs: FailureFactExtractionResult(ok=True, facts=[fact]),
    )
    monkeypatch.setattr(
        "ci_owner_agent.orchestrator.history_search_similar_failures",
        lambda *args, **kwargs: {"ok": True, "currentChunks": [], "candidates": []},
    )

    def fake_ai_history(enriched):
        calls["context"] = enriched
        return {
            "ok": True,
            "mode": "ai_failure_facts",
            "currentFacts": [{"factId": fact.factId, "inheritedOwner": {"found": False}}],
            "candidates": [],
        }

    monkeypatch.setattr("ci_owner_agent.orchestrator.history_search_similar_failure_facts", fake_ai_history)

    result = _with_precomputed_failure_context(context)

    assert calls["context"].failure_facts["facts"][0]["errorCode"] == "TS2305"
    assert result.ai_history_precheck["mode"] == "ai_failure_facts"


def test_orchestrator_skips_ai_history_when_summaries_exist(monkeypatch, repo_cache, sample_repo, logs):
    context = make_lc_context(repo_cache, sample_repo, logs)
    settings = replace(
        context.settings,
        ai_failure_facts_enabled=True,
        ai_history_compare_enabled=True,
        history_enabled=True,
    )
    context = replace(context, settings=settings, log_provider=SummaryProvider())
    monkeypatch.setattr("ci_owner_agent.orchestrator.history_search_similar_failures", lambda *args, **kwargs: {"ok": True, "candidates": []})

    def fail_ai_history(*args, **kwargs):
        raise AssertionError("AI history should not run when deterministic summaries exist")

    monkeypatch.setattr("ci_owner_agent.orchestrator.history_search_similar_failure_facts", fail_ai_history)

    result = _with_precomputed_failure_context(context)

    assert result.ai_history_precheck is None


def test_orchestrator_skips_ai_history_when_compare_disabled(monkeypatch, repo_cache, sample_repo, logs):
    context = make_lc_context(repo_cache, sample_repo, logs)
    settings = replace(
        context.settings,
        ai_failure_facts_enabled=True,
        ai_history_compare_enabled=False,
        history_enabled=True,
    )
    context = replace(context, settings=settings, log_provider=NoSummaryProvider())
    monkeypatch.setattr(
        "ci_owner_agent.orchestrator.extract_failure_facts_with_ai",
        lambda **kwargs: FailureFactExtractionResult(
            ok=True,
            facts=[
                FailureFact(
                    signatureKey="typescript_compile_error|TS2305|src/index.ts|classifyErrorMessage",
                    historyEligible=True,
                    failureKind="typescript_compile_error",
                    message="missing export",
                    rootCauseSummary="missing export",
                    confidence=0.9,
                )
            ],
        ),
    )
    monkeypatch.setattr("ci_owner_agent.orchestrator.history_search_similar_failures", lambda *args, **kwargs: {"ok": True, "candidates": []})

    def fail_ai_history(*args, **kwargs):
        raise AssertionError("AI history should not run when compare is disabled")

    monkeypatch.setattr("ci_owner_agent.orchestrator.history_search_similar_failure_facts", fail_ai_history)

    result = _with_precomputed_failure_context(context)

    assert result.failure_facts["facts"]
    assert result.ai_history_precheck is None


def test_orchestrator_ai_history_exception_does_not_fail(monkeypatch, repo_cache, sample_repo, logs):
    context = make_lc_context(repo_cache, sample_repo, logs)
    settings = replace(
        context.settings,
        ai_failure_facts_enabled=True,
        ai_history_compare_enabled=True,
        history_enabled=True,
    )
    context = replace(context, settings=settings, log_provider=NoSummaryProvider())
    monkeypatch.setattr(
        "ci_owner_agent.orchestrator.extract_failure_facts_with_ai",
        lambda **kwargs: FailureFactExtractionResult(
            ok=True,
            facts=[
                FailureFact(
                    signatureKey="typescript_compile_error|TS2305|src/index.ts|classifyErrorMessage",
                    historyEligible=True,
                    failureKind="typescript_compile_error",
                    message="missing export",
                    rootCauseSummary="missing export",
                    confidence=0.9,
                )
            ],
        ),
    )
    monkeypatch.setattr("ci_owner_agent.orchestrator.history_search_similar_failures", lambda *args, **kwargs: {"ok": True, "candidates": []})
    monkeypatch.setattr(
        "ci_owner_agent.orchestrator.history_search_similar_failure_facts",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("ai history exploded")),
    )

    result = _with_precomputed_failure_context(context)

    assert result.ai_history_precheck["ok"] is False
    assert "ai history exploded" in result.ai_history_precheck["error"]
