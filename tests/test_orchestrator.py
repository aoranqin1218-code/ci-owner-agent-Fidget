from __future__ import annotations

from dataclasses import replace

import pytest

from ci_owner_agent.orchestrator import (
    _build_no_owner_notice_from_history_decision,
    _has_new_strong_evidence,
    _save_history,
    _select_build_level_no_owner_decision,
    _validate_trusted_history_no_owner_decisions,
    _with_precomputed_failure_context,
    apply_history_no_owner_sources,
    analyze_failed_build,
    analyze_local,
)
from ci_owner_agent.config import load_settings
from ci_owner_agent.schemas import BuildInfo, ChangedFile, CiResponsibilityNotice, FailureFact, FailureFactExtractionResult
from ci_owner_agent.services.git_client import GitClient
from ci_owner_agent.services.metrics import AnalysisMetricsRecorder, use_metrics_recorder
from ci_owner_agent.services.notification_formatter import format_wecom_markdown_notice
from ci_owner_agent.services.test_maintainer_mapping import TestMaintainerResolver
from tests.test_history_store import high_confidence_payload, make_store, no_owner_item
from tests.test_langchain_agent import make_lc_context


class NoSummaryProvider:
    def find_test_failure_summaries(self, tail_lines=500, max_chunks=5):
        return {"chunks": [], "warning": "test failure summaries unavailable; no Mocha/Japa failure block found"}

    def find_focused_failure_chunks(self, tail_lines=500, max_chunks=1):
        return {"chunks": [{"content": "src/index.ts(10,27): error TS2305"}]}


class RecordingGitClient:
    def __init__(self):
        self.commit_ranges = []
        self.diff_ranges = []

    def sync(self, repo):
        return {"ok": True}

    def check_ancestor(self, repo, base_commit, head_commit):
        return {"ok": True, "isAncestor": True}

    def get_commits_between(self, repo, base_commit, head_commit):
        self.commit_ranges.append((base_commit, head_commit))
        return {
            "ok": True,
            "commits": [
                {
                    "hash": head_commit,
                    "authorName": "Test User",
                    "authorEmail": "test@example.com",
                    "subject": "change",
                }
            ],
        }

    def get_diff_files(self, repo, base_commit, head_commit):
        self.diff_ranges.append((base_commit, head_commit))
        return {"ok": True, "files": [{"path": "packages/fxp-ai/src/index.ts", "status": "M", "additions": 1, "deletions": 0}]}


def _agent_notice_payload(*, item_metadata=None, item_owner_metadata=None, **metadata):
    payload = {
        "repo": "fx-code",
        "job": "services/fx-code-unittest",
        "buildNumber": 5221,
        "buildUrl": "https://jenkins.example/job/services/job/fx-code-unittest/5221/",
        "result": "FAILURE",
        "branch": "dev",
        "baseCommit": "base-commit",
        "headCommit": "head-commit",
        "owner": {
            "type": "high_confidence",
            "name": "Zhang San",
            "email": "zhangsan@example.com",
            "commit": "head-commit",
            "confidence": 0.88,
        },
        "failureReason": "four tests failed after the current change",
        "evidence": [
            {
                "id": "E1",
                "type": "log",
                "summary": "4 failing",
                "detail": "5033 passing, 142 pending, 4 failing",
                "source": "jenkins log",
            },
            {
                "id": "E2",
                "type": "diff",
                "summary": "related code changed",
                "detail": "the failing code changed in this build",
                "source": "git diff",
            },
        ],
        "suggestions": ["fix the four failing tests"],
        "responsibilityItems": [
            {
                "failureId": "model-generated-id",
                "failureTitle": "four unit test failures",
                "failureSignature": "four-unit-test-failures",
                "failureSummary": "four tests failed",
                "owner": {
                    "type": "high_confidence",
                    "name": "Zhang San",
                    "email": "zhangsan@example.com",
                    "commit": None,
                    "confidence": 0.88,
                },
                "responsibilityType": "current_build_owner",
                "confidence": 0.88,
                "reason": "the changed code is directly related to the failures",
                "evidenceIds": ["E1", "E2"],
            }
        ],
        "hasHighConfidenceOwner": True,
    }
    payload.update(metadata)
    payload["responsibilityItems"][0].update(item_metadata or {})
    payload["responsibilityItems"][0]["owner"].update(item_owner_metadata or {})
    return payload


def _analyze_agent_notice(monkeypatch, payload, *, history_store=None):
    class DummyAgent:
        def analyze(self, agent_context):
            return CiResponsibilityNotice.model_validate(payload)

    monkeypatch.setattr("ci_owner_agent.orchestrator.create_responsibility_agent", lambda *args, **kwargs: DummyAgent())
    build_info = BuildInfo(
        job="services/fx-code-unittest",
        buildNumber=5221,
        result="FAILURE",
        buildUrl="https://jenkins.example/job/services/job/fx-code-unittest/5221/",
        branch="dev",
        commit="build-info-commit",
    )
    settings = replace(load_settings(), history_enabled=history_store is not None, ai_failure_facts_enabled=False)
    notice = analyze_failed_build(
        "fx-code",
        build_info,
        "base-commit",
        "head-commit",
        NoSummaryProvider(),
        RecordingGitClient(),
        settings=settings,
        history_store=history_store,
    )
    return notice, build_info


def test_analyze_failed_build_restores_placeholder_metadata_and_derived_sources(monkeypatch):
    payload = _agent_notice_payload(
        item_metadata={"sourceBuildNumber": None, "sourceBuildUrl": None, "sourceCommit": None},
        item_owner_metadata={"commit": None},
        repo=None,
        job="unknown",
        buildNumber=0,
        buildUrl="",
        result="UNKNOWN",
        branch=None,
        baseCommit=None,
        headCommit=None,
    )
    recorder = AnalysisMetricsRecorder(enabled=True)

    with use_metrics_recorder(recorder):
        notice, _ = _analyze_agent_notice(monkeypatch, payload)

    assert notice.repo == "fx-code"
    assert notice.job == "services/fx-code-unittest"
    assert notice.buildNumber == 5221
    assert notice.buildUrl == "https://jenkins.example/job/services/job/fx-code-unittest/5221/"
    assert notice.result == "FAILURE"
    assert notice.branch == "dev"
    assert notice.baseCommit == "base-commit"
    assert notice.headCommit == "head-commit"
    assert notice.responsibilityItems[0].sourceBuildNumber == 5221
    assert notice.responsibilityItems[0].sourceBuildUrl == (
        "https://jenkins.example/job/services/job/fx-code-unittest/5221/"
    )
    assert notice.responsibilityItems[0].sourceCommit == "head-commit"
    assert notice.owner.name == "Zhang San"
    assert notice.failureReason == "four tests failed after the current change"
    assert notice.evidence[0].summary == "4 failing"
    assert notice.suggestions == ["fix the four failing tests"]
    assert notice.hasHighConfidenceOwner is True
    assert notice.responsibilityItems[0].reason == "the changed code is directly related to the failures"
    assert recorder.warnings == [
        "restored authoritative notice metadata: repo,job,buildNumber,buildUrl,result,branch,baseCommit,headCommit"
    ]


def test_analyze_failed_build_overwrites_plausible_but_wrong_metadata(monkeypatch):
    payload = _agent_notice_payload(
        item_metadata={
            "sourceBuildNumber": 9999,
            "sourceBuildUrl": "https://wrong.example/job/9999/",
            "sourceCommit": "wrong-head",
        },
        item_owner_metadata={"commit": None},
        repo="another-repo",
        job="another-job",
        buildNumber=9999,
        buildUrl="https://wrong.example/9999/",
        result="SUCCESS",
        branch="main",
        baseCommit="wrong-base",
        headCommit="wrong-head",
    )

    notice, _ = _analyze_agent_notice(monkeypatch, payload)

    assert notice.repo == "fx-code"
    assert notice.job == "services/fx-code-unittest"
    assert notice.buildNumber == 5221
    assert notice.buildUrl == "https://jenkins.example/job/services/job/fx-code-unittest/5221/"
    assert notice.result == "FAILURE"
    assert notice.branch == "dev"
    assert notice.baseCommit == "base-commit"
    assert notice.headCommit == "head-commit"
    assert notice.responsibilityItems[0].sourceBuildNumber == 5221
    assert notice.responsibilityItems[0].sourceBuildUrl == (
        "https://jenkins.example/job/services/job/fx-code-unittest/5221/"
    )
    assert notice.responsibilityItems[0].sourceCommit == "head-commit"


