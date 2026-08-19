from __future__ import annotations

from dataclasses import replace

from ci_owner_agent.schemas import BuildInfo, CiResponsibilityNotice, FailureFact, FailureFactComparison
from ci_owner_agent.services.feedback_store import FeedbackStore
from ci_owner_agent.services.ai_history_search import history_search_similar_failure_facts
from tests.test_failure_fact_compare_ai import make_fact
from tests.test_history_store import current_owner_item, high_confidence_payload, make_store
from tests.test_langchain_agent import make_lc_context


def _settings(context, **overrides):
    base = replace(
        context.settings,
        history_enabled=True,
        ai_history_compare_enabled=True,
        ai_failure_fact_min_confidence=0.7,
        ai_history_compare_threshold=0.9,
        ai_history_max_fact_candidates=20,
        ai_history_max_compare_calls=20,
    )
    return replace(base, **overrides)


def _context(context, *, build: int = 8, facts: list[FailureFact] | None = None, settings=None):
    return replace(
        context,
        build_number=build,
        build_url=f"local://services/fx-code-unittest/{build}",
        settings=settings or _settings(context),
        last_successful_build_number=6,
        failure_facts={"ok": True, "facts": [fact.model_dump(mode="json") for fact in facts or []]},
    )


def _notice_for_fact(context, *, build: int, fact: FailureFact, owner_name: str = "test", owner_email: str = "test@test.com", no_owner: bool = False):
    payload = high_confidence_payload(context)
    payload["buildNumber"] = build
    payload["buildUrl"] = f"local://services/fx-code-unittest/{build}"
    payload["headCommit"] = f"commit-{build}"
    if no_owner:
        payload["owner"] = {
            "type": "no_high_confidence_owner",
            "name": "无高可信责任人",
            "email": None,
            "commit": None,
            "confidence": 0,
        }
        payload["hasHighConfidenceOwner"] = False
        payload["responsibilityItems"] = []
    else:
        payload["owner"] = {
            "type": "high_confidence",
            "name": owner_name,
            "email": owner_email,
            "commit": f"commit-{build}",
            "confidence": 0.9,
        }
        payload["responsibilityItems"] = [
            current_owner_item(
                failure_id=f"failure-{build}",
                failure_title=fact.failureKind,
                failure_signature=fact.signatureKey,
                owner_name=owner_name,
                owner_email=owner_email,
                owner_commit=f"commit-{build}",
            )
        ]
    return CiResponsibilityNotice.model_validate(payload)


def _save_fact(store, context, *, build: int = 7, fact: FailureFact | None = None, owner_name: str = "test", owner_email: str = "test@test.com", no_owner: bool = False):
    fact = fact or make_fact()
    notice = _notice_for_fact(context, build=build, fact=fact, owner_name=owner_name, owner_email=owner_email, no_owner=no_owner)
    build_info = BuildInfo(
        job=context.job,
        buildNumber=build,
        result="FAILURE",
        buildUrl=f"local://services/fx-code-unittest/{build}",
        branch=context.branch,
        commit=f"commit-{build}",
    )
    store.save_analysis(build_info, notice, context.base_commit, f"commit-{build}", 6, context.base_commit, [])
    store.save_failure_facts(build_info=build_info, notice=notice, facts=[fact])
    return notice


def _same(confidence: float = 0.95):
    return FailureFactComparison(
        sameFailure=True,
        confidence=confidence,
        relationship="same_root_cause",
        samePoints=["same root cause"],
        differentPoints=[],
        reason="same root cause",
    )


def _different():
    return FailureFactComparison(
        sameFailure=False,
        confidence=0.96,
        relationship="different_root_cause",
        samePoints=[],
        differentPoints=["different inner root cause"],
        reason="different root cause",
    )


def test_ai_history_disabled_when_history_disabled(repo_cache, sample_repo, logs):
    context = make_lc_context(repo_cache, sample_repo, logs)
    result = history_search_similar_failure_facts(
        _context(context, settings=replace(_settings(context), history_enabled=False)),
        store=make_store(),
    )
    assert result["ok"] is False
    assert result["historyEnabled"] is False
    assert result["candidates"] == []


