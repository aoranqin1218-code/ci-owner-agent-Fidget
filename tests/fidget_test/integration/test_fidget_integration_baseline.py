from __future__ import annotations

import subprocess
from dataclasses import replace
from pathlib import Path

from ci_owner_agent.config import load_settings
from ci_owner_agent.orchestrator import analyze_local, enforce_integration_suite_owner_ranges
from ci_owner_agent.schemas import BuildInfo, CiResponsibilityNotice, EvidenceItem, Owner, ResponsibilityItem
from ci_owner_agent.services.git_client import GitClient
from ci_owner_agent.services.integration_baseline import (
    build_trusted_integration_run_facts,
    derive_previous_stable_version,
    resolve_integration_baseline,
    resolve_previous_version_checkpoint,
    trusted_integration_assertion_suites,
)
from ci_owner_agent.services.failure_identity import build_failure_summary_signature
from ci_owner_agent.services.log_provider import LocalFileLogProvider


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return completed.stdout.strip()


def _write_sdk_version(repo: Path, version: str) -> None:
    package = repo / "packages" / "fidget-sdk" / "package.json"
    package.parent.mkdir(parents=True, exist_ok=True)
    package.write_text('{"name":"@fx/fidget-sdk","version":"' + version + '"}\n', encoding="utf-8")


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _version_fixture(repo_cache: Path) -> dict[str, str]:
    repo = repo_cache / "fxp-fidget"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.name", "CI Owner Agent test")
    _git(repo, "config", "user.email", "ci-owner-agent@example.test")
    _write_sdk_version(repo, "1.7.0")
    release_base = _commit(repo, "release 1.7.0")
    _git(repo, "branch", "-M", "main")

    # Deliberately place the release tag on a sibling branch.  It is not a
    # direct ancestor of main, so the resolver must accept only its merge-base
    # after checking that the merge-base itself declares version 1.7.0.
    _git(repo, "checkout", "-b", "master", release_base)
    (repo / "RELEASE.md").write_text("release metadata\n", encoding="utf-8")
    tag_commit = _commit(repo, "release metadata")
    _git(repo, "tag", "-a", "v1.7.0", "-m", "release 1.7.0", tag_commit)

    _git(repo, "checkout", "main")
    _write_sdk_version(repo, "1.8.0-dev.1")
    head = _commit(repo, "start 1.8.0 development")
    return {"repo": "fxp-fidget", "release_base": release_base, "head": head}


def test_derive_previous_stable_minor_never_uses_time_or_commit_fallback():
    assert derive_previous_stable_version("1.8.0-dev.1") == "1.7.0"
    assert derive_previous_stable_version("2.5.3") == "2.4.0"
    assert derive_previous_stable_version("1.0.0-dev.1") is None
    assert derive_previous_stable_version("not-a-version") is None


def test_build_24_assertion_sample_is_eligible_for_version_baseline():
    sample = Path(__file__).resolve().parents[3] / "samples" / "fidget_log" / "integration" / "failure-assertion.log"
    summaries = LocalFileLogProvider(sample).find_test_failure_summaries(max_chunks=5)

    assert trusted_integration_assertion_suites(summaries) == ("select-integration",)
    chunks = summaries["chunks"]
    assert len(chunks) == 2
    assert all(item["stageName"] == "Integration Tests" for item in chunks)
    assert "F-1003" in chunks[0]["content"] or "F-1003" in chunks[1]["content"]
    assert "O-0503" in chunks[0]["content"] or "O-0503" in chunks[1]["content"]


def test_resolve_previous_version_checkpoint_uses_tag_merge_base_and_package_history(repo_cache: Path):
    fixture = _version_fixture(repo_cache)
    result = resolve_previous_version_checkpoint(
        git_client=GitClient(repo_cache),
        repo=fixture["repo"],
        head_commit=fixture["head"],
        suites=("select-integration",),
    )

    assert result.ok is True
    assert result.baseline_type == "previous_stable_version"
    assert result.current_version == "1.8.0-dev.1"
    assert result.target_version == "1.7.0"
    assert result.baseline_commit == fixture["release_base"]
    assert result.suites == ("select-integration",)
    assert "package_history" in result.candidate_sources
    assert "tag_merge_base:v1.7.0" in result.candidate_sources
    assert GitClient(repo_cache).check_ancestor(fixture["repo"], result.baseline_commit or "", fixture["head"])["isAncestor"] is True


