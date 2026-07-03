from __future__ import annotations

from pathlib import Path

import pytest

from ci_owner_agent.orchestrator import analyze_local
from ci_owner_agent.services.command_runner import run_command
from ci_owner_agent.services.command_runner import CommandResult
from ci_owner_agent.services.git_client import GitClient
from ci_owner_agent.tools.typescript_tools import (
    check_node_dependencies_for_analysis,
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


def test_ts_analyzer_relative_dir_does_not_duplicate_path(repo_cache: Path, sample_repo, monkeypatch):
    calls = {}

    def fake_run_command(args, cwd=None, timeout=30, max_output_chars=20000):
        calls["args"] = [str(arg) for arg in args]
        calls["cwd"] = str(cwd)
        return CommandResult(
            ok=True,
            args=calls["args"],
            returncode=0,
            stdout='{"ok":true,"definitions":[]}',
            stderr="",
        )

    monkeypatch.setattr("ci_owner_agent.tools.typescript_tools.run_command", fake_run_command)
    result = ts_find_definitions(
        repo=sample_repo["repo"],
        commit=sample_repo["head"],
        symbols=["classifyError"],
        repo_cache_dir=repo_cache,
        analyzer_dir=Path("ts-analyzer"),
        allow_checkout=False,
    )
    assert result["ok"] is True
    script = Path(calls["args"][1])
    assert script.is_absolute()
    assert "ts-analyzer\\ts-analyzer" not in calls["args"][1]
    assert "ts-analyzer/ts-analyzer" not in calls["args"][1]
    assert Path(calls["cwd"]).is_absolute()


def test_ts_analyzer_missing_script_returns_clear_error(repo_cache: Path, sample_repo, tmp_path: Path):
    analyzer = tmp_path / "empty-analyzer"
    analyzer.mkdir()
    result = ts_find_definitions(
        repo=sample_repo["repo"],
        commit=sample_repo["head"],
        symbols=["classifyError"],
        repo_cache_dir=repo_cache,
        analyzer_dir=analyzer,
        allow_checkout=False,
    )
    assert result["ok"] is False
    assert "TS_ANALYZER_DIR" in result["error"]
    assert "src/find_definitions.js" in result["error"].replace("\\", "/")


def test_check_node_dependencies_missing_node_modules(repo_cache: Path, sample_repo):
    result = check_node_dependencies_for_analysis(sample_repo["repo"], repo_cache_dir=repo_cache)
    assert result["ok"] is False
    assert "node_modules" in result["missing"]
    assert "npm install" in result["suggestion"]


def test_check_node_dependencies_missing_extends_package(repo_cache: Path, sample_repo):
    repo_path = Path(sample_repo["path"])
    (repo_path / "node_modules").mkdir()
    (repo_path / "tsconfig.json").write_text('{"extends":"nstarter-tsconfig","include":["packages/**/*.ts"]}', encoding="utf-8")
    result = check_node_dependencies_for_analysis(sample_repo["repo"], repo_cache_dir=repo_cache)
    assert result["ok"] is False
    assert "node_modules/nstarter-tsconfig" in result["missing"]


def test_check_node_dependencies_hash_marker_unchanged(repo_cache: Path, sample_repo):
    repo_path = Path(sample_repo["path"])
    (repo_path / "node_modules").mkdir()
    (repo_path / "package.json").write_text('{"dependencies":{"typescript":"^5.9.3"}}', encoding="utf-8")
    first = check_node_dependencies_for_analysis(sample_repo["repo"], repo_cache_dir=repo_cache)
    assert first["ok"] is True
    assert (repo_path / ".ci-owner-agent" / "deps.json").exists()

    second = check_node_dependencies_for_analysis(sample_repo["repo"], repo_cache_dir=repo_cache)
    assert second["ok"] is True
    assert second["dependenciesCurrent"] is True
    assert not any("changed" in warning for warning in second["warnings"])


def test_check_node_dependencies_hash_change_warns(repo_cache: Path, sample_repo):
    repo_path = Path(sample_repo["path"])
    (repo_path / "node_modules").mkdir()
    package_json = repo_path / "package.json"
    package_json.write_text('{"dependencies":{"typescript":"^5.9.3"}}', encoding="utf-8")
    first = check_node_dependencies_for_analysis(sample_repo["repo"], repo_cache_dir=repo_cache)
    assert first["ok"] is True

    package_json.write_text('{"dependencies":{"typescript":"^5.9.3","left-pad":"1.3.0"}}', encoding="utf-8")
    second = check_node_dependencies_for_analysis(sample_repo["repo"], repo_cache_dir=repo_cache)
    assert second["ok"] is True
    assert second["dependenciesCurrent"] is False
    assert any("dependency definition files changed" in warning for warning in second["warnings"])


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


@pytest.mark.skipif(not has_typescript_dependency(), reason="typescript npm dependency is not installed")
def test_ts_find_definitions_checks_out_requested_commit(repo_cache: Path, sample_repo_with_newer_commit):
    repo_path = Path(sample_repo_with_newer_commit["path"])
    assert run_command(["git", "-C", str(repo_path), "rev-parse", "HEAD"]).stdout.strip() == sample_repo_with_newer_commit["newer"]
    result = ts_find_definitions(
        repo=sample_repo_with_newer_commit["repo"],
        commit=sample_repo_with_newer_commit["head"],
        symbols=["classifyError"],
        repo_cache_dir=repo_cache,
        analyzer_dir=ANALYZER_DIR,
    )
    assert result["ok"] is True
    assert run_command(["git", "-C", str(repo_path), "rev-parse", "HEAD"]).stdout.strip() == sample_repo_with_newer_commit["head"]
    assert any(item["file"] == "packages/fxp-ai/errors/classify.ts" for item in result["definitions"])


@pytest.mark.skipif(not has_typescript_dependency(), reason="typescript npm dependency is not installed")
def test_ts_find_callers_checks_out_commit_and_excludes_definition(repo_cache: Path, sample_repo_with_newer_commit):
    repo_path = Path(sample_repo_with_newer_commit["path"])
    assert run_command(["git", "-C", str(repo_path), "rev-parse", "HEAD"]).stdout.strip() == sample_repo_with_newer_commit["newer"]
    callers = ts_find_callers(
        repo=sample_repo_with_newer_commit["repo"],
        commit=sample_repo_with_newer_commit["head"],
        symbol="classifyError",
        definitionFile="packages/fxp-ai/errors/classify.ts",
        repo_cache_dir=repo_cache,
        analyzer_dir=ANALYZER_DIR,
    )
    assert callers["ok"] is True
    assert run_command(["git", "-C", str(repo_path), "rev-parse", "HEAD"]).stdout.strip() == sample_repo_with_newer_commit["head"]
    assert any(item["file"] == "packages/fxp-ai/errors/consumer.ts" for item in callers["callers"])
    assert not any(item["file"] == "packages/fxp-ai/errors/classify.ts" for item in callers["callers"])


@pytest.mark.skipif(not has_typescript_dependency(), reason="typescript npm dependency is not installed")
def test_ts_checkout_dirty_worktree_requires_force(repo_cache: Path, sample_repo_with_newer_commit):
    repo_path = Path(sample_repo_with_newer_commit["path"])
    classify = repo_path / "packages" / "fxp-ai" / "errors" / "classify.ts"
    classify.write_text(classify.read_text(encoding="utf-8") + "\n// dirty\n", encoding="utf-8")
    blocked = ts_find_definitions(
        repo=sample_repo_with_newer_commit["repo"],
        commit=sample_repo_with_newer_commit["head"],
        symbols=["classifyError"],
        repo_cache_dir=repo_cache,
        analyzer_dir=ANALYZER_DIR,
        force_checkout=False,
    )
    assert blocked["ok"] is False
    assert "worktree is not clean" in blocked["error"]

    forced = ts_find_definitions(
        repo=sample_repo_with_newer_commit["repo"],
        commit=sample_repo_with_newer_commit["head"],
        symbols=["classifyError"],
        repo_cache_dir=repo_cache,
        analyzer_dir=ANALYZER_DIR,
        force_checkout=True,
    )
    assert forced["ok"] is True
    assert run_command(["git", "-C", str(repo_path), "rev-parse", "HEAD"]).stdout.strip() == sample_repo_with_newer_commit["head"]


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
