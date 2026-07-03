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


def test_get_file_content_missing_path_suggests_repo_find_paths(repo_cache: Path, sample_repo):
    result = GitClient(repo_cache).get_file_content(sample_repo["repo"], sample_repo["head"], "packages/fxp-ai/src/errors/rules.ts")
    assert result["ok"] is False
    assert "repo_find_paths" in result["suggestion"]
