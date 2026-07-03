from __future__ import annotations

from pathlib import Path

from ci_owner_agent.services.git_client import GitClient
from ci_owner_agent.tools.keyword_tools import repo_keyword_search


def test_keyword_search_changed_files_and_whole_repo(repo_cache: Path, sample_repo):
    client = GitClient(repo_cache)
    changed = repo_keyword_search(
        client,
        repo=sample_repo["repo"],
        commit=sample_repo["head"],
        keywords=["FILE_SIZE_EXCEEDED"],
        scope="changed_files",
        baseCommit=sample_repo["base"],
        headCommit=sample_repo["head"],
    )
    assert changed["ok"] is True
    assert changed["matches"]
    assert changed["matches"][0]["changedInRange"] is True

    whole = repo_keyword_search(
        client,
        repo=sample_repo["repo"],
        commit=sample_repo["head"],
        keywords=["sample"],
        scope="whole_repo",
    )
    assert whole["ok"] is True
    assert any(match["file"] == "README.md" for match in whole["matches"])


def test_keyword_search_empty_keywords(repo_cache: Path, sample_repo):
    client = GitClient(repo_cache)
    result = repo_keyword_search(client, sample_repo["repo"], sample_repo["head"], [], scope="whole_repo")
    assert result == {"ok": True, "matches": []}


def test_keyword_search_repo_alias(repo_cache: Path, sample_repo):
    client = GitClient(repo_cache)
    result = repo_keyword_search(
        client,
        sample_repo["repo"],
        sample_repo["head"],
        ["FILE_SIZE_EXCEEDED"],
        scope="repo",
        maxMatches=10,
    )
    assert result["ok"] is True
    assert result["matches"]


def test_keyword_search_repository_and_all_aliases(repo_cache: Path, sample_repo):
    client = GitClient(repo_cache)
    for scope in ("repository", "all"):
        result = repo_keyword_search(
            client,
            sample_repo["repo"],
            sample_repo["head"],
            ["FILE_SIZE_EXCEEDED"],
            scope=scope,
            maxMatches=10,
        )
        assert result["ok"] is True
        assert result["matches"]


def test_keyword_search_paths_override_repo_alias(repo_cache: Path, sample_repo):
    client = GitClient(repo_cache)
    result = repo_keyword_search(
        client,
        sample_repo["repo"],
        sample_repo["head"],
        ["FILE_SIZE_EXCEEDED"],
        scope="repo",
        paths=["packages/fxp-ai"],
        maxMatches=10,
    )
    assert result["ok"] is True
    assert result["matches"]
    assert all(match["file"].startswith("packages/fxp-ai/") for match in result["matches"])


def test_keyword_search_unsupported_scope_still_errors(repo_cache: Path, sample_repo):
    client = GitClient(repo_cache)
    result = repo_keyword_search(
        client,
        sample_repo["repo"],
        sample_repo["head"],
        ["FILE_SIZE_EXCEEDED"],
        scope="unknown",
    )
    assert result["ok"] is False
    assert "unsupported scope" in result["error"]
