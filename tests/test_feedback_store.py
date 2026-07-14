from __future__ import annotations

import pytest
from dataclasses import replace

from ci_owner_agent.schemas import BuildInfo, CiResponsibilityNotice
from ci_owner_agent.main import main
from ci_owner_agent.services.feedback_store import FeedbackStore
from ci_owner_agent.tools.history_tools import history_search_similar_failures
from tests.test_history_store import focused_chunk, make_store
from tests.test_langchain_agent import high_confidence_payload, make_lc_context


def notice_with_item(context, owner_name="Zhang San", failure_signature="sig-same"):
    payload = high_confidence_payload(context)
    payload["owner"]["name"] = owner_name
    payload["responsibilityItems"] = [
        {
            "failureId": "auto",
            "failureTitle": "same failure",
            "failureSignature": failure_signature,
            "failureSummary": "same",
            "owner": payload["owner"],
            "responsibilityType": "current_build_owner",
            "sourceBuildNumber": context.build_number,
            "sourceCommit": context.head_commit,
            "confidence": 0.9,
            "reason": "model owner",
            "evidenceIds": ["E1", "E2"],
        }
    ]
    return CiResponsibilityNotice.model_validate(payload)


def save_history_build(store, context, build_number: int, notice, chunk):
    build_info = BuildInfo(job=context.job, buildNumber=build_number, result="FAILURE", buildUrl=f"local://job/{build_number}", branch=context.branch, commit=context.head_commit)
    store.save_analysis(build_info, notice, context.base_commit, context.head_commit, 5068, context.base_commit, [chunk])


def test_correct_owner_writes_feedback_and_deactivates_old(repo_cache, sample_repo, logs):
    context = make_lc_context(repo_cache, sample_repo, logs)
    store = make_store()
    chunk = focused_chunk("FAIL same\nError: UNKNOWN")
    notice = notice_with_item(context)
    save_history_build(store, context, 5099, notice, chunk)
    feedback = FeedbackStore(store)
    first = feedback.apply_feedback(repo=context.repo, job=context.job, branch=context.branch, build_number=5099, failure_id=notice.responsibilityItems[0].failureId, failure_signature=None, action="correct_owner", owner_name="Li Si")
    second = feedback.apply_feedback(repo=context.repo, job=context.job, branch=context.branch, build_number=5099, failure_id=notice.responsibilityItems[0].failureId, failure_signature=None, action="correct_owner", owner_name="Wang Wu")
    assert first["ok"] is True
    assert second["ok"] is True
    active = [doc for doc in store.feedback.docs if doc.get("isActive")]
    inactive = [doc for doc in store.feedback.docs if not doc.get("isActive")]
    assert len(active) == 1
    assert active[0]["correctedOwner"]["name"] == "Wang Wu"
    assert inactive


def test_correct_owner_requires_owner_name(repo_cache, sample_repo, logs):
    feedback = FeedbackStore(make_store())
    with pytest.raises(ValueError):
        feedback.apply_feedback(repo="repo", job="job", branch="dev", build_number=1, failure_id="failure-x", failure_signature=None, action="correct_owner")


def test_correct_owner_rejects_invalid_owner_type(repo_cache, sample_repo, logs):
    feedback = FeedbackStore(make_store())
    with pytest.raises(ValueError, match="owner-type for correct_owner"):
        feedback.apply_feedback(
            repo="repo",
            job="job",
            branch="dev",
            build_number=1,
            failure_id="failure-x",
            failure_signature=None,
            action="correct_owner",
            owner_name="Li Si",
            owner_type="no_high_confidence_owner",
        )


def test_feedback_requires_existing_notice(repo_cache, sample_repo, logs):
    with pytest.raises(ValueError, match="notice not found for repo=repo, job=job, branch=dev, build=1"):
        FeedbackStore(make_store()).apply_feedback(repo="repo", job="job", branch="dev", build_number=1, failure_id="failure-x", failure_signature=None, action="mark_flaky")