def test_analyze_failed_build_preserves_explicit_current_build_source_commit(monkeypatch):
    payload = _agent_notice_payload(
        item_metadata={"sourceCommit": "specific-culprit-commit"},
        item_owner_metadata={"commit": None},
        headCommit="wrong-model-head",
    )

    notice, _ = _analyze_agent_notice(monkeypatch, payload)

    assert notice.headCommit == "head-commit"
    assert notice.responsibilityItems[0].sourceCommit == "specific-culprit-commit"


def test_analyze_failed_build_preserves_source_commit_equal_to_correct_head(monkeypatch):
    payload = _agent_notice_payload(
        item_metadata={
            "sourceBuildNumber": 5221,
            "sourceBuildUrl": "https://jenkins.example/job/services/job/fx-code-unittest/5221/",
            "sourceCommit": "head-commit",
        },
        item_owner_metadata={"commit": "another-commit"},
        headCommit="head-commit",
    )

    notice, _ = _analyze_agent_notice(monkeypatch, payload)

    item = notice.responsibilityItems[0]
    assert notice.headCommit == "head-commit"
    assert item.sourceCommit == "head-commit"
    assert item.sourceBuildNumber == 5221
    assert item.sourceBuildUrl == "https://jenkins.example/job/services/job/fx-code-unittest/5221/"


@pytest.mark.parametrize(
    ("owner_commit", "expected_source_commit"),
    [
        ("specific-owner-commit", "specific-owner-commit"),
        (None, "head-commit"),
    ],
)
def test_analyze_failed_build_derives_missing_current_build_source_commit(
    monkeypatch,
    owner_commit,
    expected_source_commit,
):
    payload = _agent_notice_payload(
        item_metadata={"sourceCommit": None},
        item_owner_metadata={"commit": owner_commit},
    )

    notice, _ = _analyze_agent_notice(monkeypatch, payload)

    assert notice.responsibilityItems[0].sourceCommit == expected_source_commit


@pytest.mark.parametrize(
    ("owner_commit", "expected_source_commit"),
    [
        ("real-culprit-commit", "real-culprit-commit"),
        (None, "head-commit"),
    ],
)
def test_analyze_failed_build_rebuilds_source_commit_derived_from_wrong_model_head(
    monkeypatch,
    owner_commit,
    expected_source_commit,
):
    payload = _agent_notice_payload(
        item_metadata={"sourceCommit": "wrong-model-head"},
        item_owner_metadata={"commit": owner_commit},
        headCommit="wrong-model-head",
    )

    notice, _ = _analyze_agent_notice(monkeypatch, payload)

    assert notice.headCommit == "head-commit"
    assert notice.responsibilityItems[0].sourceCommit == expected_source_commit


def test_analyze_failed_build_preserves_inherited_owner_sources(monkeypatch):
    payload = _agent_notice_payload(
        item_metadata={
            "responsibilityType": "inherited_failure_owner",
            "sourceBuildNumber": 5001,
            "sourceBuildUrl": "https://jenkins.example/job/test/5001/",
            "sourceCommit": "historical-culprit",
            "matchType": "signature_exact",
            "relationship": "same_failure",
        },
        item_owner_metadata={
            "type": "inherited_failure_owner",
            "commit": "historical-culprit",
        },
        headCommit="wrong-model-head",
    )

    notice, _ = _analyze_agent_notice(monkeypatch, payload)

    item = notice.responsibilityItems[0]
    assert item.responsibilityType == "inherited_failure_owner"
    assert item.sourceBuildNumber == 5001
    assert item.sourceBuildUrl == "https://jenkins.example/job/test/5001/"
    assert item.sourceCommit == "historical-culprit"


def test_analyze_failed_build_saves_restored_notice_to_history(monkeypatch):
    store = make_store()
    payload = _agent_notice_payload(
        item_metadata={
            "sourceBuildNumber": 9999,
            "sourceBuildUrl": "https://wrong.example/job/9999/",
            "sourceCommit": "specific-culprit-commit",
        },
        job="unknown",
        buildNumber=0,
        buildUrl="",
        result="UNKNOWN",
    )

    notice, _ = _analyze_agent_notice(monkeypatch, payload, history_store=store)

    assert notice.job == "services/fx-code-unittest"
    assert notice.buildNumber == 5221
    assert notice.result == "FAILURE"
    saved_notice = store.notices.docs[0]["notice"]
    assert saved_notice["job"] == "services/fx-code-unittest"
    assert saved_notice["buildNumber"] == 5221
    assert saved_notice["result"] == "FAILURE"
    saved_item = saved_notice["responsibilityItems"][0]
    assert saved_item["sourceBuildNumber"] == 5221
    assert saved_item["sourceBuildUrl"] == (
        "https://jenkins.example/job/services/job/fx-code-unittest/5221/"
    )
    assert saved_item["sourceCommit"] == "specific-culprit-commit"


def test_analyze_failed_build_keeps_matching_metadata_and_analysis(monkeypatch):
    payload = _agent_notice_payload(
        item_metadata={
            "sourceBuildNumber": 5221,
            "sourceBuildUrl": "https://jenkins.example/job/services/job/fx-code-unittest/5221/",
            "sourceCommit": "specific-culprit-commit",
        }
    )
    expected_signature = CiResponsibilityNotice.model_validate(payload).responsibilityItems[0].failureSignature
    recorder = AnalysisMetricsRecorder(enabled=True)

    with use_metrics_recorder(recorder):
        notice, _ = _analyze_agent_notice(monkeypatch, payload)

    assert notice.model_dump(include={"repo", "job", "buildNumber", "buildUrl", "result", "branch", "baseCommit", "headCommit"}) == {
        "repo": "fx-code",
        "job": "services/fx-code-unittest",
        "buildNumber": 5221,
        "buildUrl": "https://jenkins.example/job/services/job/fx-code-unittest/5221/",
        "result": "FAILURE",
        "branch": "dev",
        "baseCommit": "base-commit",
        "headCommit": "head-commit",
    }
    assert notice.owner.name == "Zhang San"
    assert notice.failureReason == "four tests failed after the current change"
    item = notice.responsibilityItems[0]
    assert item.responsibilityType == "current_build_owner"
    assert item.sourceBuildNumber == 5221
    assert item.sourceBuildUrl == "https://jenkins.example/job/services/job/fx-code-unittest/5221/"
    assert item.sourceCommit == "specific-culprit-commit"
    assert item.owner.name == "Zhang San"
    assert item.reason == "the changed code is directly related to the failures"
    assert item.evidenceIds == ["E1", "E2"]
    assert item.failureSignature == expected_signature
    assert notice.evidence[0].summary == "4 failing"
    assert notice.suggestions == ["fix the four failing tests"]
    assert recorder.warnings == []


def test_analyze_failed_build_stops_before_diff_when_base_is_not_ancestor():
    class NonAncestorGitClient:
        def sync(self, repo): return {"ok": True}
        def check_ancestor(self, repo, base, head): return {"ok": True, "isAncestor": False}
        def get_commits_between(self, *args): raise AssertionError("must not read diff")
        def get_diff_files(self, *args): raise AssertionError("must not read diff")

    build = BuildInfo(job="job", buildNumber=2, result="FAILURE", buildUrl="local://job/2", branch="dev", commit="head")
    notice = analyze_failed_build("repo", build, "base", "head", NoSummaryProvider(), NonAncestorGitClient(), settings=replace(load_settings(), history_enabled=False), allow_sync_failure=True)
    assert notice.owner.type == "no_high_confidence_owner"
    assert "不是本次构建提交的祖先" in notice.failureReason


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


