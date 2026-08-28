from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from ci_owner_agent.schemas import CiResponsibilityNotice


REPO_ROOT = Path(__file__).resolve().parents[2]


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=repo, text=True, encoding="utf-8", capture_output=True, check=True)
    return result.stdout.strip()


def test_company_batch_runs_real_analyze_local_with_temporary_git_repo(tmp_path: Path) -> None:
    if shutil.which("git") is None:
        pytest.skip("git executable unavailable")
    outside = tmp_path / "outside"
    log_dir, out_dir = outside / "logs", outside / "output"
    repo = tmp_path / "repo-cache" / "fx-code"
    log_dir.mkdir(parents=True)
    repo.mkdir(parents=True)
    git(repo, "init")
    git(repo, "config", "user.name", "Test User")
    git(repo, "config", "user.email", "test@example.com")
    (repo / "sample.txt").write_text("base\n", encoding="utf-8")
    git(repo, "add", "sample.txt")
    git(repo, "commit", "-m", "base")
    base_sha = git(repo, "rev-parse", "HEAD")
    (repo / "sample.txt").write_text("head\n", encoding="utf-8")
    git(repo, "commit", "-am", "head")
    head_sha = git(repo, "rev-parse", "HEAD")
    (log_dir / "company-unittest-5088.log").write_text(
        f"Checking out Revision {head_sha} (refs/remotes/origin/dev)\n"
        f"> git checkout -f {head_sha}\n"
        "1) SampleSuite should work\nAssertionError: expected 1 to equal 2\n"
        "    at test/sample.test.ts:10:5\nFinished: FAILURE\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env.update({
        "CI_AGENT_MODEL_PROVIDER": "fake", "CI_AGENT_HISTORY_ENABLED": "false",
        "CI_AGENT_WECOM_NOTIFY_ENABLED": "false", "CI_AGENT_WECOM_BOT_DISCOVER_CHAT_ID": "false",
        "CI_AGENT_METRICS_ENABLED": "true", "LANGCHAIN_TRACING_V2": "false", "LANGSMITH_TRACING": "false",
        "CI_AGENT_REPO_CACHE_DIR": str(tmp_path / "repo-cache"),
    })
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "batch_analyze_company_logs.py"),
         "--log-dir", "logs", "--out-dir", "output", "--repo", "fx-code", "--job", "services/fx-code-unittest",
         "--branch", "origin/dev", "--initial-base-commit", base_sha, "--timeout-seconds", "30"],
        cwd=outside, env=env, text=True, encoding="utf-8", capture_output=True, check=False,
    )
    assert result.returncode == 0, result.stderr + result.stdout[-2000:]
    notice_path = next((out_dir / "notices").glob("*.notice.json"))
    notice = CiResponsibilityNotice.model_validate_json(notice_path.read_text(encoding="utf-8"))
    assert (notice.repo, notice.job, notice.buildNumber, notice.result, notice.branch) == (
        "fx-code", "services/fx-code-unittest", 5088, "FAILURE", "dev"
    )
    assert notice.baseCommit == base_sha and notice.headCommit == head_sha
    index = json.loads((out_dir / "index.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert index["noticeValid"] is True
    assert (out_dir / "summary.csv").exists()
    assert list((out_dir / "metrics").glob("*.metrics.jsonl"))
    assert list((out_dir / "stdout").glob("*.stdout.txt"))
    assert list((out_dir / "stderr").glob("*.stderr.txt"))

    rerun = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "rerun_analyze_local.py"), "--runs", "1", "--repo", "fx-code",
         "--job", "services/fx-code-unittest", "--build", "5088", "--branch", "origin/dev", "--base-commit", base_sha,
         "--head-commit", head_sha, "--console-file", "logs/company-unittest-5088.log", "--out-dir", "rerun-output"],
        cwd=outside, env=env, text=True, encoding="utf-8", capture_output=True, check=False,
    )
    assert rerun.returncode == 0, rerun.stderr + rerun.stdout[-2000:]
    rerun_notice = CiResponsibilityNotice.model_validate_json((outside / "rerun-output" / "run-01" / "notice.json").read_text(encoding="utf-8"))
    assert rerun_notice.baseCommit == base_sha and rerun_notice.headCommit == head_sha and rerun_notice.branch == "dev"
    rerun_summary = json.loads((outside / "rerun-output" / "summary.json").read_text(encoding="utf-8"))[0]
    assert rerun_summary["status"] == "OK" and rerun_summary["noticeValid"] is True
