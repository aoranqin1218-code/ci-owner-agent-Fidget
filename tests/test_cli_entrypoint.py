from __future__ import annotations

import os
import subprocess
import sys


def _run_module(*args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["CI_AGENT_MODEL_PROVIDER"] = "fake"
    return subprocess.run(
        [sys.executable, "-m", "ci_owner_agent", *args],
        cwd=os.getcwd(),
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def test_module_notify_notice_missing_file_returns_process_code_2(tmp_path):
    missing = tmp_path / "missing.json"

    completed = _run_module("notify-notice", "--notice-file", str(missing), "--dry-run")

    assert completed.returncode == 2
    assert "ERROR: notice file not found" in completed.stderr
    assert "Traceback" not in completed.stderr


def test_module_feedback_apply_bad_owner_type_returns_process_code_2():
    completed = _run_module(
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
    )

    assert completed.returncode == 2
    assert "invalid choice" in completed.stderr
    assert "Traceback" not in completed.stderr