class SigSummaryProvider:
    def __init__(self, signature_key: str = "sig-timeout", test_file: str = "modules/automation/tests/venv.ts"):
        self.signature_key = signature_key
        self.test_file = test_file

    def find_test_failure_summaries(self, tail_lines=500, max_chunks=5):
        return {
            "chunks": [
                {
                    "chunkIndex": 0,
                    "schemaVersion": 3,
                    "chunkSource": "local_test_failure_summary",
                    "content": "✖ Webhook触发\nRun\nAwaitFunc\nTimeout!",
                    "signature": {
                        "signatureKey": self.signature_key,
                        "testName": "Webhook触发",
                        "errorType": "Timeout",
                        "errorMessage": "run awaitfunc timeout",
                        "testFile": self.test_file,
                        "topStackFile": self.test_file,
                    },
                    "signatureHash": self.signature_key,
                }
            ]
        }

    def find_focused_failure_chunks(self, tail_lines=500, max_chunks=1):
        return {"chunks": [{"content": "✖ Webhook触发\nRun\nAwaitFunc\nTimeout!"}]}


class MultiSigSummaryProvider:
    def __init__(self, signatures: list[str], test_files: list[str] | None = None):
        self.signatures = signatures
        self.test_files = test_files or [f"modules/automation/tests/{signature}.ts" for signature in signatures]

    def find_test_failure_summaries(self, tail_lines=500, max_chunks=5):
        chunks = []
        for index, signature_key in enumerate(self.signatures):
            chunks.append(
                {
                    "chunkIndex": index,
                    "schemaVersion": 3,
                    "chunkSource": "local_test_failure_summary",
                    "content": f"✖ failure {index}\nRun\nAwaitFunc\nTimeout!",
                    "signature": {
                        "signatureKey": signature_key,
                        "testName": f"failure {index}",
                        "errorType": "Timeout",
                        "errorMessage": "run awaitfunc timeout",
                        "testFile": self.test_files[index],
                        "topStackFile": self.test_files[index],
                    },
                    "signatureHash": signature_key,
                }
            )
        return {"chunks": chunks}

    def find_focused_failure_chunks(self, tail_lines=500, max_chunks=1):
        return {"chunks": [{"content": "multiple timeout failures"}]}


def _save_existing_failure_fact(store, context, fact: FailureFact) -> tuple[BuildInfo, CiResponsibilityNotice]:
    payload = high_confidence_payload(context)
    payload["buildNumber"] = 7
    payload["responsibilityItems"] = [
        {
            "failureId": "failure-7",
            "failureTitle": "TS2305",
            "failureSignature": fact.signatureKey,
            "owner": {
                "type": "high_confidence",
                "name": "test",
                "email": "test@test.com",
                "commit": context.head_commit,
                "confidence": 0.9,
            },
            "responsibilityType": "current_build_owner",
            "confidence": 0.9,
            "reason": "existing fact owner",
        }
    ]
    notice = CiResponsibilityNotice.model_validate(payload)
    build_info = BuildInfo(
        job=context.job,
        buildNumber=7,
        result="FAILURE",
        buildUrl=context.build_url,
        branch=context.branch,
        commit=context.head_commit,
    )
    store.save_analysis(build_info, notice, context.base_commit, context.head_commit, 6, context.base_commit, [])
    store.save_failure_facts(build_info=build_info, notice=notice, facts=[fact])
    return build_info, notice


def _save_historical_no_owner(store, context, *, signature_key: str = "sig-timeout", build: int = 5088):
    payload = high_confidence_payload(context)
    payload["owner"] = {"type": "no_high_confidence_owner", "name": "无高可信责任人", "email": None, "commit": None, "confidence": 0}
    payload["hasHighConfidenceOwner"] = False
    payload["responsibilityItems"] = [no_owner_item(failure_id="F1", failure_title="Webhook触发", failure_signature=signature_key)]
    notice = CiResponsibilityNotice.model_validate(payload)
    build_info = BuildInfo(job=context.job, buildNumber=build, result="FAILURE", buildUrl=f"local://job/{build}", branch=context.branch, commit=context.head_commit)
    chunk = SigSummaryProvider(signature_key).find_test_failure_summaries()["chunks"][0]
    store.save_analysis(build_info, notice, context.base_commit, context.head_commit, build - 1, context.base_commit, [chunk])
    return build_info, notice


def _save_historical_no_owner_chunks(store, context, *, signature_keys: list[str], build: int = 5088):
    payload = high_confidence_payload(context)
    payload["owner"] = {"type": "no_high_confidence_owner", "name": "无高可信责任人", "email": None, "commit": None, "confidence": 0}
    payload["hasHighConfidenceOwner"] = False
    payload["responsibilityItems"] = [
        no_owner_item(failure_id=f"F{index + 1}", failure_title=f"failure {index}", failure_signature=signature_key)
        for index, signature_key in enumerate(signature_keys)
    ]
    notice = CiResponsibilityNotice.model_validate(payload)
    build_info = BuildInfo(job=context.job, buildNumber=build, result="FAILURE", buildUrl=f"local://job/{build}", branch=context.branch, commit=context.head_commit)
    chunks = MultiSigSummaryProvider(signature_keys).find_test_failure_summaries()["chunks"]
    store.save_analysis(build_info, notice, context.base_commit, context.head_commit, build - 1, context.base_commit, chunks)
    return build_info, notice


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

    def fake_ai_history(enriched, **kwargs):
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


def test_orchestrator_reuses_history_store_for_prechecks(monkeypatch, repo_cache, sample_repo, logs):
    context = make_lc_context(repo_cache, sample_repo, logs)
    settings = replace(
        context.settings,
        ai_failure_facts_enabled=True,
        ai_history_compare_enabled=True,
        history_enabled=True,
    )
    context = replace(context, settings=settings, log_provider=NoSummaryProvider())
    store = make_store()
    calls = {}
    fact = FailureFact(
        signatureKey="typescript_compile_error|TS2305|src/index.ts|classifyErrorMessage",
        historyEligible=True,
        failureKind="typescript_compile_error",
        message="missing export",
        rootCauseSummary="missing export",
        confidence=0.9,
    )
    monkeypatch.setattr(
        "ci_owner_agent.orchestrator.extract_failure_facts_with_ai",
        lambda **kwargs: FailureFactExtractionResult(ok=True, facts=[fact]),
    )

    def fake_history(enriched, **kwargs):
        calls["deterministic_store"] = kwargs.get("store")
        return {"ok": True, "currentChunks": [], "candidates": []}

    def fake_ai_history(enriched, **kwargs):
        calls["ai_store"] = kwargs.get("store")
        return {"ok": True, "mode": "ai_failure_facts", "currentFacts": [], "candidates": []}

    monkeypatch.setattr("ci_owner_agent.orchestrator.history_search_similar_failures", fake_history)
    monkeypatch.setattr("ci_owner_agent.orchestrator.history_search_similar_failure_facts", fake_ai_history)

    _with_precomputed_failure_context(context, history_store=store)

    assert calls["deterministic_store"] is store
    assert calls["ai_store"] is store


