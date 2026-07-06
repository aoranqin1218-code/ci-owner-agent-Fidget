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
