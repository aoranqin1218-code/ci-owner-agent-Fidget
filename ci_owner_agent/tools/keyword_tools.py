from __future__ import annotations

from ci_owner_agent.schemas import KeywordMatch
from ci_owner_agent.services.command_runner import validate_commit_ref, validate_git_path
from ci_owner_agent.services.git_client import GitClient


def _changed_paths(client: GitClient, repo: str, base_commit: str | None, head_commit: str | None) -> tuple[list[str], str | None]:
    if not base_commit or not head_commit:
        return [], "baseCommit and headCommit are required for changed_files scope"
    diff = client.get_diff_files(repo, base_commit, head_commit)
    if not diff.get("ok"):
        return [], str(diff.get("error") or "failed to get changed files")
    return [item["path"] for item in diff.get("files", []) if item.get("status") != "D"], None


def repo_keyword_search(
    client: GitClient,
    repo: str,
    commit: str,
    keywords: list[str],
    scope: str = "changed_files",
    baseCommit: str | None = None,
    headCommit: str | None = None,
    paths: list[str] | None = None,
    maxMatches: int = 50,
) -> dict:
    if not keywords:
        return {"ok": True, "matches": []}
    error = validate_commit_ref(commit)
    if error:
        return {"ok": False, "error": error, "matches": []}

    search_paths: list[str] = []
    if scope == "changed_files":
        search_paths, path_error = _changed_paths(client, repo, baseCommit, headCommit)
        if path_error:
            return {"ok": False, "error": path_error, "matches": []}
    elif scope == "paths":
        search_paths = paths or []
    elif scope == "whole_repo":
        search_paths = []
    else:
        return {"ok": False, "error": f"unsupported scope: {scope}", "matches": []}

    for path in search_paths:
        path_error = validate_git_path(path)
        if path_error:
            return {"ok": False, "error": path_error, "matches": []}

    matches: list[KeywordMatch] = []
    limit = max(0, maxMatches)
    changed_set = set(search_paths)
    for keyword in [item for item in keywords if item]:
        args = ["grep", "-n", "-I", "-i", keyword, commit, "--", *search_paths]
        result, repo_error = client._run_git(repo, args)
        if repo_error:
            return {"ok": False, "error": repo_error, "matches": [item.model_dump() for item in matches]}
        if result is None:
            continue
        if not result.ok and result.returncode not in (1,):
            return {"ok": False, "error": result.error, "command": result.to_dict(), "matches": [item.model_dump() for item in matches]}
        for line in result.stdout.splitlines():
            if line.startswith(f"{commit}:"):
                line = line[len(commit) + 1 :]
            if ":" not in line:
                continue
            file_part, line_part, text = line.split(":", 2)
            try:
                line_no = int(line_part)
            except ValueError:
                continue
            matches.append(
                KeywordMatch(
                    keyword=keyword,
                    file=file_part,
                    line=line_no,
                    text=text.strip(),
                    changedInRange=scope == "changed_files" or file_part in changed_set,
                )
            )
            if len(matches) >= limit:
                return {"ok": True, "matches": [item.model_dump() for item in matches]}
    return {"ok": True, "matches": [item.model_dump() for item in matches]}
