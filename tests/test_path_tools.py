from __future__ import annotations

from pathlib import Path

from ci_owner_agent.services.git_client import GitClient
from ci_owner_agent.tools.path_tools import repo_find_paths


def test_repo_find_paths_matches_query_basename_and_suffix(repo_cache: Path, sample_repo):
    result = repo_find_paths(
        GitClient(repo_cache),
        sample_repo["repo"],
        sample_repo["head"],
        query="classify",
        paths=["packages/fxp-ai"],
        suffixes=[".ts"],
        maxMatches=10,
    )
    assert result["ok"] is True
    assert result["matches"] == ["packages/fxp-ai/errors/classify.ts"]
    assert result["truncated"] is False


def test_repo_find_paths_no_match_is_ok(repo_cache: Path, sample_repo):
    result = repo_find_paths(GitClient(repo_cache), sample_repo["repo"], sample_repo["head"], query="missing")
    assert result["ok"] is True
    assert result["matches"] == []


def test_repo_find_paths_full_path_checks_exact_first(repo_cache: Path, sample_repo):
    result = repo_find_paths(
        GitClient(repo_cache),
        sample_repo["repo"],
        sample_repo["head"],
        query="packages/fxp-ai/errors/classify.ts",
    )
    assert result["ok"] is True
    assert result["matches"] == ["packages/fxp-ai/errors/classify.ts"]
    assert result["paths"] == []


def test_repo_find_paths_full_path_falls_back_to_dirname_and_basename(repo_cache: Path, sample_repo):
    result = repo_find_paths(
        GitClient(repo_cache),
        sample_repo["repo"],
        sample_repo["head"],
        query="packages/fxp-ai/errors/missing.ts",
    )
    assert result["ok"] is True
    assert result["query"] == "missing.ts"
    assert result["paths"] == ["packages/fxp-ai/errors"]
    assert result["matches"] == []


def test_repo_find_paths_reports_truncated_listing():
    class FakeClient:
        def list_paths(self, repo, commit, paths=None):
            return {"ok": True, "paths": [], "truncated": True}

    result = repo_find_paths(FakeClient(), "repo", "abc123", query="missing")
    assert result["ok"] is True
    assert result["truncated"] is True
    assert "paths=[...]" in result["suggestion"]


def test_get_file_content_missing_path_suggests_repo_find_paths(repo_cache: Path, sample_repo):
    result = GitClient(repo_cache).get_file_content(sample_repo["repo"], sample_repo["head"], "packages/fxp-ai/src/errors/rules.ts")
    assert result["ok"] is False
    assert "repo_find_paths" in result["suggestion"]
