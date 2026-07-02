from __future__ import annotations

import json
from pathlib import Path

from ci_owner_agent.main import main
from ci_owner_agent.orchestrator import analyze_local
from ci_owner_agent.services.git_client import GitClient


def test_success_build_short_circuits(repo_cache: Path, sample_repo, logs):
    notice = analyze_local(
        repo=sample_repo["repo"],
        job="services/fx-code-unittest",
        build=5060,
        branch="dev",
        base_commit=sample_repo["base"],
        head_commit=sample_repo["head"],
        console_file=str(logs["success"]),
        build_url="local://services/fx-code-unittest/5060",
        git_client=GitClient(repo_cache),
    )
    assert notice.result == "SUCCESS"
    assert notice.owner.name == "无高可信责任人"
    assert notice.hasHighConfidenceOwner is False


def test_aborted_build_short_circuits(repo_cache: Path, sample_repo, logs):
    notice = analyze_local(
        repo=sample_repo["repo"],
        job="services/fx-code-unittest",
        build=5090,
        branch="dev",
        base_commit=sample_repo["base"],
        head_commit=sample_repo["head"],
        console_file=str(logs["aborted"]),
        build_url="local://services/fx-code-unittest/5090",
        git_client=GitClient(repo_cache),
    )
    assert notice.result == "ABORTED"
    assert notice.owner.type == "no_high_confidence_owner"
    assert "不进入普通业务代码定责流程" in notice.failureReason


def test_failure_high_confidence(repo_cache: Path, sample_repo, logs):
    notice = analyze_local(
        repo=sample_repo["repo"],
        job="services/fx-code-unittest",
        build=5061,
        branch="dev",
        base_commit=sample_repo["base"],
        head_commit=sample_repo["head"],
        console_file=str(logs["auth_failed"]),
        build_url="local://services/fx-code-unittest/5061",
        git_client=GitClient(repo_cache),
    )
    assert notice.owner.type == "high_confidence"
    assert notice.owner.email == "zhangsan@example.com"
    assert notice.hasHighConfidenceOwner is True
    assert len({item.type for item in notice.evidence}) >= 2
    assert any(item.source == "repo_sync" for item in notice.evidence)


def test_failure_insufficient_evidence(repo_cache: Path, readme_only_repo, logs):
    notice = analyze_local(
        repo=readme_only_repo["repo"],
        job="services/fx-code-unittest",
        build=5061,
        branch="dev",
        base_commit=readme_only_repo["base"],
        head_commit=readme_only_repo["head"],
        console_file=str(logs["auth_failed"]),
        build_url="local://services/fx-code-unittest/5061",
        git_client=GitClient(repo_cache),
    )
    assert notice.owner.type == "no_high_confidence_owner"
    assert notice.owner.name == "无高可信责任人"
    assert notice.hasHighConfidenceOwner is False


def test_cli_analyze_local_outputs_json(repo_cache: Path, sample_repo, logs, capsys, monkeypatch):
    monkeypatch.setenv("CI_AGENT_REPO_CACHE_DIR", str(repo_cache))
    code = main(
        [
            "analyze-local",
            "--repo",
            sample_repo["repo"],
            "--job",
            "services/fx-code-unittest",
            "--build",
            "5061",
            "--branch",
            "dev",
            "--base-commit",
            sample_repo["base"],
            "--head-commit",
            sample_repo["head"],
            "--console-file",
            str(logs["auth_failed"]),
            "--build-url",
            "local://services/fx-code-unittest/5061",
        ]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["owner"]["type"] == "high_confidence"


def test_explicit_result_overrides_log_detection(repo_cache: Path, sample_repo, logs):
    notice = analyze_local(
        repo=sample_repo["repo"],
        job="services/fx-code-unittest",
        build=1,
        branch="dev",
        base_commit=sample_repo["base"],
        head_commit=sample_repo["head"],
        console_file=str(logs["success"]),
        build_url="local://services/fx-code-unittest/1",
        git_client=GitClient(repo_cache),
        result="FAILURE",
    )
    assert notice.result == "FAILURE"


def test_formal_failed_build_blocks_on_repo_sync_failure(repo_cache: Path, sample_repo, logs):
    from ci_owner_agent.orchestrator import analyze_failed_build
    from ci_owner_agent.schemas import BuildInfo
    from ci_owner_agent.services.log_provider import LocalFileLogProvider

    provider = LocalFileLogProvider(logs["auth_failed"])
    build_info = BuildInfo(
        job="services/fx-code-unittest",
        buildNumber=5061,
        result="FAILURE",
        buildUrl="jenkins://services/fx-code-unittest/5061",
        branch="dev",
        commit=sample_repo["head"],
        logTail=provider.read_tail(500),
    )
    notice = analyze_failed_build(
        repo=sample_repo["repo"],
        build_info=build_info,
        base_commit=sample_repo["base"],
        head_commit=sample_repo["head"],
        log_provider=provider,
        git_client=GitClient(repo_cache),
        allow_sync_failure=False,
    )
    assert notice.owner.type == "no_high_confidence_owner"
    assert "repo_sync 失败" in notice.failureReason
