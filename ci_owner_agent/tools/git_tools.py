from __future__ import annotations

from ci_owner_agent.services.git_client import GitClient


def repo_sync(client: GitClient, repo: str) -> dict:
    return client.sync(repo)


def repo_get_commits_between(client: GitClient, repo: str, baseCommit: str, headCommit: str) -> dict:
    return client.get_commits_between(repo, baseCommit, headCommit)


def repo_get_diff_files(client: GitClient, repo: str, baseCommit: str, headCommit: str) -> dict:
    return client.get_diff_files(repo, baseCommit, headCommit)


def repo_get_file_diff(
    client: GitClient,
    repo: str,
    baseCommit: str,
    headCommit: str,
    path: str,
    contextLines: int = 8,
) -> dict:
    return client.get_file_diff(repo, baseCommit, headCommit, path, contextLines)


def repo_get_file_content(
    client: GitClient,
    repo: str,
    commit: str,
    path: str,
    startLine: int | None = None,
    endLine: int | None = None,
) -> dict:
    return client.get_file_content(repo, commit, path, startLine, endLine)
