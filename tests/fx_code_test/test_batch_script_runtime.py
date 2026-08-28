from __future__ import annotations

import subprocess
import sys
import os
import json
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE = REPO_ROOT / "tests" / "fx_code_test" / "fixtures" / "fake_analyze_launcher.py"


def make_fake_python(directory: Path) -> Path:
    if os.name == "nt":
        launcher = directory / "fake-python.cmd"
        launcher.write_text(f'@"{sys.executable}" "{FIXTURE}" %*\r\n', encoding="utf-8")
    else:
        launcher = directory / "fake-python"
        launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FIXTURE}" "$@"\n', encoding="utf-8")
        launcher.chmod(0o755)
    return launcher


def test_production_cli_contains_no_fake_analyzer_switch() -> None:
    forbidden = ("CI_AGENT_TEST_FAKE_ANALYZE_LOCAL_MODE", "CI_AGENT_TEST_DIAGNOSTIC_FILE", "PYTEST_CURRENT_TEST", "test-only fake analyze-local")
    for path in [REPO_ROOT / "ci_owner_agent" / "main.py", *(REPO_ROOT / "scripts").glob("*.py")]:
        text = path.read_text(encoding="utf-8")
        assert not any(token in text for token in forbidden), path


def test_fake_launcher_rejects_unknown_mode(tmp_path: Path) -> None:
    output = tmp_path / "notice.json"
    result = subprocess.run(
        [sys.executable, str(FIXTURE), "-m", "ci_owner_agent", "analyze-local", "--output-file", str(output)],
        env={**os.environ, "FAKE_ANALYZE_MODE": "sucess"}, text=True, encoding="utf-8", capture_output=True, check=False,
    )
    assert result.returncode == 64
    assert "unsupported FAKE_ANALYZE_MODE" in result.stderr
    assert not output.exists()


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
    (outside / "config" / "test.env").write_text("CI_AGENT_HISTORY_ENABLED=false\nCI_AGENT_TEST_ENV_MARKER=loaded-from-relative-env\n", encoding="utf-8")
    diagnostic = outside / "diagnostic.json"
    env = os.environ.copy()
    env.update({
        "FAKE_ANALYZE_MODE": "success",
        "FAKE_ANALYZE_DIAGNOSTIC_FILE": str(diagnostic),
        "CI_AGENT_WECOM_NOTIFY_ENABLED": "false",
    })
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "rerun_analyze_local.py"), "--runs", "1", "--python", str(make_fake_python(outside)), "--repo", "fx-code",
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
    assert observed["marker"] == "loaded-from-relative-env"
