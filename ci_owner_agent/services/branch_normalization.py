from __future__ import annotations


def normalize_branch_name(value: str | None) -> str | None:
    """Return a logical branch name, never a Jenkins/Git ref or symbolic HEAD."""
    if value is None:
        return None
    branch = value.strip()
    if not branch:
        return None
    prefixes = ("refs/remotes/", "remotes/", "refs/heads/")
    for prefix in prefixes:
        if branch.startswith(prefix):
            branch = branch[len(prefix):]
            if prefix in {"refs/remotes/", "remotes/"} and "/" in branch:
                branch = branch.split("/", 1)[1]
            break
    else:
        if branch.startswith("*/"):
            branch = branch[2:]
        elif branch.startswith("refs/"):
            return None
        elif "/" in branch:
            remote, candidate = branch.split("/", 1)
            if remote in {"origin", "upstream"}:
                branch = candidate
    if not branch or branch == "HEAD" or branch.endswith("/HEAD"):
        return None
    return branch
