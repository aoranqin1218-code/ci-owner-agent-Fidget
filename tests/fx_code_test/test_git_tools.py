from __future__ import annotations

import subprocess
from pathlib import Path

from ci_owner_agent.services.command_runner import run_command
from ci_owner_agent.services.git_client import GitClient
from ci_owner_agent.services.history_inheritance import assess_failure_chain_continuity


def test_git_diff_files_numstat_and_authors(repo_cache: Path, sample_repo):
    client = GitClient(repo_cache)
    result = client.get_diff_files(sample_repo["repo"], sample_repo["base"], sample_repo["head"])
    assert result["ok"] is True
    files = result["files"]
    assert files[0]["path"] == "packages/fxp-ai/errors/classify.ts"
    assert files[0]["status"] == "M"
    assert files[0]["additions"] is not None
    assert files[0]["authors"][0]["name"] == "Zhang San"


def test_git_file_diff_and_content(repo_cache: Path, sample_repo):
    client = GitClient(repo_cache, max_output_chars=1000)
    diff = client.get_file_diff(
        sample_repo["repo"],
        sample_repo["base"],
        sample_repo["head"],
        "packages/fxp-ai/errors/classify.ts",
    )
    assert diff["ok"] is True
    assert "limit = 10" in diff["diff"]

    content = client.get_file_content(
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
    result = client.get_diff_files(sample_repo["repo"], "bad ref", sample_repo["head"])
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


def test_git_path_history_detects_delete_and_readd_even_when_net_diff_is_empty(repo_cache: Path, sample_repo):
    repo = Path(sample_repo["path"])
    path = "packages/fxp-ai/errors/classify.ts"
    target = repo / path
    original = target.read_text(encoding="utf-8")
    target.unlink()
    subprocess.run(["git", "-C", str(repo), "add", path], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "remove classify"], check=True)
    target.write_text(original, encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", path], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "restore classify"], check=True)
    current = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    client = GitClient(repo_cache)

    source_paths = client.get_commit_changed_paths(sample_repo["repo"], sample_repo["head"])
    history = client.get_path_changes_between(
        sample_repo["repo"],
        sample_repo["head"],
        current,
        [path],
    )
    net_diff = client.get_diff_files(sample_repo["repo"], sample_repo["head"], current)
    continuity = assess_failure_chain_continuity(
        git_client=client,
        repo=sample_repo["repo"],
        current_build_number=3,
        previous_build_number=2,
        current_head_commit=current,
        historical_build_number=2,
        historical_head_commit=sample_repo["head"],
        source_commit=sample_repo["head"],
        relevant_paths=[path],
    )

    assert source_paths["ok"] is True
    assert path in source_paths["paths"]
    assert history["ok"] is True
    assert history["touchedPaths"] == [path]
    assert len(history["changes"]) == 2
    assert net_diff["files"] == []
    assert continuity["continuityEligible"] is False
    assert continuity["continuityTouchedPaths"] == [path]
