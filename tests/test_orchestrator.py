from __future__ import annotations

from dataclasses import replace

from ci_owner_agent.orchestrator import (
    _has_new_strong_evidence,
    _save_history,
    _select_build_level_no_owner_decision,
    _with_precomputed_failure_context,
    analyze_failed_build,
    analyze_local,
)
from ci_owner_agent.schemas import BuildInfo, ChangedFile, CiResponsibilityNotice, FailureFact, FailureFactExtractionResult
from ci_owner_agent.services.git_client import GitClient
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
    def __init__(self, signatures: list[str]):
        self.signatures = signatures

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
                        "testFile": f"modules/automation/tests/{signature_key}.ts",
                        "topStackFile": f"modules/automation/tests/{signature_key}.ts",
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
    assert "所有当前失败项" in notice.failureReason
    assert "coveredFailureCount=2" in notice.evidence[0].detail


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


def test_all_chunks_no_owner_but_second_path_changed_falls_back_to_agent(monkeypatch, repo_cache, sample_repo, logs):
    context = make_lc_context(repo_cache, sample_repo, logs)
    settings = replace(context.settings, history_enabled=True, history_inherit_no_owner_enabled=True)
    store = make_store()
    _save_historical_no_owner_chunks(store, context, signature_keys=["sig-timeout-a", "sig-timeout-b"], build=5088)
    calls = {"agent": 0}

    class ChangedPathGitClient:
        def sync(self, repo):
            return {"ok": True}

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
        MultiSigSummaryProvider(["sig-timeout-a", "sig-timeout-b"]),
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