def test_analyze_failed_build_uses_previous_commit_focus_range(monkeypatch, repo_cache, sample_repo, logs):
    context = make_lc_context(repo_cache, sample_repo, logs)
    settings = replace(context.settings, history_enabled=False)
    git_client = RecordingGitClient()
    calls = {"agent": 0}

    class DummyAgent:
        def analyze(self, agent_context):
            calls["agent"] += 1
            payload = high_confidence_payload(context)
            payload["baseCommit"] = "last-success"
            payload["headCommit"] = "current"
            payload["responsibilityItems"] = [
                {
                    "failureId": "auto",
                    "failureTitle": "Webhook触发",
                    "failureSignature": "sig-timeout",
                    "owner": payload["owner"],
                    "responsibilityType": "current_build_owner",
                    "confidence": 0.88,
                    "reason": "日志和 diff 直接关联。",
                    "evidenceIds": ["E1", "E2"],
                }
            ]
            return CiResponsibilityNotice.model_validate(payload)

    monkeypatch.setattr("ci_owner_agent.orchestrator.create_responsibility_agent", lambda *args, **kwargs: DummyAgent())
    build_info = BuildInfo(job=context.job, buildNumber=5088, result="FAILURE", buildUrl="local://job/5088", branch=context.branch, commit="current")

    notice = analyze_failed_build(
        sample_repo["repo"],
        build_info,
        "last-success",
        "current",
        SigSummaryProvider("sig-timeout"),
        git_client,
        allow_sync_failure=True,
        settings=settings,
        last_successful_build_number=5068,
        previous_build_number=5087,
        previous_commit="previous",
    )

    assert git_client.commit_ranges == [("previous", "current")]
    assert git_client.diff_ranges == [("previous", "current")]
    assert notice.baseCommit == "last-success"
    assert notice.headCommit == "current"
    assert notice.repo == sample_repo["repo"]
    assert notice.responsibilityItems[0].testFilePath == "modules/automation/tests/venv.ts"
    assert calls["agent"] == 1


def test_analyze_failed_build_falls_back_to_full_when_no_previous_commit(monkeypatch, repo_cache, sample_repo, logs):
    context = make_lc_context(repo_cache, sample_repo, logs)
    settings = replace(context.settings, history_enabled=False)
    git_client = RecordingGitClient()

    class DummyAgent:
        def analyze(self, agent_context):
            return CiResponsibilityNotice.model_validate(high_confidence_payload(context))

    monkeypatch.setattr("ci_owner_agent.orchestrator.create_responsibility_agent", lambda *args, **kwargs: DummyAgent())
    build_info = BuildInfo(job=context.job, buildNumber=5088, result="FAILURE", buildUrl="local://job/5088", branch=context.branch, commit="current")

    analyze_failed_build(
        sample_repo["repo"],
        build_info,
        "last-success",
        "current",
        SigSummaryProvider("sig-timeout"),
        git_client,
        allow_sync_failure=True,
        settings=settings,
        last_successful_build_number=5068,
    )

    assert git_client.commit_ranges == [("last-success", "current")]
    assert git_client.diff_ranges == [("last-success", "current")]


def test_find_previous_build_from_mongo_when_cli_previous_missing(monkeypatch, repo_cache, sample_repo, logs):
    context = make_lc_context(repo_cache, sample_repo, logs)
    settings = replace(context.settings, history_enabled=True)
    store = make_store()
    previous_notice = CiResponsibilityNotice.model_validate(high_confidence_payload(context))
    previous_info = BuildInfo(job=context.job, buildNumber=5087, result="FAILURE", buildUrl="local://job/5087", branch=context.branch, commit="previous")
    store.save_analysis(previous_info, previous_notice, "last-success", "previous", 5068, "last-success", [])
    git_client = RecordingGitClient()

    class DummyAgent:
        def analyze(self, agent_context):
            assert agent_context.changed_files[0].path == "packages/fxp-ai/src/index.ts"
            return CiResponsibilityNotice.model_validate(high_confidence_payload(context))

    monkeypatch.setattr("ci_owner_agent.orchestrator.create_responsibility_agent", lambda *args, **kwargs: DummyAgent())
    build_info = BuildInfo(job=context.job, buildNumber=5088, result="FAILURE", buildUrl="local://job/5088", branch=context.branch, commit="current")

    analyze_failed_build(
        sample_repo["repo"],
        build_info,
        "last-success",
        "current",
        SigSummaryProvider("sig-timeout"),
        git_client,
        allow_sync_failure=True,
        settings=settings,
        last_successful_build_number=5068,
        history_store=store,
    )

    assert git_client.commit_ranges == [("previous", "current")]
    assert git_client.diff_ranges == [("previous", "current")]


def test_analyze_local_accepts_previous_commit(monkeypatch, repo_cache, sample_repo, logs):
    context = make_lc_context(repo_cache, sample_repo, logs)
    settings = replace(context.settings, history_enabled=False)
    git_client = RecordingGitClient()

    class DummyAgent:
        def analyze(self, agent_context):
            return CiResponsibilityNotice.model_validate(high_confidence_payload(context))

    monkeypatch.setattr("ci_owner_agent.orchestrator.create_responsibility_agent", lambda *args, **kwargs: DummyAgent())

    analyze_local(
        repo=sample_repo["repo"],
        job=context.job,
        build=5088,
        branch=context.branch,
        base_commit="last-success",
        head_commit="current",
        console_file=str(logs["auth_failed"]),
        build_url="local://job/5088",
        git_client=git_client,
        settings=settings,
        ignore_checkout_commit_mismatch=True,
        last_successful_build_number=5068,
        previous_build_number=5087,
        previous_commit="previous",
    )

    assert git_client.commit_ranges == [("previous", "current")]
    assert git_client.diff_ranges == [("previous", "current")]


def test_no_owner_decision_short_circuits_agent(monkeypatch, repo_cache, sample_repo, logs):
    context = make_lc_context(repo_cache, sample_repo, logs)
    settings = replace(context.settings, history_enabled=True, history_inherit_no_owner_enabled=True)
    store = make_store()
    _save_historical_no_owner(store, context, signature_key="sig-timeout", build=5088)

    def fail_agent(*args, **kwargs):
        raise AssertionError("agent should be skipped by historical no-owner decision")

    monkeypatch.setattr("ci_owner_agent.orchestrator.create_responsibility_agent", fail_agent)
    build_info = BuildInfo(job=context.job, buildNumber=5089, result="FAILURE", buildUrl="local://job/5089", branch=context.branch, commit=context.head_commit)

    notice = analyze_failed_build(
        sample_repo["repo"],
        build_info,
        context.base_commit,
        context.head_commit,
        SigSummaryProvider("sig-timeout"),
        GitClient(repo_cache),
        allow_sync_failure=True,
        settings=settings,
        last_successful_build_number=5087,
        history_store=store,
    )

    assert notice.owner.type == "no_high_confidence_owner"
    assert notice.responsibilityItems[0].responsibilityType == "no_high_confidence_owner"
    assert notice.repo == sample_repo["repo"]
    assert notice.responsibilityItems[0].testFilePath == "modules/automation/tests/venv.ts"
    assert "历史构建 #5088" in notice.failureReason


def test_inherited_owner_has_priority_over_no_owner_decision():
    decision = _select_build_level_no_owner_decision(
        {
            "currentChunks": [
                {
                    "inheritedOwner": {"found": True, "ownerName": "Tang"},
                    "noOwnerDecision": {"found": True, "sourceBuildNumber": 5088},
                }
            ]
        },
        None,
    )

    assert decision is None


def test_single_no_owner_decision_does_not_short_circuit_multi_failure_build():
    decision = _select_build_level_no_owner_decision(
        {
            "currentChunks": [
                {"inheritedOwner": {"found": False}, "noOwnerDecision": {"found": True, "sourceBuildNumber": 5088}},
                {"inheritedOwner": {"found": False}, "noOwnerDecision": {"found": False}},
            ]
        },
        None,
    )

    assert decision is None


