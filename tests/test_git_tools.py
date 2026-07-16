from __future__ import annotations

from pathlib import Path

from ci_owner_agent.services.command_runner import run_command
from ci_owner_agent.services.git_client import GitClient
from ci_owner_agent.tools.git_tools import repo_get_diff_files, repo_get_file_content, repo_get_file_diff


def test_git_diff_files_numstat_and_authors(repo_cache: Path, sample_repo):
    client = GitClient(repo_cache)
    result = repo_get_diff_files(client, sample_repo["repo"], sample_repo["base"], sample_repo["head"])
    assert result["ok"] is True
    files = result["files"]
    assert files[0]["path"] == "packages/fxp-ai/errors/classify.ts"
    assert files[0]["status"] == "M"
    assert files[0]["additions"] is not None
    assert files[0]["authors"][0]["name"] == "Zhang San"


def test_git_file_diff_and_content(repo_cache: Path, sample_repo):
    client = GitClient(repo_cache, max_output_chars=1000)
    diff = repo_get_file_diff(
        client,
        sample_repo["repo"],
        sample_repo["base"],
        sample_repo["head"],
        "packages/fxp-ai/errors/classify.ts",
    )
    assert diff["ok"] is True
    assert "limit = 10" in diff["diff"]

    content = repo_get_file_content(
        client,
        sample_repo["repo"],
        sample_repo["head"],
        "packages/fxp-ai/errors/classify.ts",
        1,
        2,
    )
    assert content["ok"] is True
    assert content["startLine"] == 1
    assert "limit" in content["content"]


def test_git_failure_returns_structured_error(repo_cache: Path, sample_repo):
    client = GitClient(repo_cache)
    result = repo_get_diff_files(client, sample_repo["repo"], "bad ref", sample_repo["head"])
    assert result["ok"] is False
    assert "error" in result


def test_command_output_is_truncated():
    result = run_command(["python", "-c", "print('x' * 1000)"], max_output_chars=100)
    assert result.truncated is True
    assert len(result.stdout) <= 100 + len("\n...[truncated]...")

def test_check_ancestor_uses_supplied_jenkins_commits(repo_cache, sample_repo):
    client = GitClient(repo_cache)
    assert client.check_ancestor(sample_repo["repo"], sample_repo["base"], sample_repo["head"]) == {"ok": True, "isAncestor": True}
    assert client.check_ancestor(sample_repo["repo"], sample_repo["head"], sample_repo["base"]) == {"ok": True, "isAncestor": False}