def test_resolve_previous_version_checkpoint_rejects_missing_target_release(repo_cache: Path):
    fixture = _version_fixture(repo_cache)
    repo = Path(repo_cache) / fixture["repo"]
    _git(repo, "tag", "-d", "v1.7.0")
    _git(repo, "checkout", "main")
    _write_sdk_version(repo, "1.9.0-dev.1")
    head = _commit(repo, "start 1.9.0 development")

    result = resolve_previous_version_checkpoint(
        git_client=GitClient(repo_cache),
        repo=fixture["repo"],
        head_commit=head,
    )

    assert result.ok is False
    assert result.baseline_commit is None
    assert result.target_version == "1.8.0"
    assert "no trusted ancestor" in (result.reason or "")


def test_unit_or_protocol_conflict_never_enters_version_baseline_selection():
    unit_mixed = {
        "chunks": [
            {"anchorType": "japa_failure_block", "stageName": "Integration Tests"},
            {"anchorType": "japa_failure_block", "stageName": "Unit Tests"},
        ],
        "integrationClassifications": [
            {"kind": "assertion", "stage": "Integration Tests", "suite": "select-integration"},
        ],
    }
    conflicted = {
        "chunks": [{"anchorType": "japa_failure_block", "stageName": "Integration Tests"}],
        "integrationClassifications": [
            {"kind": "assertion", "stage": "Integration Tests", "suite": "select-integration"},
        ],
        "integrationConflicts": ["missing cleanup"],
    }

    assert trusted_integration_assertion_suites(unit_mixed) == ()
    assert trusted_integration_assertion_suites(conflicted) == ()
    assert trusted_integration_assertion_suites(
        {
            "chunks": [{"anchorType": "japa_failure_block", "stageName": "Integration Tests"}],
            "coverageFiles": [{"rawCoveragePath": "src/example.ts"}],
            "integrationClassifications": [
                {"kind": "assertion", "stage": "Integration Tests", "suite": "select-integration"},
            ],
        }
    ) == ()


def test_protocol_facts_record_each_passing_suite_without_persisting_connection_details():
    sample = Path(__file__).resolve().parents[3] / "samples" / "fidget_log" / "integration" / "failure-assertion.log"
    summaries = LocalFileLogProvider(sample).find_test_failure_summaries(max_chunks=5)
    build_info = BuildInfo(
        job="npm/fxp-fidget/fidget-xiaoqin-pipeline",
        buildNumber=24,
        result="FAILURE",
        buildUrl="https://jenkins.example/jenkins/job/npm/job/fxp-fidget/job/fidget-xiaoqin-pipeline/24/",
        branch="main",
        commit="a" * 40,
    )

    facts = build_trusted_integration_run_facts(summaries["integrationProtocolIndex"], build_info)

    assert facts is not None
    assert facts["protocolValid"] is True
    assert facts["cleanupStatus"] == "success"
    assert {item["name"] for item in facts["suites"]} == {
        "select-integration",
        "stream-select-integration",
        "delete-integration",
        "insert-integration",
        "update-integration",
        "upsert-integration",
        "bulk-integration",
        "shadow-integration",
    }
    assert next(item for item in facts["suites"] if item["name"] == "select-integration") == {
        "name": "select-integration",
        "status": "failed",
        "exitCode": 1,
    }
    assert all("mongo" not in key.lower() or key == "environmentProfile" for key in facts)


def test_suite_checkpoint_is_preferred_before_previous_version(repo_cache: Path):
    fixture = _version_fixture(repo_cache)
    checkpoint = {
        "buildNumber": 23,
        "result": "SUCCESS",
        "headCommit": fixture["release_base"],
        "integrationRun": {
            "protocol": "FIDGET_INTEGRATION_V1",
            "protocolValid": True,
            "environmentProfile": "fidget-mongo42-protonbase-test",
            "cleanupStatus": "success",
            "suites": [{"name": "select-integration", "status": "passed", "exitCode": 0}],
        },
    }

    result = resolve_integration_baseline(
        git_client=GitClient(repo_cache),
        repo=fixture["repo"],
        head_commit=fixture["head"],
        suite="select-integration",
        checkpoint_candidates=[checkpoint],
        current_build_number=24,
    )

    assert result.ok is True
    assert result.baseline_type == "suite_checkpoint"
    assert result.baseline_commit == fixture["release_base"]
    assert result.source_build_number == 23