def test_all_chunks_no_owner_decision_short_circuits_agent(monkeypatch, repo_cache, sample_repo, logs):
    context = make_lc_context(repo_cache, sample_repo, logs)
    settings = replace(context.settings, history_enabled=True, history_inherit_no_owner_enabled=True)
    store = make_store()
    _save_historical_no_owner_chunks(store, context, signature_keys=["sig-timeout-a", "sig-timeout-b"], build=5088)

    def fail_agent(*args, **kwargs):
        raise AssertionError("agent should be skipped when all failures are historical no-owner")

    monkeypatch.setattr("ci_owner_agent.orchestrator.create_responsibility_agent", fail_agent)
    build_info = BuildInfo(job=context.job, buildNumber=5089, result="FAILURE", buildUrl="local://job/5089", branch=context.branch, commit=context.head_commit)

    notice = analyze_failed_build(
        sample_repo["repo"],
        build_info,
        context.base_commit,
        context.head_commit,
        MultiSigSummaryProvider(["sig-timeout-a", "sig-timeout-b"]),
        GitClient(repo_cache),
        allow_sync_failure=True,
        settings=settings,
        last_successful_build_number=5087,
        history_store=store,
    )

    assert notice.owner.type == "no_high_confidence_owner"
    assert len(notice.responsibilityItems) == 2
    assert [item.failureSignature for item in notice.responsibilityItems] == ["sig-timeout-a", "sig-timeout-b"]
    assert all(item.sourceBuildNumber == 5088 for item in notice.responsibilityItems)
    assert all(item.sourceBuildUrl == "local://job/5088" for item in notice.responsibilityItems)
    assert all(item.matchType == "signature_exact" for item in notice.responsibilityItems)
    assert all(item.relationship == "very_likely_same_failure" for item in notice.responsibilityItems)
    assert all(item.reason for item in notice.responsibilityItems)
    assert "所有当前失败项" in notice.failureReason
    assert len(notice.evidence) == 3
    assert [item.evidenceIds for item in notice.responsibilityItems] == [
        ["E_HISTORY_NO_OWNER_1"],
        ["E_HISTORY_NO_OWNER_2"],
    ]
    assert "sig-timeout-a" in notice.evidence[1].detail
    assert "sig-timeout-b" not in notice.evidence[1].detail
    assert "sig-timeout-b" in notice.evidence[2].detail
    assert "sig-timeout-a" not in notice.evidence[2].detail
    restored = CiResponsibilityNotice.model_validate_json(notice.model_dump_json())
    assert [item.evidenceIds for item in restored.responsibilityItems] == [
        ["E_HISTORY_NO_OWNER_1"],
        ["E_HISTORY_NO_OWNER_2"],
    ]


def test_multi_failure_no_owner_short_circuit_routes_each_test_maintainer(monkeypatch, repo_cache, sample_repo, logs, tmp_path):
    context = make_lc_context(repo_cache, sample_repo, logs)
    settings = replace(context.settings, history_enabled=True, history_inherit_no_owner_enabled=True)
    store = make_store()
    signatures = ["sig-view", "sig-quota"]
    _save_historical_no_owner_chunks(store, context, signature_keys=signatures, build=5088)
    monkeypatch.setattr(
        "ci_owner_agent.orchestrator.create_responsibility_agent",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("agent should be skipped")),
    )
    provider = MultiSigSummaryProvider(signatures, ["test/view/ViewTest.ts", "test/quota/QuotaTest.ts"])
    build_info = BuildInfo(job=context.job, buildNumber=5089, result="FAILURE", buildUrl="local://job/5089", branch=context.branch, commit=context.head_commit)

    notice = analyze_failed_build(
        sample_repo["repo"],
        build_info,
        context.base_commit,
        context.head_commit,
        provider,
        GitClient(repo_cache),
        allow_sync_failure=True,
        settings=settings,
        last_successful_build_number=5087,
        history_store=store,
    )
    mapping = tmp_path / "maintainers.yml"
    mapping.write_text(
        "version: 1\nrules:\n"
        "  - paths: [\"test/view/**\"]\n"
        "    maintainers: [{name: Charlie, wecomUserId: charlie}]\n"
        "  - paths: [\"test/quota/**\"]\n"
        "    maintainers: [{name: Mars, wecomUserId: mars}]\n",
        encoding="utf-8",
    )
    markdown = format_wecom_markdown_notice(notice, maintainer_resolver=TestMaintainerResolver.from_yaml(mapping))

    assert len(notice.responsibilityItems) == 2
    assert [item.testFilePath for item in notice.responsibilityItems] == ["test/view/ViewTest.ts", "test/quota/QuotaTest.ts"]
    assert "**待确认维护人**：<@charlie>、<@mars>" in markdown
    assert "📁 测试文件：test/view/ViewTest.ts" in markdown
    assert "📣 待确认维护人：<@charlie>" in markdown
    assert "📁 测试文件：test/quota/QuotaTest.ts" in markdown
    assert "📣 待确认维护人：<@mars>" in markdown


def test_ai_no_owner_decisions_build_one_item_per_current_fact():
    decision = _select_build_level_no_owner_decision(
        None,
        {
            "currentFacts": [
                {
                    "signatureKey": "fact-a",
                    "failureKind": "typescript_compile_error",
                    "errorCode": "TS2305",
                    "filePath": "test/a/A.test.ts",
                    "inheritedOwner": {"found": False},
                    "noOwnerDecision": {"found": True, "sourceBuildNumber": 7, "sourceBuildUrl": "local://7", "matchType": "ai_fact_semantic", "relationship": "same_root_cause", "reason": "same A"},
                },
                {
                    "signatureKey": "fact-b",
                    "failureKind": "npm_dependency_resolution_error",
                    "errorCode": "ETARGET",
                    "filePath": "test/b/B.test.ts",
                    "inheritedOwner": {"found": False},
                    "noOwnerDecision": {"found": True, "sourceBuildNumber": 8, "sourceBuildUrl": "local://8", "matchType": "ai_fact_semantic", "relationship": "same_root_cause", "reason": "same B"},
                },
            ]
        },
    )
    build_info = BuildInfo(job="job", buildNumber=9, result="FAILURE", buildUrl="local://9", branch="dev", commit="head")
    notice = _build_no_owner_notice_from_history_decision(
        build_info=build_info,
        base_commit="base",
        head_commit="head",
        decision=decision,
        failure_summaries=None,
        failure_facts=None,
    )

    assert [item.failureSignature for item in notice.responsibilityItems] == ["fact-a", "fact-b"]
    assert [item.sourceBuildNumber for item in notice.responsibilityItems] == [7, 8]
    assert [item.reason for item in notice.responsibilityItems] == ["same A", "same B"]
    assert [item.evidenceIds for item in notice.responsibilityItems] == [
        ["E_HISTORY_NO_OWNER_1"],
        ["E_HISTORY_NO_OWNER_2"],
    ]
    assert "sourceBuildNumber=7" in notice.evidence[1].detail
    assert "sourceBuildNumber=8" not in notice.evidence[1].detail
    assert "sourceBuildNumber=8" in notice.evidence[2].detail
    assert "sourceBuildNumber=7" not in notice.evidence[2].detail


def test_ai_etarget_history_no_owner_short_circuits_without_agent(monkeypatch, repo_cache, sample_repo, logs):
    context = make_lc_context(repo_cache, sample_repo, logs)
    settings = replace(context.settings, history_inherit_no_owner_enabled=True)
    fact = FailureFact.model_validate({
        "signatureKey": "npm|ETARGET|@scope/pkg",
        "historyEligible": True,
        "isGenericWrapper": False,
        "failureKind": "npm_dependency_resolution_error",
        "errorCode": "ETARGET",
        "errorType": "NpmDependencyResolutionError",
        "packageName": "@scope/pkg",
        "filePath": "package.json",
        "message": "No matching version found",
        "rootCauseSummary": "dependency version does not exist",
        "confidence": 0.95,
    }).model_dump(mode="json")

    def precomputed(runtime, history_store=None):
        return replace(
            runtime,
            failure_summaries={"chunks": []},
            failure_facts={"ok": True, "facts": [fact]},
            history_precheck={"currentChunks": []},
            ai_history_precheck={
                "currentFacts": [
                    {
                        **fact,
                        "inheritedOwner": {"found": False},
                        "noOwnerDecision": {
                            "found": True,
                            "sourceBuildNumber": 7,
                            "sourceBuildUrl": "local://job/7",
                            "matchType": "ai_fact_semantic",
                            "relationship": "same_root_cause",
                            "reason": "historical ETARGET was no-owner",
                            "signature": {
                                "signatureKey": fact["signatureKey"],
                                "errorCode": "ETARGET",
                                "errorType": "NpmDependencyResolutionError",
                                "failureKind": "npm_dependency_resolution_error",
                            },
                        },
                    }
                ]
            },
        )

    monkeypatch.setattr("ci_owner_agent.orchestrator._with_precomputed_failure_context", precomputed)
    monkeypatch.setattr(
        "ci_owner_agent.orchestrator.create_responsibility_agent",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("agent should be skipped")),
    )
    notice = analyze_failed_build(
        sample_repo["repo"],
        BuildInfo(job=context.job, buildNumber=8, result="FAILURE", buildUrl="local://job/8", branch=context.branch),
        context.base_commit,
        context.head_commit,
        context.log_provider,
        GitClient(repo_cache),
        allow_sync_failure=True,
        settings=settings,
        last_successful_build_number=6,
    )

    assert notice.responsibilityItems[0].failureSignature == fact["signatureKey"]
    assert notice.responsibilityItems[0].sourceBuildNumber == 7
    assert notice.responsibilityItems[0].matchType == "ai_fact_semantic"


