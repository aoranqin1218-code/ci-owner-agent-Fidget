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