def test_feedback_isolated_by_repo_and_requires_repo():
    store = make_store()
    store.notices.update_one(
        {"repo": "repo-a", "job": "job", "branch": "dev", "buildNumber": 1},
        {"$set": {"repo": "repo-a", "job": "job", "branch": "dev", "buildNumber": 1, "notice": {"responsibilityItems": []}}},
        upsert=True,
    )
    feedback = FeedbackStore(store)
    with pytest.raises(ValueError, match="repo is required"):
        feedback.list_feedback(repo="", job="job", branch="dev", build_number=1)
    feedback.apply_feedback(repo="repo-a", job="job", branch="dev", build_number=1, failure_id="failure-x", failure_signature=None, action="mark_flaky")
    assert len(feedback.list_feedback(repo="repo-a", job="job", branch="dev", build_number=1)) == 1
    assert feedback.list_feedback(repo="repo-b", job="job", branch="dev", build_number=1) == []


def test_feedback_store_isolated_by_branch_and_update_does_not_deactivate_other_branch():
    store = make_store()
    for branch in ("dev", "release"):
        store.notices.update_one(
            {"repo": "repo-a", "job": "job-x", "branch": branch, "buildNumber": 100},
            {"$set": {"repo": "repo-a", "job": "job-x", "branch": branch, "buildNumber": 100, "notice": {"responsibilityItems": []}}},
            upsert=True,
        )
    feedback = FeedbackStore(store)
    feedback.apply_feedback(repo="repo-a", job="job-x", branch="dev", build_number=100, failure_id="dev", failure_signature=None, action="mark_flaky")
    feedback.apply_feedback(repo="repo-a", job="job-x", branch="release", build_number=100, failure_id="release", failure_signature=None, action="mark_flaky")
    feedback.apply_feedback(repo="repo-a", job="job-x", branch="dev", build_number=100, failure_id="dev", failure_signature=None, action="confirm_owner")
    assert [doc["failureId"] for doc in feedback.list_feedback(repo="repo-a", job="job-x", branch="dev", build_number=100)] == ["dev"]
    assert [doc["failureId"] for doc in feedback.list_feedback(repo="repo-a", job="job-x", branch="release", build_number=100)] == ["release"]


def test_feedback_fills_original_owner_and_signature_from_notice(repo_cache, sample_repo, logs):
    context = make_lc_context(repo_cache, sample_repo, logs)
    store = make_store()
    chunk = focused_chunk("FAIL same\nError: UNKNOWN")
    notice = notice_with_item(context)
    save_history_build(store, context, 5099, notice, chunk)
    result = FeedbackStore(store).apply_feedback(
        repo=context.repo,
        job=context.job,
        branch=context.branch,
        build_number=5099,
        failure_id=notice.responsibilityItems[0].failureId,
        failure_signature=None,
        action="confirm_owner",
    )
    assert result["feedback"]["failureSignature"] == "sig-same"
    assert result["feedback"]["originalOwner"]["name"] == "Zhang San"


def test_history_overlay_correct_owner_changes_inherited_owner(repo_cache, sample_repo, logs):
    context = make_lc_context(repo_cache, sample_repo, logs)
    chunk = focused_chunk("FAIL same\nError: UNKNOWN")
    context = replace(context, settings=replace(context.settings, history_enabled=True), build_number=5100, failure_summaries={"chunks": [chunk]})
    store = make_store()
    notice = notice_with_item(context, owner_name="Zhang San", failure_signature=chunk["signature"]["signatureKey"])
    save_history_build(store, context, 5099, notice, chunk)
    FeedbackStore(store).apply_feedback(
        repo=context.repo,
        job=context.job,
        branch=context.branch,
        build_number=5099,
        failure_id=notice.responsibilityItems[0].failureId,
        failure_signature=None,
        action="correct_owner",
        owner_name="Li Si",
        owner_email="lisi@example.com",
    )
    result = history_search_similar_failures(context, store=store)
    assert result["currentChunks"][0]["inheritedOwner"]["ownerName"] == "Li Si"