def test_ai_history_compare_disabled(repo_cache, sample_repo, logs):
    context = make_lc_context(repo_cache, sample_repo, logs)
    result = history_search_similar_failure_facts(
        _context(context, settings=replace(_settings(context), ai_history_compare_enabled=False)),
        store=make_store(),
    )
    assert result["ok"] is False
    assert "disabled" in result["warning"]


def test_ai_history_no_current_facts(repo_cache, sample_repo, logs):
    context = make_lc_context(repo_cache, sample_repo, logs)
    result = history_search_similar_failure_facts(_context(context, facts=[]), store=make_store())
    assert result["ok"] is True
    assert result["currentFacts"] == []
    assert result["candidates"] == []


def test_ai_history_diagnostics_when_historical_facts_missing(repo_cache, sample_repo, logs):
    context = make_lc_context(repo_cache, sample_repo, logs)
    result = history_search_similar_failure_facts(_context(context, facts=[make_fact()]), store=make_store())
    diagnostics = result["diagnostics"]
    assert diagnostics["eligibleCurrentFactsCount"] == 1
    assert diagnostics["historicalFactsCount"] == 0
    assert diagnostics["rankedPairsCount"] == 0
    assert diagnostics["comparedPairsCount"] == 0
    assert diagnostics["acceptedCandidatesCount"] == 0


def test_ai_history_filters_current_generic_fact(monkeypatch, repo_cache, sample_repo, logs):
    calls = []
    monkeypatch.setattr("ci_owner_agent.services.ai_history_search.compare_failure_facts_with_ai", lambda **kwargs: calls.append(kwargs))
    context = make_lc_context(repo_cache, sample_repo, logs)
    generic = make_fact(historyEligible=False, isGenericWrapper=True)
    result = history_search_similar_failure_facts(_context(context, facts=[generic]), store=make_store())
    assert calls == []
    assert result["currentFacts"][0]["inheritedOwner"]["found"] is False
    assert result["currentFacts"][0]["blockedReason"] == "blocked_by_generic_wrapper"
    assert result["candidates"] == []


def test_unknown_failure_does_not_enter_ai_history(monkeypatch, repo_cache, sample_repo, logs):
    def unexpected_compare(**kwargs):
        raise AssertionError("unknown_failure must not invoke AI comparison")

    monkeypatch.setattr("ci_owner_agent.services.ai_history_search.compare_failure_facts_with_ai", unexpected_compare)
    context = make_lc_context(repo_cache, sample_repo, logs)
    store = make_store()
    _save_fact(store, context, build=7, fact=make_fact())
    unknown = FailureFact(
        signatureKey="model-only-shell",
        historyEligible=True,
        isGenericWrapper=False,
        failureKind="",
        message="",
        rootCauseSummary="",
        confidence=0.99,
    )

    result = history_search_similar_failure_facts(_context(context, facts=[unknown]), store=store)

    current = result["currentFacts"][0]
    diagnostics = result["diagnostics"]
    assert current["blockedReason"] == "blocked_by_generic_wrapper"
    assert current["inheritedOwner"]["found"] is False
    assert current["noOwnerDecision"]["found"] is False
    assert diagnostics["eligibleCurrentFactsCount"] == 0
    assert diagnostics["rankedPairsCount"] == 0
    assert diagnostics["comparedPairsCount"] == 0
    assert result["candidates"] == []


def test_buildkit_wrapper_does_not_enter_ai_history(monkeypatch, repo_cache, sample_repo, logs):
    def unexpected_compare(**kwargs):
        raise AssertionError("generic BuildKit wrapper must not invoke AI comparison")

    monkeypatch.setattr("ci_owner_agent.services.ai_history_search.compare_failure_facts_with_ai", unexpected_compare)
    context = make_lc_context(repo_cache, sample_repo, logs)
    wrapper = FailureFact(
        signatureKey="model-wrapper",
        historyEligible=True,
        isGenericWrapper=False,
        failureKind="build_failure",
        errorType="BuildError",
        filePath="/var/lib/jenkins/workspace/services/fx-code-unittest/server/a.ts",
        message='ERROR: process "/bin/sh -c npm run build" did not complete successfully: exit code: 1',
        rootCauseSummary="command failed",
        confidence=0.99,
    )

    result = history_search_similar_failure_facts(_context(context, facts=[wrapper]), store=make_store())

    assert result["currentFacts"][0]["blockedReason"] == "blocked_by_generic_wrapper"
    assert wrapper.filePath is None
    assert result["diagnostics"]["eligibleCurrentFactsCount"] == 0
    assert result["diagnostics"]["rankedPairsCount"] == 0
    assert result["diagnostics"]["comparedPairsCount"] == 0
    assert result["candidates"] == []


