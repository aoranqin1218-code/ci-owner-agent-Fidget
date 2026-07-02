from __future__ import annotations

from pathlib import Path

from ci_owner_agent.orchestrator import analyze_local
from ci_owner_agent.services.git_client import GitClient
from ci_owner_agent.tools.typescript_tools import ts_analyze_changed_functions


def test_typescript_tool_failure_is_structured():
    result = ts_analyze_changed_functions()
    assert result["ok"] is False
    assert result["changedFunctions"] == []


def test_typescript_tool_failure_does_not_break_analysis(repo_cache: Path, readme_only_repo, logs):
    notice = analyze_local(
        repo=readme_only_repo["repo"],
        job="services/fx-code-unittest",
        build=5061,
        branch="dev",
        base_commit=readme_only_repo["base"],
        head_commit=readme_only_repo["head"],
        console_file=str(logs["unknown_failed"]),
        build_url="local://services/fx-code-unittest/5061",
        git_client=GitClient(repo_cache),
    )
    assert notice.owner.type == "no_high_confidence_owner"
