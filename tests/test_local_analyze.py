from __future__ import annotations

import json
from pathlib import Path

import pytest

from ci_owner_agent.main import main
from ci_owner_agent.orchestrator import analyze_local
from ci_owner_agent.services.git_client import GitClient
from ci_owner_agent.services.log_provider import detect_checkout_revision_from_console_log


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


def test_detect_checkout_revision_from_console_log():
    text = (
        "git checkout -f 1111111111111111111111111111111111111111\n"
        "Checking out Revision b9869e53b70cc543aac84a2148da0fd7a945b4ff (refs/remotes/origin/dev)\n"
    )
    assert detect_checkout_revision_from_console_log(text) == "b9869e53b70cc543aac84a2148da0fd7a945b4ff"
    assert detect_checkout_revision_from_console_log("git checkout -f 1234567890abcdef") == "1234567890abcdef"
    assert detect_checkout_revision_from_console_log("no checkout here") is None


def test_analyze_local_rejects_head_commit_mismatch(repo_cache: Path, sample_repo, logs, tmp_path: Path):
    actual = "b9869e53b70cc543aac84a2148da0fd7a945b4ff"
    log = tmp_path / "mismatch.log"
    log.write_text(f"Checking out Revision {actual}\nFinished: FAILURE\n", encoding="utf-8")
    with pytest.raises(ValueError) as exc:
        analyze_local(
            repo=sample_repo["repo"],
            job="job",
            build=1,
            branch="dev",
            base_commit=sample_repo["base"],
            head_commit=sample_repo["head"],
            console_file=str(log),
            build_url="local://job/1",
            git_client=GitClient(repo_cache),
        )
    assert actual in str(exc.value)
    assert "--head-commit" in str(exc.value)


def test_analyze_local_allows_when_checkout_commit_matches(repo_cache: Path, sample_repo, tmp_path: Path):
    log = tmp_path / "match.log"
    log.write_text(f"Checking out Revision {sample_repo['head']}\nFinished: SUCCESS\n", encoding="utf-8")
    notice = analyze_local(
        repo=sample_repo["repo"],
        job="job",
        build=1,
        branch="dev",
        base_commit=sample_repo["base"],
        head_commit=sample_repo["head"],
        console_file=str(log),
        build_url="local://job/1",
        git_client=GitClient(repo_cache),
    )
    assert notice.result == "SUCCESS"


def test_analyze_local_allows_when_checkout_commit_not_found(repo_cache: Path, sample_repo, logs):
    notice = analyze_local(
        repo=sample_repo["repo"],
        job="job",
        build=1,
        branch="dev",
        base_commit=sample_repo["base"],
        head_commit=sample_repo["head"],
        console_file=str(logs["success"]),
        build_url="local://job/1",
        git_client=GitClient(repo_cache),
    )
    assert notice.result == "SUCCESS"


def test_analyze_local_can_ignore_checkout_commit_mismatch(repo_cache: Path, sample_repo, tmp_path: Path):
    log = tmp_path / "ignore.log"
    log.write_text("Checking out Revision b9869e53b70cc543aac84a2148da0fd7a945b4ff\nFinished: SUCCESS\n", encoding="utf-8")
    notice = analyze_local(
        repo=sample_repo["repo"],
        job="job",
        build=1,
        branch="dev",
        base_commit=sample_repo["base"],
        head_commit=sample_repo["head"],
        console_file=str(log),
        build_url="local://job/1",
        git_client=GitClient(repo_cache),
        ignore_checkout_commit_mismatch=True,
    )
    assert notice.result == "SUCCESS"


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
    output = capsys.readouterr().out
    assert "```" not in output
    assert output.lstrip().startswith("{")
    payload = json.loads(output)
    assert payload["owner"]["type"] == "high_confidence"


def test_cli_analyze_local_checkout_mismatch_outputs_clear_error(repo_cache: Path, sample_repo, tmp_path: Path, capsys, monkeypatch):
    monkeypatch.setenv("CI_AGENT_REPO_CACHE_DIR", str(repo_cache))
    actual = "b9869e53b70cc543aac84a2148da0fd7a945b4ff"
    log = tmp_path / "mismatch.log"
    log.write_text(f"Checking out Revision {actual}\nFinished: FAILURE\n", encoding="utf-8")
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
            str(log),
            "--build-url",
            "local://services/fx-code-unittest/5061",
        ]
    )
    captured = capsys.readouterr()
    assert code == 2
    assert "ERROR:" in captured.err
    assert actual in captured.err
    assert "--head-commit" in captured.err


def test_cli_analyze_local_accepts_last_success_build(repo_cache: Path, sample_repo, logs, monkeypatch, capsys):
    captured = {}

    def fake_analyze_local(**kwargs):
        captured.update(kwargs)
        from ci_owner_agent.orchestrator import success_notice
        from ci_owner_agent.schemas import BuildInfo

        return success_notice(
            BuildInfo(
                job=kwargs["job"],
                buildNumber=kwargs["build"],
                result="SUCCESS",
                buildUrl=kwargs["build_url"],
                branch=kwargs["branch"],
                commit=kwargs["head_commit"],
            )
        )

    monkeypatch.setenv("CI_AGENT_REPO_CACHE_DIR", str(repo_cache))
    monkeypatch.setattr("ci_owner_agent.main.analyze_local", fake_analyze_local)
    code = main(
        [
            "analyze-local",
            "--repo",
            sample_repo["repo"],
            "--job",
            "services/fx-code-unittest",
            "--build",
            "5076",
            "--branch",
            "dev",
            "--base-commit",
            sample_repo["base"],
            "--head-commit",
            sample_repo["head"],
            "--console-file",
            str(logs["auth_failed"]),
            "--build-url",
            "local://services/fx-code-unittest/5076",
            "--last-success-build",
            "5075",
        ]
    )
    assert code == 0
    assert captured["last_successful_build_number"] == 5075
    assert "```" not in capsys.readouterr().out


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