def test_ai_history_compares_relative_and_var_app_paths(monkeypatch, repo_cache, sample_repo, logs):
    monkeypatch.setattr("ci_owner_agent.services.ai_history_search.compare_failure_facts_with_ai", lambda **kwargs: _same())
    context = make_lc_context(repo_cache, sample_repo, logs)
    store = make_store()
    historical = make_fact(filePath="/var/app/server/workflow/service.ts")
    current = make_fact(filePath="server/workflow/service.ts")
    _save_fact(store, context, build=7, fact=historical)

    result = history_search_similar_failure_facts(_context(context, facts=[current], build=8), store=store)

    assert current.filePath == historical.filePath == "server/workflow/service.ts"
    assert current.signatureKey == historical.signatureKey
    assert current.factId == historical.factId
    assert result["diagnostics"]["comparedPairsCount"] > 0
    assert result["candidates"]
def test_ai_history_ts2305_vs_etarget_not_inherited(monkeypatch, repo_cache, sample_repo, logs):
    monkeypatch.setattr("ci_owner_agent.services.ai_history_search.compare_failure_facts_with_ai", lambda **kwargs: _different())
    context = make_lc_context(repo_cache, sample_repo, logs)
    store = make_store()
    _save_fact(store, context, build=7, fact=make_fact())
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
    result = history_search_similar_failure_facts(_context(context, facts=[etarget]), store=store)
    assert result["candidates"] == []
    assert result["currentFacts"][0]["inheritedOwner"]["found"] is False
    assert result["diagnostics"]["comparedPairsCount"] > 0
    assert result["diagnostics"]["skipped"]["compareNotSameFailure"] > 0
    assert result["diagnostics"]["compareResults"][0]["skipReason"] == "compare_not_same_failure"


def test_ai_history_same_ts2305_compare_false_has_diagnostics(monkeypatch, repo_cache, sample_repo, logs):
    monkeypatch.setattr("ci_owner_agent.services.ai_history_search.compare_failure_facts_with_ai", lambda **kwargs: _different())
    context = make_lc_context(repo_cache, sample_repo, logs)
    store = make_store()
    fact = make_fact()
    _save_fact(store, context, build=7, fact=fact)
    result = history_search_similar_failure_facts(_context(context, facts=[fact], build=13), store=store)
    assert result["candidates"] == []
    assert result["currentFacts"][0]["inheritedOwner"]["found"] is False
    assert result["diagnostics"]["comparedPairsCount"] > 0
    assert result["diagnostics"]["skipped"]["compareNotSameFailure"] > 0
    compare_result = result["diagnostics"]["compareResults"][0]
    assert compare_result["currentSignatureKey"] == fact.signatureKey
    assert compare_result["historicalSignatureKey"] == fact.signatureKey
    assert compare_result["accepted"] is False


def test_ai_history_same_ts2305_inherits_owner(monkeypatch, repo_cache, sample_repo, logs):
    monkeypatch.setattr("ci_owner_agent.services.ai_history_search.compare_failure_facts_with_ai", lambda **kwargs: _same())
    context = make_lc_context(repo_cache, sample_repo, logs)
    store = make_store()
    fact = make_fact()
    _save_fact(store, context, build=7, fact=fact, owner_name="test")
    result = history_search_similar_failure_facts(_context(context, facts=[fact], build=9), store=store)
    assert len(result["candidates"]) == 1
    inherited = result["currentFacts"][0]["inheritedOwner"]
    assert inherited["found"] is True
    assert inherited["sourceBuildNumber"] == 7
    assert inherited["ownerName"] == "test"
    assert inherited["matchType"] == "ai_fact_semantic"
    assert inherited["relationship"] == "same_root_cause"
    assert result["diagnostics"]["acceptedCandidatesCount"] == 1


def test_ai_history_compare_below_threshold_not_inherited(monkeypatch, repo_cache, sample_repo, logs):
    monkeypatch.setattr("ci_owner_agent.services.ai_history_search.compare_failure_facts_with_ai", lambda **kwargs: _same(0.7))
    context = make_lc_context(repo_cache, sample_repo, logs)
    store = make_store()
    fact = make_fact()
    _save_fact(store, context, build=7, fact=fact)
    result = history_search_similar_failure_facts(_context(context, facts=[fact]), store=store)
    assert result["candidates"] == []
    assert result["currentFacts"][0]["inheritedOwner"]["found"] is False


