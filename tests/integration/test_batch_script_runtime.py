from __future__ import annotations

import subprocess
import sys
import os
import json
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("script", [
    "batch_analyze_company_logs.py",
    "batch_analyze_jenkins_builds.py",
    "rerun_analyze_local.py",
])
def test_scripts_start_from_external_cwd(tmp_path: Path, script: str) -> None:
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / script), "--help"],
        cwd=tmp_path,
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_rerun_real_cli_from_external_cwd_with_relative_paths(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    (outside / "input").mkdir(parents=True)
    (outside / "config").mkdir()
    (outside / "input" / "console.log").write_text("Finished: FAILURE\n", encoding="utf-8")
    (outside / "config" / "test.env").write_text("CI_AGENT_HISTORY_ENABLED=false\n", encoding="utf-8")
    diagnostic = outside / "diagnostic.json"
    env = os.environ.copy()
    env.update({
        "CI_AGENT_TEST_FAKE_ANALYZE_LOCAL_MODE": "success",
        "CI_AGENT_TEST_DIAGNOSTIC_FILE": str(diagnostic),
        "PYTEST_CURRENT_TEST": "external-cwd-integration",
        "CI_AGENT_WECOM_NOTIFY_ENABLED": "false",
    })
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "rerun_analyze_local.py"), "--runs", "1", "--repo", "fx-code",
         "--job", "services/fx-code-unittest", "--build", "5088", "--branch", "origin/dev", "--base-commit", "base",
         "--head-commit", "head", "--console-file", "input/console.log", "--env-file", "config/test.env", "--out-dir", "output"],
        cwd=outside, env=env, text=True, encoding="utf-8", capture_output=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    output = outside / "output"
    assert (output / "summary.csv").exists() and (output / "run-01" / "notice.json").exists()
    command = (output / "run-01" / "command.txt").read_text(encoding="utf-8")
    assert "-m ci_owner_agent analyze-local" in command and "--branch dev" in command
    observed = json.loads(diagnostic.read_text(encoding="utf-8"))
    assert Path(observed["cwd"]) == REPO_ROOT
    assert observed["pythonpath"].split(os.pathsep)[0] == str(REPO_ROOT)