def test_suite_owner_guard_rejects_owner_at_its_own_baseline(repo_cache: Path):
    fixture = _version_fixture(repo_cache)
    signature = build_failure_summary_signature(
        {"testName": "F-1003", "testCase": "group by main field", "errorType": "AssertionError"}
    )
    owner = Owner(
        type="high_confidence",
        name="Fixture owner",
        email="fixture-owner@example.test",
        commit=fixture["release_base"],
        confidence=0.9,
    )
    item = ResponsibilityItem(
        failureId="F-1003",
        failureTitle="F-1003",
        failureSignature=signature,
        failureSummary="group by main field",
        owner=owner,
        responsibilityType="current_build_owner",
        sourceCommit=fixture["release_base"],
        confidence=0.9,
        reason="fixture",
        evidenceIds=[],
    )
    notice = CiResponsibilityNotice(
        repo=fixture["repo"],
        job="npm/fxp-fidget/fidget-xiaoqin-pipeline",
        buildNumber=24,
        buildUrl="local://build/24",
        result="FAILURE",
        branch="main",
        headCommit=fixture["head"],
        baseCommit=fixture["release_base"],
        owner=owner,
        failureReason="fixture",
        evidence=[],
        responsibilityItems=[item],
        suggestions=[],
        hasHighConfidenceOwner=True,
    )
    baseline = resolve_integration_baseline(
        git_client=GitClient(repo_cache),
        repo=fixture["repo"],
        head_commit=fixture["head"],
        suite="select-integration",
        checkpoint_candidates=[],
        current_build_number=24,
    )

    guarded = enforce_integration_suite_owner_ranges(
        notice,
        repo=fixture["repo"],
        git_client=GitClient(repo_cache),
        head_commit=fixture["head"],
        failure_summaries={
            "chunks": [
                {
                    "integrationSuite": "select-integration",
                    "signature": {"testName": "F-1003", "testCase": "group by main field", "errorType": "AssertionError"},
                }
            ]
        },
        suite_baselines={"select-integration": baseline},
    )

    assert guarded.responsibilityItems[0].responsibilityType == "no_high_confidence_owner"
    assert guarded.owner.type == "no_high_confidence_owner"
    assert any(item.id == "E_INTEGRATION_SUITE_SELECT_INTEGRATION" for item in guarded.evidence)


def test_build_24_uses_version_window_and_retains_two_in_range_owners(repo_cache: Path, monkeypatch):
    """The sanitized build #24 replay exercises the F-1003/O-0503 range guard.

    This is intentionally an offline owner-guard proof, not a replacement for
    the later Agent-cache read-only verification of the real source diffs.
    """
    fixture = _version_fixture(repo_cache)
    sample = Path(__file__).resolve().parents[3] / "samples" / "fidget_log" / "integration" / "failure-assertion.log"
    expected_owner = Owner(
        type="high_confidence",
        name="Fixture owner",
        email="fixture-owner@example.test",
        commit=fixture["head"],
        confidence=0.9,
    )

    class KnownBuild24Agent:
        def analyze(self, context):
            assert context.base_commit == fixture["release_base"]
            items = [
                ResponsibilityItem(
                    failureId="auto",
                    failureTitle=failure_id,
                    failureSignature=f"fixture-{failure_id}",
                    failureSummary=failure_id,
                    owner=expected_owner,
                    responsibilityType="current_build_owner",
                    sourceBuildNumber=24,
                    sourceBuildUrl="local://build/24",
                    sourceCommit=fixture["head"],
                    confidence=0.9,
                    reason="fixture diff evidence",
                    evidenceIds=["E_LOG", "E_DIFF"],
                )
                for failure_id in ("F-1003", "O-0503")
            ]
            return CiResponsibilityNotice(
                repo=fixture["repo"],
                job="npm/fxp-fidget/fidget-xiaoqin-pipeline",
                buildNumber=24,
                buildUrl="local://build/24",
                result="FAILURE",
                branch="main",
                headCommit=fixture["head"],
                baseCommit=fixture["release_base"],
                owner=expected_owner,
                failureReason="fixture build #24 analysis",
                evidence=[
                    EvidenceItem(id="E_LOG", type="log", summary="F-1003/O-0503", detail="sanitized build #24", source="fixture"),
                    EvidenceItem(id="E_DIFF", type="diff", summary="related change", detail="fixture", source="fixture"),
                ],
                responsibilityItems=items,
                suggestions=[],
                hasHighConfidenceOwner=True,
            )

    monkeypatch.setattr("ci_owner_agent.orchestrator.create_responsibility_agent", lambda *_: KnownBuild24Agent())
    settings = replace(load_settings(), history_enabled=False)
    notice = analyze_local(
        repo=fixture["repo"],
        job="npm/fxp-fidget/fidget-xiaoqin-pipeline",
        build=24,
        branch="main",
        base_commit=fixture["head"],
        head_commit=fixture["head"],
        console_file=str(sample),
        build_url="local://build/24",
        git_client=GitClient(repo_cache),
        result="FAILURE",
        settings=settings,
        ignore_checkout_commit_mismatch=True,
    )

    assert notice.baseCommit == fixture["release_base"]
    assert [item.failureTitle for item in notice.responsibilityItems] == ["F-1003", "O-0503"]
    assert all(item.owner.commit == fixture["head"] for item in notice.responsibilityItems)
    assert any(item.id == "E_INTEGRATION_BASELINE" for item in notice.evidence)
