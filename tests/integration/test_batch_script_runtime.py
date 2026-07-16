from __future__ import annotations

import subprocess
import sys
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
