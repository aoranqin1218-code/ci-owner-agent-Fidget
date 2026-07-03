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
    requested_paths = paths or []
    for path in requested_paths:
        path_error = validate_git_path(path)
        if path_error:
            return {"ok": False, "error": path_error, "query": query, "paths": requested_paths, "suffixes": suffixes or [], "matches": []}

    if query and "/" in query and not requested_paths:
        query_error = validate_git_path(query)
        if query_error:
            return {"ok": False, "error": query_error, "query": query, "paths": requested_paths, "suffixes": suffixes or [], "matches": []}
        exact = client.list_paths(repo, commit, [query])
        if not exact.get("ok"):
            return {
                "ok": False,
                "error": exact.get("error"),
                "command": exact.get("command"),
                "query": query,
                "paths": requested_paths,
                "suffixes": suffixes or [],
                "matches": [],
            }
        exact_matches = [path for path in exact.get("paths", []) if path == query and _suffix_allowed(path, suffixes or [])]
        if exact_matches:
            return {
                "ok": True,
                "query": query,
                "paths": requested_paths,
                "suffixes": suffixes or [],
                "matches": exact_matches[: max(0, min(maxMatches, 500))],
                "truncated": exact.get("truncated", False) or len(exact_matches) > max(0, min(maxMatches, 500)),
            }
        dirname, basename = query.rsplit("/", 1)
        requested_paths = [dirname]
        query = basename

    listed = client.list_paths(repo, commit, requested_paths)
    if not listed.get("ok"):
        return {
            "ok": False,
            "error": listed.get("error"),
            "command": listed.get("command"),
            "query": query,
            "paths": requested_paths,
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
    source_truncated = bool(listed.get("truncated"))
    truncated = source_truncated or len(matches) > limit
    suggestion = None
    if source_truncated:
        suggestion = "Path listing was truncated; pass paths=[...] to narrow the search and avoid false empty matches."
    return {
        "ok": True,
        "query": query,
        "paths": requested_paths,
        "suffixes": suffixes or [],
        "matches": matches[:limit],
        "truncated": truncated,
        **({"suggestion": suggestion} if suggestion else {}),
    }


def _suffix_allowed(path: str, suffixes: list[str]) -> bool:
    normalized_suffixes = [suffix.lower() for suffix in suffixes if suffix]
    return not normalized_suffixes or any(path.lower().endswith(suffix) for suffix in normalized_suffixes)
