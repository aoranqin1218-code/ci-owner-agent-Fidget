from __future__ import annotations

from pathlib import Path

import pytest

from ci_owner_agent.orchestrator import analyze_local
from ci_owner_agent.services.git_client import GitClient
from ci_owner_agent.tools.typescript_tools import (
    ts_analyze_changed_functions,
    ts_find_callers,
    ts_find_definitions,
)


ANALYZER_DIR = Path(__file__).resolve().parents[1] / "ts-analyzer"


def has_typescript_dependency() -> bool:
    return (ANALYZER_DIR / "node_modules" / "typescript").exists()


def test_typescript_tool_failure_is_structured():
    result = ts_analyze_changed_functions()
    assert result["ok"] is False
    assert result["changedFunctions"] == []


def test_typescript_tool_missing_tsconfig_is_structured(repo_cache: Path, sample_repo):
    if not has_typescript_dependency():
        pytest.skip("typescript npm dependency is not installed")
    result = ts_find_definitions(
        repo=sample_repo["repo"],
        commit=sample_repo["head"],
        symbols=["classifyError"],
        tsconfig="missing-tsconfig.json",
        repo_cache_dir=repo_cache,
        analyzer_dir=ANALYZER_DIR,
    )
    assert result["ok"] is False
    assert result["definitions"] == []
    assert "tsconfig not found" in result["error"]


def test_typescript_tool_missing_dependency_is_structured(repo_cache: Path, sample_repo, tmp_path: Path):
    analyzer = tmp_path / "ts-analyzer"
    (analyzer / "src").mkdir(parents=True)
    script = ANALYZER_DIR / "src" / "find_definitions.js"
    (analyzer / "src" / "find_definitions.js").write_text(script.read_text(encoding="utf-8"), encoding="utf-8")
    (analyzer / "src" / "common.js").write_text((ANALYZER_DIR / "src" / "common.js").read_text(encoding="utf-8"), encoding="utf-8")
    result = ts_find_definitions(
        repo=sample_repo["repo"],
        commit=sample_repo["head"],
        symbols=["classifyError"],
        repo_cache_dir=repo_cache,
        analyzer_dir=analyzer,
    )
    assert result["ok"] is False
    assert result["definitions"] == []
    assert "typescript package is not installed" in result["error"]


@pytest.mark.skipif(not has_typescript_dependency(), reason="typescript npm dependency is not installed")
def test_ts_analyze_changed_functions(repo_cache: Path, sample_repo):
    result = ts_analyze_changed_functions(
        repo=sample_repo["repo"],
        baseCommit=sample_repo["base"],
        headCommit=sample_repo["head"],
        files=["packages/fxp-ai/errors/classify.ts", "README.md"],
        repo_cache_dir=repo_cache,
        analyzer_dir=ANALYZER_DIR,
    )
    assert result["ok"] is True
    assert any(item["name"] == "classifyError" for item in result["changedFunctions"])


@pytest.mark.skipif(not has_typescript_dependency(), reason="typescript npm dependency is not installed")
def test_ts_find_definitions_and_callers(repo_cache: Path, sample_repo):
    definitions = ts_find_definitions(
        repo=sample_repo["repo"],
        commit=sample_repo["head"],
        symbols=["classifyError"],
        repo_cache_dir=repo_cache,
        analyzer_dir=ANALYZER_DIR,
    )
    assert definitions["ok"] is True
    assert any(item["file"] == "packages/fxp-ai/errors/classify.ts" for item in definitions["definitions"])

    callers = ts_find_callers(
        repo=sample_repo["repo"],
        commit=sample_repo["head"],
        symbol="classifyError",
        definitionFile="packages/fxp-ai/errors/classify.ts",
        repo_cache_dir=repo_cache,
        analyzer_dir=ANALYZER_DIR,
    )
    assert callers["ok"] is True
    assert any(item["file"] == "packages/fxp-ai/errors/consumer.ts" for item in callers["callers"])


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
