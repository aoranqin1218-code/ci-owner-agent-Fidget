from __future__ import annotations


def normalize_branch_name(value: str | None) -> str | None:
    """Return a logical branch name, never a Jenkins/Git ref or symbolic HEAD."""
    if value is None:
        return None
    branch = value.strip()
    if not branch:
        return None
    while True:
        if branch == "HEAD" or branch.endswith("/HEAD") or branch.startswith("refs/tags/") or branch.startswith("refs/pull/"):
            return None
        previous = branch
        if branch.startswith("refs/remotes/") or branch.startswith("remotes/"):
            prefix = "refs/remotes/" if branch.startswith("refs/remotes/") else "remotes/"
            remainder = branch[len(prefix):]
            if "/" not in remainder:
                return None
            _, branch = remainder.split("/", 1)
        elif branch.startswith("refs/heads/"):
            branch = branch[len("refs/heads/"):]
        elif branch.startswith("*/"):
            branch = branch[2:]
        elif branch.startswith("origin/") or branch.startswith("upstream/"):
            branch = branch.split("/", 1)[1]
        elif branch.startswith("refs/"):
            return None
        if not branch or branch == previous:
            break
    return None if branch == "HEAD" or branch.endswith("/HEAD") else branch