def test_invalid_history_decision_falls_back_to_agent_without_history_evidence(monkeypatch, repo_cache, sample_repo, logs):
    context = make_lc_context(repo_cache, sample_repo, logs)
    settings = replace(context.settings, history_inherit_no_owner_enabled=True)
    calls = {"agent": 0}

    def precomputed(runtime, history_store=None):
        summary = SigSummaryProvider("sig-timeout").find_test_failure_summaries()
        return replace(
            runtime,
            failure_summaries=summary,
            history_precheck={
                "currentChunks": [
                    {
                        "signature": summary["chunks"][0]["signature"],
                        "inheritedOwner": {"found": False},
                        "noOwnerDecision": {
                            "found": True,
                            "sourceBuildNumber": runtime.build_number,
                            "sourceBuildUrl": runtime.build_url,
                            "matchType": "signature_exact",
                            "relationship": "very_likely_same_failure",
                            "reason": "invalid current-build source",
                            "signature": summary["chunks"][0]["signature"],
                        },
                    }
                ]
            },
        )

    class DummyAgent:
        def analyze(self, agent_context):
            calls["agent"] += 1
            return CiResponsibilityNotice.model_validate(high_confidence_payload(context))

    monkeypatch.setattr("ci_owner_agent.orchestrator._with_precomputed_failure_context", precomputed)
    monkeypatch.setattr("ci_owner_agent.orchestrator.create_responsibility_agent", lambda *args, **kwargs: DummyAgent())
    notice = analyze_failed_build(
        sample_repo["repo"],
        BuildInfo(job=context.job, buildNumber=5089, result="FAILURE", buildUrl="local://job/5089", branch=context.branch),
        context.base_commit,
        context.head_commit,
        context.log_provider,
        GitClient(repo_cache),
        allow_sync_failure=True,
        settings=settings,
        last_successful_build_number=5087,
    )

    assert calls["agent"] == 1
    assert all(item.source != "history_no_owner_decision" for item in notice.evidence)


def test_trusted_history_no_owner_rejects_current_or_future_source_and_unknown_match_type():
    build_info = BuildInfo(job="job", buildNumber=9, result="FAILURE", buildUrl="local://9", branch="dev")
    base_decision = {
        "found": True,
        "failureSignature": "sig-a",
        "failureTitle": "failure A",
        "sourceBuildNumber": 7,
        "sourceBuildUrl": "local://7",
        "matchType": "signature_exact",
        "relationship": "very_likely_same_failure",
        "reason": "same failure",
    }
    notice = _build_no_owner_notice_from_history_decision(
        build_info=build_info,
        base_commit="base",
        head_commit="head",
        decision=base_decision,
        failure_summaries=None,
        failure_facts=None,
    )
    item = notice.responsibilityItems[0]
    assert item.sourceBuildNumber == 7

    for invalid in (
        {**base_decision, "sourceBuildNumber": 9},
        {**base_decision, "sourceBuildNumber": 10},
        {**base_decision, "matchType": "unknown_match"},
    ):
        clean = CiResponsibilityNotice.model_validate(notice.model_dump())
        apply_history_no_owner_sources(clean, [invalid])
        invalid_item = clean.responsibilityItems[0]
        assert invalid_item.sourceBuildNumber is None
        assert invalid_item.sourceBuildUrl is None
        assert invalid_item.matchType is None
        assert invalid_item.relationship is None


def test_trusted_history_no_owner_allows_missing_source_url_without_none_evidence():
    decision = {
        "found": True,
        "failureSignature": "sig-a",
        "failureTitle": "failure A",
        "sourceBuildNumber": 7,
        "sourceBuildUrl": None,
        "matchType": "signature_exact",
        "relationship": "very_likely_same_failure",
        "reason": "same failure",
    }
    notice = _build_no_owner_notice_from_history_decision(
        build_info=BuildInfo(job="job", buildNumber=9, result="FAILURE", buildUrl="local://9", branch="dev"),
        base_commit="base",
        head_commit="head",
        decision=decision,
        failure_summaries=None,
        failure_facts=None,
    )

    item = notice.responsibilityItems[0]
    assert item.sourceBuildNumber == 7
    assert item.sourceBuildUrl is None
    assert "sourceBuildUrl=None" not in notice.evidence[1].detail
    assert "feedbackAction=None" not in notice.evidence[1].detail


@pytest.mark.parametrize(
    "overrides",
    [
        {"sourceBuildNumber": 9},
        {"sourceBuildNumber": 10},
        {"matchType": "unknown"},
        {"relationship": None},
        {"failureSignature": None},
    ],
)
def test_invalid_history_no_owner_decision_is_rejected_before_notice(overrides):
    decision = {
        "found": True,
        "failureSignature": "sig-a",
        "failureTitle": "failure A",
        "sourceBuildNumber": 7,
        "sourceBuildUrl": None,
        "matchType": "signature_exact",
        "relationship": "very_likely_same_failure",
        "reason": "same failure",
        **overrides,
    }

    assert _validate_trusted_history_no_owner_decisions(decision, current_build_number=9) is None


def test_multi_failure_with_one_invalid_history_decision_is_fully_rejected():
    decision = {
        "allFailuresNoOwnerDecision": True,
        "decisions": [
            {
                "failureSignature": "sig-a",
                "sourceBuildNumber": 7,
                "matchType": "signature_exact",
                "relationship": "very_likely_same_failure",
                "reason": "same A",
            },
            {
                "failureSignature": "sig-b",
                "sourceBuildNumber": 8,
                "matchType": "unknown",
                "relationship": "very_likely_same_failure",
                "reason": "same B",
            },
        ],
    }

    assert _validate_trusted_history_no_owner_decisions(decision, current_build_number=9) is None


def test_no_owner_decision_disabled_falls_back_to_agent(monkeypatch, repo_cache, sample_repo, logs):
    context = make_lc_context(repo_cache, sample_repo, logs)
    settings = replace(context.settings, history_enabled=True, history_inherit_no_owner_enabled=False)
    store = make_store()
    _save_historical_no_owner(store, context, signature_key="sig-timeout", build=5088)
    calls = {"agent": 0}

    class DummyAgent:
        def analyze(self, agent_context):
            calls["agent"] += 1
            return CiResponsibilityNotice.model_validate(high_confidence_payload(context))

    monkeypatch.setattr("ci_owner_agent.orchestrator.create_responsibility_agent", lambda *args, **kwargs: DummyAgent())
    build_info = BuildInfo(job=context.job, buildNumber=5089, result="FAILURE", buildUrl="local://job/5089", branch=context.branch, commit=context.head_commit)

    analyze_failed_build(
        sample_repo["repo"],
        build_info,
        context.base_commit,
        context.head_commit,
        SigSummaryProvider("sig-timeout"),
        GitClient(repo_cache),
        allow_sync_failure=True,
        settings=settings,
        last_successful_build_number=5087,
        history_store=store,
    )

    assert calls["agent"] == 1