def test_ai_history_historical_fact_without_owner_not_inherited(monkeypatch, repo_cache, sample_repo, logs):
    monkeypatch.setattr("ci_owner_agent.services.ai_history_search.compare_failure_facts_with_ai", lambda **kwargs: _same())
    context = make_lc_context(repo_cache, sample_repo, logs)
    store = make_store()
    fact = make_fact()
    _save_fact(store, context, build=7, fact=fact, no_owner=True)
    result = history_search_similar_failure_facts(_context(context, facts=[fact]), store=store)
    assert result["candidates"] == []
    assert result["currentFacts"][0]["inheritedOwner"]["found"] is False
    signature = result["currentFacts"][0]["noOwnerDecision"]["signature"]
    assert signature["errorCode"] == fact.errorCode
    assert signature.get("errorType") == fact.errorType
    assert signature["failureKind"] == fact.failureKind
    assert signature["filePath"] == fact.filePath


def test_ai_history_feedback_mark_flaky_blocks_inheritance(monkeypatch, repo_cache, sample_repo, logs):
    monkeypatch.setattr("ci_owner_agent.services.ai_history_search.compare_failure_facts_with_ai", lambda **kwargs: _same())
    context = make_lc_context(repo_cache, sample_repo, logs)
    store = make_store()
    fact = make_fact()
    _save_fact(store, context, build=7, fact=fact)
    FeedbackStore(store).apply_feedback(repo=context.repo, job=context.job, branch=context.branch, build_number=7, failure_id=None, failure_signature=fact.signatureKey, action="mark_flaky")
    result = history_search_similar_failure_facts(_context(context, facts=[fact]), store=store)
    assert result["candidates"] == []
    assert "mark_flaky" in result["currentFacts"][0]["blockedReason"]
    signature = result["currentFacts"][0]["noOwnerDecision"]["signature"]
    assert signature["errorCode"] == fact.errorCode
    assert signature["failureKind"] == fact.failureKind


def test_ai_history_feedback_mark_no_owner_blocks_inheritance(monkeypatch, repo_cache, sample_repo, logs):
    monkeypatch.setattr("ci_owner_agent.services.ai_history_search.compare_failure_facts_with_ai", lambda **kwargs: _same())
    context = make_lc_context(repo_cache, sample_repo, logs)
    store = make_store()
    fact = make_fact()
    _save_fact(store, context, build=7, fact=fact)
    FeedbackStore(store).apply_feedback(repo=context.repo, job=context.job, branch=context.branch, build_number=7, failure_id=None, failure_signature=fact.signatureKey, action="mark_no_owner")
    result = history_search_similar_failure_facts(_context(context, facts=[fact]), store=store)
    assert result["candidates"] == []
    assert "mark_no_owner" in result["currentFacts"][0]["blockedReason"]


def test_ai_history_feedback_correct_owner_overrides_owner(monkeypatch, repo_cache, sample_repo, logs):
    monkeypatch.setattr("ci_owner_agent.services.ai_history_search.compare_failure_facts_with_ai", lambda **kwargs: _same())
    context = make_lc_context(repo_cache, sample_repo, logs)
    store = make_store()
    fact = make_fact()
    _save_fact(store, context, build=7, fact=fact, owner_name="test")
    FeedbackStore(store).apply_feedback(
        repo=context.repo,
        job=context.job,
        branch=context.branch,
        build_number=7,
        failure_id=None,
        failure_signature=fact.signatureKey,
        action="correct_owner",
        owner_name="lisi",
        owner_email="lisi@test.com",
    )
    result = history_search_similar_failure_facts(_context(context, facts=[fact]), store=store)
    inherited = result["currentFacts"][0]["inheritedOwner"]
    assert inherited["found"] is True
    assert inherited["ownerName"] == "lisi"
    assert inherited["feedbackCorrected"] is True


