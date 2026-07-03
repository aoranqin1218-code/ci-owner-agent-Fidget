from __future__ import annotations

from ci_owner_agent.services.command_runner import validate_commit_ref, validate_git_path
from ci_owner_agent.services.git_client import GitClient


def repo_find_paths(
    client: GitClient,
    repo: str,
    commit: str,
    query: str | None = None,
    paths: list[str] | None = None,
    suffixes: list[str] | None = None,
    maxMatches: int = 50,
) -> dict:
    commit_error = validate_commit_ref(commit)
    if commit_error:
        return {"ok": False, "error": commit_error, "query": query, "paths": paths or [], "suffixes": suffixes or [], "matches": []}
    for path in paths or []:
        path_error = validate_git_path(path)
        if path_error:
            return {"ok": False, "error": path_error, "query": query, "paths": paths or [], "suffixes": suffixes or [], "matches": []}

    listed = client.list_paths(repo, commit, paths or [])
    if not listed.get("ok"):
        return {
            "ok": False,
            "error": listed.get("error"),
            "command": listed.get("command"),
            "query": query,
            "paths": paths or [],
            "suffixes": suffixes or [],
            "matches": [],
        }

    normalized_suffixes = [suffix.lower() for suffix in suffixes or [] if suffix]
    needle = (query or "").lower()
    matches: list[str] = []
    for item in listed.get("paths", []):
        path = str(item)
        basename = path.rsplit("/", 1)[-1]
        if needle and needle not in path.lower() and needle not in basename.lower():
            continue
        if normalized_suffixes and not any(path.lower().endswith(suffix) for suffix in normalized_suffixes):
            continue
        matches.append(path)

    limit = max(0, min(maxMatches, 500))
    return {
        "ok": True,
        "query": query,
        "paths": paths or [],
        "suffixes": suffixes or [],
        "matches": matches[:limit],
        "truncated": len(matches) > limit,
    }