def test_no_owner_decision_reanalyzes_when_new_strong_evidence():
    assert _has_new_strong_evidence(
        decision={"signature": {"signatureKey": "sig-timeout", "errorType": "Timeout", "errorMessage": "run awaitfunc timeout"}},
        failure_summaries={
            "chunks": [
                {
                    "signature": {
                        "signatureKey": "sig-timeout",
                        "errorType": "Timeout",
                        "errorMessage": "run awaitfunc timeout",
                        "testFile": "modules/automation/tests/venv.ts",
                        "topStackFile": "modules/automation/tests/venv.ts",
                    },
                    "signatureHash": "sig-timeout",
                }
            ]
        },
        failure_facts=None,
        changed_files=[ChangedFile(path="modules/automation/tests/venv.ts", status="M")],
    )


def test_no_owner_decision_no_new_strong_evidence_for_same_timeout():
    assert not _has_new_strong_evidence(
        decision={"signature": {"signatureKey": "sig-timeout", "errorType": "Timeout", "errorMessage": "run awaitfunc timeout"}},
        failure_summaries={
            "chunks": [
                {
                    "signature": {
                        "signatureKey": "sig-timeout",
                        "errorType": "Timeout",
                        "errorMessage": "run awaitfunc timeout",
                        "testFile": "modules/automation/tests/venv.ts",
                    },
                    "signatureHash": "sig-timeout",
                }
            ]
        },
        failure_facts=None,
        changed_files=[ChangedFile(path="packages/fxp-ai/src/index.ts", status="M")],
    )


def test_no_owner_decision_detects_changed_path_in_second_summary():
    assert _has_new_strong_evidence(
        decision={
            "allFailuresNoOwnerDecision": True,
            "coveredSignatures": ["sig-a", "sig-b"],
            "signature": {"signatureKey": "sig-a"},
        },
        failure_summaries={
            "chunks": [
                {
                    "signature": {
                        "signatureKey": "sig-a",
                        "errorType": "Timeout",
                        "errorMessage": "run awaitfunc timeout",
                        "testFile": "modules/automation/tests/a.ts",
                        "topStackFile": "modules/automation/tests/a.ts",
                    },
                    "signatureHash": "sig-a",
                },
                {
                    "signature": {
                        "signatureKey": "sig-b",
                        "errorType": "Timeout",
                        "errorMessage": "run awaitfunc timeout",
                        "testFile": "modules/automation/tests/b.ts",
                        "topStackFile": "modules/automation/tests/b.ts",
                    },
                    "signatureHash": "sig-b",
                },
            ]
        },
        failure_facts=None,
        changed_files=[ChangedFile(path="modules/automation/tests/b.ts", status="M")],
    )


def test_no_owner_decision_no_new_strong_evidence_checks_all_summaries():
    assert not _has_new_strong_evidence(
        decision={
            "allFailuresNoOwnerDecision": True,
            "coveredSignatures": ["sig-a", "sig-b"],
            "signature": {"signatureKey": "sig-a"},
            "decisions": [
                {"failureSignature": "sig-a", "signature": {"signatureKey": "sig-a", "errorType": "Timeout"}},
                {"failureSignature": "sig-b", "signature": {"signatureKey": "sig-b", "errorType": "Timeout"}},
            ],
        },
        failure_summaries={
            "chunks": [
                {
                    "signature": {
                        "signatureKey": "sig-a",
                        "errorType": "Timeout",
                        "errorMessage": "run awaitfunc timeout",
                        "testFile": "modules/automation/tests/a.ts",
                        "topStackFile": "modules/automation/tests/a.ts",
                    },
                    "signatureHash": "sig-a",
                },
                {
                    "signature": {
                        "signatureKey": "sig-b",
                        "errorType": "Timeout",
                        "errorMessage": "run awaitfunc timeout",
                        "testFile": "modules/automation/tests/b.ts",
                        "topStackFile": "modules/automation/tests/b.ts",
                    },
                    "signatureHash": "sig-b",
                },
            ]
        },
        failure_facts=None,
        changed_files=[ChangedFile(path="packages/fxp-ai/src/index.ts", status="M")],
    )


def test_no_owner_decision_aligns_heterogeneous_failures_without_false_positive():
    decision = {
        "allFailuresNoOwnerDecision": True,
        "decisions": [
            {"failureSignature": "sig-timeout", "signature": {"signatureKey": "sig-timeout", "errorType": "Timeout"}},
            {"failureSignature": "sig-ts2305", "signature": {"signatureKey": "sig-ts2305", "errorCode": "TS2305"}},
        ],
    }
    summaries = {
        "chunks": [
            {"signature": {"signatureKey": "sig-timeout", "errorType": "Timeout", "testFile": "test/a.test.ts"}, "signatureHash": "sig-timeout"},
            {"signature": {"signatureKey": "sig-ts2305", "errorCode": "TS2305", "testFile": "test/b.test.ts"}, "signatureHash": "sig-ts2305"},
        ]
    }

    assert not _has_new_strong_evidence(
        decision=decision,
        failure_summaries=summaries,
        failure_facts=None,
        changed_files=[ChangedFile(path="src/unrelated.ts", status="M")],
    )


def test_no_owner_decision_detects_changed_error_code_in_aligned_second_failure():
    decision = {
        "allFailuresNoOwnerDecision": True,
        "decisions": [
            {"failureSignature": "sig-timeout", "signature": {"signatureKey": "sig-timeout", "errorType": "Timeout"}},
            {"failureSignature": "sig-ts", "signature": {"signatureKey": "sig-ts", "errorCode": "TS2305"}},
        ],
    }
    summaries = {
        "chunks": [
            {"signature": {"signatureKey": "sig-timeout", "errorType": "Timeout"}, "signatureHash": "sig-timeout"},
            {"signature": {"signatureKey": "sig-ts", "errorCode": "TS2307"}, "signatureHash": "sig-ts"},
        ]
    }

    assert _has_new_strong_evidence(
        decision=decision, failure_summaries=summaries, failure_facts=None, changed_files=[]
    )


def test_no_owner_decision_unaligned_failures_reanalyze_conservatively():
    assert _has_new_strong_evidence(
        decision={
            "allFailuresNoOwnerDecision": True,
            "decisions": [
                {"failureSignature": "sig-a", "signature": {"signatureKey": "sig-a", "errorType": "Timeout"}},
                {"failureSignature": "sig-other", "signature": {"signatureKey": "sig-other", "errorType": "Timeout"}},
            ],
        },
        failure_summaries={
            "chunks": [
                {"signature": {"signatureKey": "sig-a", "errorType": "Timeout"}, "signatureHash": "sig-a"},
                {"signature": {"signatureKey": "sig-b", "errorType": "Timeout"}, "signatureHash": "sig-b"},
            ]
        },
        failure_facts=None,
        changed_files=[],
    )


@pytest.mark.parametrize(
    ("error_code", "error_type"),
    [
        ("ETARGET", "NpmDependencyResolutionError"),
        ("ERESOLVE", "NpmDependencyResolutionError"),
        ("ECONNRESET", "NetworkError"),
        (None, "TypeError"),
    ],
)
def test_ai_no_owner_same_structured_marker_is_not_new_evidence(error_code, error_type):
    signature = f"fact-{error_code or error_type}"
    assert not _has_new_strong_evidence(
        decision={
            "failureSignature": signature,
            "signature": {
                "signatureKey": signature,
                "errorCode": error_code,
                "errorType": error_type,
                "failureKind": "ai_failure",
            },
        },
        failure_summaries=None,
        failure_facts={
            "facts": [
                {
                    "signatureKey": signature,
                    "errorCode": error_code,
                    "errorType": error_type,
                    "failureKind": "ai_failure",
                }
            ]
        },
        changed_files=[],
    )