def test_ai_history_feedback_correct_owner_medium_confidence_can_inherit(monkeypatch, repo_cache, sample_repo, logs):
    monkeypatch.setattr("ci_owner_agent.services.ai_history_search.compare_failure_facts_with_ai", lambda **kwargs: _same())
    context = make_lc_context(repo_cache, sample_repo, logs)
    store = make_store()
    fact = make_fact()
    _save_fact(store, context, build=7, fact=fact, owner_name="test")
    FeedbackStore(store).apply_feedback(
        repo=context.repo,
        job=context.job,
        branch=context.branch,
        build_number=7,
        failure_id=None,
        failure_signature=fact.signatureKey,
        action="correct_owner",
        owner_name="lisi",
        owner_email="lisi@test.com",
        owner_type="medium_confidence",
    )
    result = history_search_similar_failure_facts(_context(context, facts=[fact]), store=store)
    inherited = result["currentFacts"][0]["inheritedOwner"]
    assert inherited["found"] is True
    assert inherited["ownerName"] == "lisi"
    assert inherited["ownerType"] == "medium_confidence"
    assert inherited["confidence"] > 0
    assert inherited["feedbackCorrected"] is True


def test_ai_history_feedback_confirm_owner_marks_verified(monkeypatch, repo_cache, sample_repo, logs):
    monkeypatch.setattr("ci_owner_agent.services.ai_history_search.compare_failure_facts_with_ai", lambda **kwargs: _same())
    context = make_lc_context(repo_cache, sample_repo, logs)
    store = make_store()
    fact = make_fact()
    _save_fact(store, context, build=7, fact=fact, owner_name="test")
    FeedbackStore(store).apply_feedback(repo=context.repo, job=context.job, branch=context.branch, build_number=7, failure_id=None, failure_signature=fact.signatureKey, action="confirm_owner")
    result = history_search_similar_failure_facts(_context(context, facts=[fact]), store=store)
    inherited = result["currentFacts"][0]["inheritedOwner"]
    assert inherited["ownerName"] == "test"
    assert inherited["feedbackVerified"] is True


def test_ai_history_limits_compare_calls(monkeypatch, repo_cache, sample_repo, logs):
    calls = []

    def fake_compare(**kwargs):
        calls.append(kwargs)
        return _different()

    monkeypatch.setattr("ci_owner_agent.services.ai_history_search.compare_failure_facts_with_ai", fake_compare)
    context = make_lc_context(repo_cache, sample_repo, logs)
    context = _context(context, build=10, facts=[make_fact()], settings=_settings(context, ai_history_max_compare_calls=2))
    store = make_store()
    for build in [7, 8, 9]:
        _save_fact(store, context, build=build, fact=make_fact(signatureKey=f"sig-{build}"))
    result = history_search_similar_failure_facts(context, store=store)
    assert result["ok"] is True
    assert len(calls) == 2


def test_ai_history_candidate_sort_prefers_signature_and_error_code(monkeypatch, repo_cache, sample_repo, logs):
    compared = []

    def fake_compare(**kwargs):
        compared.append(kwargs["historical_fact"].signatureKey)
        return _different()

    monkeypatch.setattr("ci_owner_agent.services.ai_history_search.compare_failure_facts_with_ai", fake_compare)
    context = make_lc_context(repo_cache, sample_repo, logs)
    current = make_fact(signatureKey="same-signature")
    store = make_store()
    _save_fact(store, context, build=7, fact=make_fact(signatureKey="only-kind", errorCode=None, symbol=None))
    _save_fact(store, context, build=8, fact=make_fact(signatureKey="same-signature"))
    history_search_similar_failure_facts(_context(context, facts=[current], build=9), store=store)
    assert compared[0] == current.signatureKey


def test_ai_history_compare_exception_does_not_fail(monkeypatch, repo_cache, sample_repo, logs):
    def explode(**kwargs):
        raise RuntimeError("compare exploded")

    monkeypatch.setattr("ci_owner_agent.services.ai_history_search.compare_failure_facts_with_ai", explode)
    context = make_lc_context(repo_cache, sample_repo, logs)
    store = make_store()
    fact = make_fact()
    _save_fact(store, context, build=7, fact=fact)
    result = history_search_similar_failure_facts(_context(context, facts=[fact]), store=store)
    assert result["ok"] is True
    assert result["candidates"] == []
    assert "compare exploded" in result["warning"]
    assert result["diagnostics"]["skipped"]["compareError"] > 0
    assert result["diagnostics"]["compareResults"][0]["skipReason"] == "compare_error"