def test_history_overlay_mark_flaky_suppresses_inherited_owner(repo_cache, sample_repo, logs):
    context = make_lc_context(repo_cache, sample_repo, logs)
    chunk = focused_chunk("FAIL same\nError: UNKNOWN")
    context = replace(context, settings=replace(context.settings, history_enabled=True), build_number=5100, failure_summaries={"chunks": [chunk]})
    store = make_store()
    notice = notice_with_item(context, owner_name="Zhang San", failure_signature=chunk["signature"]["signatureKey"])
    save_history_build(store, context, 5099, notice, chunk)
    FeedbackStore(store).apply_feedback(
        repo=context.repo,
        job=context.job,
        branch=context.branch,
        build_number=5099,
        failure_id=notice.responsibilityItems[0].failureId,
        failure_signature=None,
        action="mark_flaky",
    )
    result = history_search_similar_failures(context, store=store)
    assert result["currentChunks"][0]["inheritedOwner"]["found"] is False


def test_history_overlay_confirm_owner_marks_inherited_owner_verified(repo_cache, sample_repo, logs):
    context = make_lc_context(repo_cache, sample_repo, logs)
    chunk = focused_chunk("FAIL same\nError: UNKNOWN")
    context = replace(context, settings=replace(context.settings, history_enabled=True), build_number=5100, failure_summaries={"chunks": [chunk]})
    store = make_store()
    notice = notice_with_item(context, owner_name="Zhang San", failure_signature=chunk["signature"]["signatureKey"])
    save_history_build(store, context, 5099, notice, chunk)
    FeedbackStore(store).apply_feedback(
        repo=context.repo,
        job=context.job,
        branch=context.branch,
        build_number=5099,
        failure_id=notice.responsibilityItems[0].failureId,
        failure_signature=None,
        action="confirm_owner",
    )

    result = history_search_similar_failures(context, store=store)

    inherited_owner = result["currentChunks"][0]["inheritedOwner"]
    assert inherited_owner["ownerName"] == "Zhang San"
    assert inherited_owner["feedbackVerified"] is True


def test_feedback_apply_cli_requires_branch(monkeypatch, capsys):
    store = make_store()
    monkeypatch.setenv("CI_AGENT_MODEL_PROVIDER", "fake")
    monkeypatch.setenv("CI_AGENT_HISTORY_ENABLED", "true")
    monkeypatch.setattr("ci_owner_agent.main.get_history_store", lambda settings: store)
    rc = main(
        [
            "feedback",
            "apply",
            "--repo",
            "sample-ts-repo",
            "--job",
            "services/fx-code-unittest",
            "--build",
            "5099",
            "--failure-id",
            "failure-xxx",
            "--action",
            "correct_owner",
            "--owner-name",
            "Henry.Zeng-曾纪龙",
        ]
    )
    captured = capsys.readouterr()
    assert rc == 2
    assert "--branch" in captured.err
    assert store.feedback.docs == []


def test_feedback_apply_cli_rejects_bad_owner_type(monkeypatch, capsys):
    monkeypatch.setenv("CI_AGENT_MODEL_PROVIDER", "fake")
    rc = main(
        [
            "feedback",
            "apply",
            "--job",
            "services/fx-code-unittest",
            "--build",
            "5099",
            "--failure-id",
            "failure-xxx",
            "--action",
            "correct_owner",
            "--owner-name",
            "Henry.Zeng-曾纪龙",
            "--owner-type",
            "no_high_confidence_owner",
        ]
    )
    captured = capsys.readouterr()
    assert rc == 2
    assert "invalid choice" in captured.err
    assert "Traceback" not in captured.err