def test_ai_no_owner_different_structured_marker_is_new_evidence():
    assert _has_new_strong_evidence(
        decision={
            "failureSignature": "fact-dependency",
            "signature": {
                "signatureKey": "fact-dependency",
                "errorCode": "ERESOLVE",
                "errorType": "SyntaxError",
                "failureKind": "dependency_error",
            },
        },
        failure_summaries=None,
        failure_facts={
            "facts": [
                {
                    "signatureKey": "fact-dependency",
                    "errorCode": "ETARGET",
                    "errorType": "TypeError",
                    "failureKind": "dependency_error",
                }
            ]
        },
        changed_files=[],
    )


@pytest.mark.parametrize(
    "failure_path",
    [
        "/var/app/test/A.test.ts",
        "./test/A.test.ts:12:3",
        "/var/app/server/service/a.ts",
    ],
)
def test_no_owner_failure_paths_use_repository_normalization(failure_path):
    changed_path = "server/service/a.ts" if "server/" in failure_path else "test/A.test.ts"
    assert _has_new_strong_evidence(
        decision={
            "failureSignature": "sig-a",
            "signature": {"signatureKey": "sig-a", "errorType": "Timeout"},
        },
        failure_summaries={
            "chunks": [
                {
                    "signature": {
                        "signatureKey": "sig-a",
                        "errorType": "Timeout",
                        "testFile": failure_path,
                    },
                    "signatureHash": "sig-a",
                }
            ]
        },
        failure_facts=None,
        changed_files=[ChangedFile(path=changed_path, status="M")],
    )


def test_no_owner_unsupported_windows_absolute_path_does_not_match_changed_file():
    assert not _has_new_strong_evidence(
        decision={
            "failureSignature": "sig-a",
            "signature": {"signatureKey": "sig-a", "errorType": "Timeout"},
        },
        failure_summaries={
            "chunks": [
                {
                    "signature": {
                        "signatureKey": "sig-a",
                        "errorType": "Timeout",
                        "testFile": r"C:\workspace\repo\test\A.test.ts",
                    },
                    "signatureHash": "sig-a",
                }
            ]
        },
        failure_facts=None,
        changed_files=[ChangedFile(path="test/A.test.ts", status="M")],
    )


def test_no_owner_parent_traversal_path_does_not_match_changed_file():
    assert not _has_new_strong_evidence(
        decision={
            "failureSignature": "sig-a",
            "signature": {"signatureKey": "sig-a", "errorType": "Timeout"},
        },
        failure_summaries={
            "chunks": [
                {
                    "signature": {
                        "signatureKey": "sig-a",
                        "errorType": "Timeout",
                        "testFile": "../test/A.test.ts",
                    },
                    "signatureHash": "sig-a",
                }
            ]
        },
        failure_facts=None,
        changed_files=[ChangedFile(path="test/A.test.ts", status="M")],
    )


def test_all_chunks_no_owner_but_second_path_changed_falls_back_to_agent(monkeypatch, repo_cache, sample_repo, logs):
    context = make_lc_context(repo_cache, sample_repo, logs)
    settings = replace(context.settings, history_enabled=True, history_inherit_no_owner_enabled=True)
    store = make_store()
    _save_historical_no_owner_chunks(store, context, signature_keys=["sig-timeout-a", "sig-timeout-b"], build=5088)
    calls = {"agent": 0}

    class ChangedPathGitClient:
        def sync(self, repo):
            return {"ok": True}

        def check_ancestor(self, repo, base_commit, head_commit):
            return {"ok": True, "isAncestor": True}

        def get_commits_between(self, repo, base_commit, head_commit):
            return {"ok": True, "commits": []}

        def get_diff_files(self, repo, base_commit, head_commit):
            return {
                "ok": True,
                "files": [
                    {
                        "path": "modules/automation/tests/sig-timeout-b.ts",
                        "status": "M",
                        "additions": 1,
                        "deletions": 0,
                    }
                ],
            }

    class DummyAgent:
        def analyze(self, agent_context):
            calls["agent"] += 1
            return CiResponsibilityNotice.model_validate(high_confidence_payload(context))

    monkeypatch.setattr("ci_owner_agent.orchestrator.create_responsibility_agent", lambda *args, **kwargs: DummyAgent())
    build_info = BuildInfo(job=context.job, buildNumber=5089, result="FAILURE", buildUrl="local://job/5089", branch=context.branch, commit=context.head_commit)

    analyze_failed_build(
        sample_repo["repo"],
        build_info,
        context.base_commit,
        context.head_commit,
        MultiSigSummaryProvider(
            ["sig-timeout-a", "sig-timeout-b"],
            [
                "modules/automation/tests/sig-timeout-a.ts",
                "/var/app/modules/automation/tests/sig-timeout-b.ts",
            ],
        ),
        ChangedPathGitClient(),
        allow_sync_failure=True,
        settings=settings,
        last_successful_build_number=5087,
        history_store=store,
    )

    assert calls["agent"] == 1


def test_no_owner_decision_detects_new_strong_error_code():
    assert _has_new_strong_evidence(
        decision={"signature": {"signatureKey": "sig-timeout", "errorType": "Timeout", "errorMessage": "run awaitfunc timeout"}},
        failure_summaries={
            "chunks": [
                {
                    "signature": {
                        "signatureKey": "sig-timeout",
                        "errorType": "TypeScriptCompileError",
                        "errorMessage": "error TS2305: missing export",
                    },
                    "signatureHash": "sig-timeout",
                }
            ]
        },
        failure_facts=None,
        changed_files=[],
    )


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


def test_save_history_does_not_clear_existing_failure_facts_when_extraction_failed(monkeypatch, repo_cache, sample_repo, logs):
    context = make_lc_context(repo_cache, sample_repo, logs)
    settings = replace(context.settings, history_enabled=True, ai_failure_facts_enabled=True)
    store = make_store()
    fact = FailureFact(
        signatureKey="typescript_compile_error|TS2305|src/index.ts|classifyErrorMessage",
        historyEligible=True,
        failureKind="typescript_compile_error",
        errorCode="TS2305",
        message="missing export",
        rootCauseSummary="missing export",
        confidence=0.9,
    )
    build_info, notice = _save_existing_failure_fact(store, context, fact)
    assert len(store.failure_facts.docs) == 1
    monkeypatch.setattr("ci_owner_agent.orchestrator.get_history_store", lambda settings: store)

    _save_history(
        settings,
        build_info,
        notice,
        NoSummaryProvider(),
        context.base_commit,
        context.head_commit,
        6,
        failure_summaries={"chunks": []},
        failure_facts={"ok": False, "facts": [], "warning": "boom"},
    )

    assert len(store.failure_facts.docs) == 1
    assert store.failure_facts.docs[0]["signatureKey"] == fact.signatureKey
    assert any("failure facts not saved: boom" in warning for warning in build_info.warnings)


def test_save_history_clears_existing_failure_facts_when_extraction_succeeds_with_no_facts(monkeypatch, repo_cache, sample_repo, logs):
    context = make_lc_context(repo_cache, sample_repo, logs)
    settings = replace(context.settings, history_enabled=True, ai_failure_facts_enabled=True)
    store = make_store()
    fact = FailureFact(
        signatureKey="typescript_compile_error|TS2305|src/index.ts|classifyErrorMessage",
        historyEligible=True,
        failureKind="typescript_compile_error",
        errorCode="TS2305",
        message="missing export",
        rootCauseSummary="missing export",
        confidence=0.9,
    )
    build_info, notice = _save_existing_failure_fact(store, context, fact)
    assert len(store.failure_facts.docs) == 1
    monkeypatch.setattr("ci_owner_agent.orchestrator.get_history_store", lambda settings: store)

    _save_history(
        settings,
        build_info,
        notice,
        NoSummaryProvider(),
        context.base_commit,
        context.head_commit,
        6,
        failure_summaries={"chunks": []},
        failure_facts={"ok": True, "facts": []},
    )

    assert store.failure_facts.docs == []
