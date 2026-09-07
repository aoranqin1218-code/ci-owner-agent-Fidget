from __future__ import annotations

from collections import OrderedDict
from pathlib import Path

from ci_owner_agent.schemas import ChangedFile, CommitInfo, FileAuthor
from ci_owner_agent.services.command_runner import (
    CommandResult,
    run_command,
    truncate_text,
    validate_commit_ref,
    validate_git_path,
    validate_repo_name,
)


class GitClient:
    def __init__(self, repo_cache_dir: str | Path, max_output_chars: int = 20000, timeout: int = 30) -> None:
        self.repo_cache_dir = Path(repo_cache_dir)
        self.max_output_chars = max_output_chars
        self.timeout = timeout

    def _repo_error(self, repo: str) -> str | None:
        return validate_repo_name(repo)

    def resolve_repo(self, repo: str) -> tuple[Path | None, bool, str | None]:
        error = self._repo_error(repo)
        if error:
            return None, False, error
        regular = self.repo_cache_dir / repo
        bare = self.repo_cache_dir / f"{repo}.git"
        if bare.exists() and (bare / "HEAD").exists():
            return bare, True, None
        if regular.exists() and (regular / ".git").exists():
            return regular, False, None
        return (
            None,
            False,
            f"repo cache not found for {repo}; create {regular} or bare mirror {bare}",
        )

    def _git_dir_args(self, path: Path, bare: bool) -> list[str]:
        if bare:
            return ["git", f"--git-dir={path}"]
        return ["git", "-C", str(path)]

    def _run_git(self, repo: str, args: list[str]) -> tuple[CommandResult | None, str | None]:
        path, bare, error = self.resolve_repo(repo)
        if error or path is None:
            return None, error
        result = run_command(
            [*self._git_dir_args(path, bare), *args],
            timeout=self.timeout,
            max_output_chars=self.max_output_chars,
        )
        return result, None

    def sync(self, repo: str) -> dict:
        path, bare, error = self.resolve_repo(repo)
        if error or path is None:
            return {"ok": False, "error": error, "suggestion": "pre-create or mirror the repo under CI_AGENT_REPO_CACHE_DIR"}
        args = ["remote", "update", "--prune"] if bare else ["fetch", "origin", "--prune", "--no-tags"]
        result = run_command(
            [*self._git_dir_args(path, bare), *args],
            timeout=self.timeout,
            max_output_chars=self.max_output_chars,
        )
        return {"ok": result.ok, "repoPath": str(path), "bare": bare, "command": result.to_dict()}

    def check_ancestor(self, repo: str, base_commit: str, head_commit: str) -> dict:
        error = validate_commit_ref(base_commit) or validate_commit_ref(head_commit)
        if error:
            return {"ok": False, "isAncestor": False, "error": error}
        result, repo_error = self._run_git(repo, ["merge-base", "--is-ancestor", base_commit, head_commit])
        if repo_error:
            return {"ok": False, "isAncestor": False, "error": repo_error}
        if result is None:
            return {"ok": False, "isAncestor": False, "error": "git command failed"}
        if result.returncode == 0:
            return {"ok": True, "isAncestor": True}
        if result.returncode == 1:
            return {"ok": True, "isAncestor": False}
        return {"ok": False, "isAncestor": False, "error": result.error or result.stderr or "git merge-base failed", "command": result.to_dict()}

    def checkout_commit_for_analysis(self, repo: str, commit: str, force: bool = False) -> dict:
        error = validate_repo_name(repo) or validate_commit_ref(commit)
        if error:
            return {"ok": False, "error": error}
        path, bare, repo_error = self.resolve_repo(repo)
        if repo_error or path is None:
            return {"ok": False, "error": repo_error}
        if bare:
            return {"ok": False, "error": "checkout for analysis requires a normal working-tree repo, not a bare mirror"}
        status = run_command(
            ["git", "-C", str(path), "status", "--porcelain"],
            timeout=self.timeout,
            max_output_chars=self.max_output_chars,
        )
        if not status.ok:
            return {"ok": False, "error": "failed to inspect worktree status", "command": status.to_dict()}
        dirty_lines = [
            line
            for line in status.stdout.splitlines()
            if line.strip() and not line[3:].replace("\\", "/").startswith(".ci-owner-agent/")
        ]
        if dirty_lines and not force:
            return {
                "ok": False,
                "error": "worktree is not clean; refusing to checkout analysis commit without force=True",
                "status": "\n".join(dirty_lines),
                "repoPath": str(path),
            }
        checkout_args = ["git", "-C", str(path), "checkout", "--detach"]
        if force:
            checkout_args.append("--force")
        checkout_args.append(commit)
        checkout = run_command(
            checkout_args,
            timeout=self.timeout,
            max_output_chars=self.max_output_chars,
        )
        return {
            "ok": checkout.ok,
            "repoPath": str(path),
            "commit": commit,
            "force": force,
            "command": checkout.to_dict(),
            "error": None if checkout.ok else checkout.error,
        }

    def get_commits_between(self, repo: str, base_commit: str, head_commit: str) -> dict:
        error = validate_commit_ref(base_commit) or validate_commit_ref(head_commit)
        if error:
            return {"ok": False, "error": error, "commits": []}
        result, repo_error = self._run_git(
            repo,
            ["log", "--pretty=format:%H%x09%an%x09%ae%x09%at%x09%s", f"{base_commit}..{head_commit}"],
        )
        if repo_error:
            return {"ok": False, "error": repo_error, "commits": []}
        if result is None or not result.ok:
            return {"ok": False, "error": result.error if result else "git command failed", "command": result.to_dict() if result else None, "commits": []}
        commits = []
        for line in result.stdout.splitlines():
            parts = line.split("\t", 4)
            if len(parts) != 5:
                continue
            commit_hash, author_name, author_email, timestamp, subject = parts
            commits.append(
                CommitInfo(
                    hash=commit_hash,
                    authorName=author_name,
                    authorEmail=author_email,
                    timestamp=timestamp,
                    subject=subject,
                )
            )
        return {"ok": True, "commits": [item.model_dump() for item in commits]}

    def _authors_for_path(self, repo: str, base_commit: str, head_commit: str, path: str) -> list[FileAuthor]:
        result, _ = self._run_git(
            repo,
            ["log", "--pretty=format:%H%x09%an%x09%ae", f"{base_commit}..{head_commit}", "--", path],
        )
        if result is None or not result.ok:
            return []
        authors: OrderedDict[tuple[str, str], list[str]] = OrderedDict()
        for line in result.stdout.splitlines():
            parts = line.split("\t", 2)
            if len(parts) != 3:
                continue
            commit_hash, name, email = parts
            authors.setdefault((name, email), []).append(commit_hash)
        return [FileAuthor(name=name, email=email or None, commits=commits) for (name, email), commits in authors.items()]

    def get_diff_files(self, repo: str, base_commit: str, head_commit: str) -> dict:
        error = validate_commit_ref(base_commit) or validate_commit_ref(head_commit)
        if error:
            return {"ok": False, "error": error, "files": []}
        name_status, repo_error = self._run_git(repo, ["diff", "--name-status", base_commit, head_commit])
        if repo_error:
            return {"ok": False, "error": repo_error, "files": []}
        if name_status is None or not name_status.ok:
            return {"ok": False, "error": name_status.error if name_status else "git command failed", "command": name_status.to_dict() if name_status else None, "files": []}
        numstat, _ = self._run_git(repo, ["diff", "--numstat", base_commit, head_commit])
        stats: dict[str, tuple[int | None, int | None]] = {}
        if numstat is not None and numstat.ok:
            for line in numstat.stdout.splitlines():
                parts = line.split("\t")
                if len(parts) < 3:
                    continue
                additions = None if parts[0] == "-" else int(parts[0])
                deletions = None if parts[1] == "-" else int(parts[1])
                stats[parts[-1]] = (additions, deletions)
        files = []
        for line in name_status.stdout.splitlines():
            parts = line.split("\t")
            if not parts:
                continue
            raw_status = parts[0]
            status = raw_status[0]
            path = parts[-1]
            additions, deletions = stats.get(path, (None, None))
            authors = self._authors_for_path(repo, base_commit, head_commit, path)
            files.append(
                ChangedFile(
                    path=path,
                    status=status,
                    additions=additions,
                    deletions=deletions,
                    authors=authors,
                )
            )
        return {"ok": True, "files": [item.model_dump() for item in files]}

    def get_file_diff(self, repo: str, base_commit: str, head_commit: str, path: str, context_lines: int = 8) -> dict:
        error = validate_commit_ref(base_commit) or validate_commit_ref(head_commit) or validate_git_path(path)
        if error:
            return {"ok": False, "error": error, "path": path, "diff": "", "authors": []}
        context = max(0, min(context_lines, 100))
        result, repo_error = self._run_git(repo, ["diff", f"-U{context}", base_commit, head_commit, "--", path])
        if repo_error:
            return {"ok": False, "error": repo_error, "path": path, "diff": "", "authors": []}
        if result is None or not result.ok:
            return {"ok": False, "error": result.error if result else "git command failed", "command": result.to_dict() if result else None, "path": path, "diff": "", "authors": []}
        authors = self._authors_for_path(repo, base_commit, head_commit, path)
        diff, truncated = truncate_text(result.stdout, self.max_output_chars)
        return {"ok": True, "path": path, "diff": diff, "authors": [item.model_dump() for item in authors], "truncated": truncated}

    def get_file_content(self, repo: str, commit: str, path: str, start_line: int | None = None, end_line: int | None = None) -> dict:
        error = validate_commit_ref(commit) or validate_git_path(path)
        if error:
            return {"ok": False, "error": error, "path": path, "content": "", "truncated": False}
        result, repo_error = self._run_git(repo, ["show", f"{commit}:{path}"])
        if repo_error:
            return {"ok": False, "error": repo_error, "path": path, "content": "", "truncated": False}
        if result is None or not result.ok:
            response = {"ok": False, "error": result.error if result else "git command failed", "command": result.to_dict() if result else None, "path": path, "content": "", "truncated": False}
            stderr = result.stderr if result else ""
            stdout = result.stdout if result else ""
            if "does not exist" in stderr or "exists on disk, but not in" in stderr or "does not exist" in stdout:
                response["suggestion"] = (
                    "Path does not exist at this commit. Call repo_find_paths with a filename or directory fragment first, "
                    "then call repo_get_file_content using an exact returned path."
                )
            return response
        lines = result.stdout.splitlines()
        if start_line is not None or end_line is not None:
            start = max(1, start_line or 1)
            end = min(len(lines), end_line or len(lines))
            content = "\n".join(lines[start - 1 : end])
        else:
            start, end, content = 1, len(lines), result.stdout
        content, truncated = truncate_text(content, self.max_output_chars)
        return {"ok": True, "path": path, "startLine": start, "endLine": end, "content": content, "truncated": truncated}

    def get_file_commit_history(self, repo: str, head_commit: str, path: str) -> dict:
        """Return every trusted ancestor that changed ``path``, newest first.

        Version-baseline resolution deliberately reads this history from the
        Agent repository cache.  A bounded or malformed Git response is not
        enough evidence to pick a baseline, so it is reported as a failure
        rather than silently using the visible prefix.
        """
        error = validate_commit_ref(head_commit) or validate_git_path(path)
        if error:
            return {"ok": False, "error": error, "commits": []}
        result, repo_error = self._run_git(repo, ["log", "--format=%H", head_commit, "--", path])
        if repo_error:
            return {"ok": False, "error": repo_error, "commits": []}
        if result is None or not result.ok:
            return {
                "ok": False,
                "error": result.error if result else "git command failed",
                "command": result.to_dict() if result else None,
                "commits": [],
            }
        if result.truncated:
            return {
                "ok": False,
                "error": "git file history output was truncated",
                "command": result.to_dict(),
                "commits": [],
            }
        commits = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if any(validate_commit_ref(commit) for commit in commits):
            return {"ok": False, "error": "git file history contained an invalid commit", "commits": []}
        return {"ok": True, "commits": commits}

    def get_commit_changed_paths(self, repo: str, commit: str) -> dict:
        """Return all repository paths touched by one trusted commit."""
        error = validate_commit_ref(commit)
        if error:
            return {"ok": False, "error": error, "paths": []}
        result, repo_error = self._run_git(
            repo,
            ["show", "--pretty=format:", "--name-only", "--no-renames", commit],
        )
        if repo_error:
            return {"ok": False, "error": repo_error, "paths": []}
        if result is None or not result.ok or result.truncated:
            return {
                "ok": False,
                "error": (
                    "git commit path output was truncated"
                    if result is not None and result.truncated
                    else result.error if result else "git command failed"
                ),
                "command": result.to_dict() if result else None,
                "paths": [],
            }
        paths = list(dict.fromkeys(line.strip() for line in result.stdout.splitlines() if line.strip()))
        if any(validate_git_path(path) for path in paths):
            return {"ok": False, "error": "git commit contained an invalid path", "paths": []}
        return {"ok": True, "commit": commit, "paths": paths}

    def get_path_changes_between(
        self,
        repo: str,
        base_commit: str,
        head_commit: str,
        paths: list[str],
    ) -> dict:
        """Return every commit/path touch in ``base..head``, including delete/re-add cycles."""
        error = validate_commit_ref(base_commit) or validate_commit_ref(head_commit)
        if error:
            return {"ok": False, "error": error, "changes": [], "touchedPaths": []}
        normalized_paths = list(dict.fromkeys(path.replace("\\", "/") for path in paths if path))
        for path in normalized_paths:
            path_error = validate_git_path(path)
            if path_error:
                return {"ok": False, "error": path_error, "changes": [], "touchedPaths": []}
        if not normalized_paths:
            return {"ok": False, "error": "at least one path is required", "changes": [], "touchedPaths": []}
        result, repo_error = self._run_git(
            repo,
            [
                "log",
                "--format=commit:%H",
                "--name-only",
                "--no-renames",
                f"{base_commit}..{head_commit}",
                "--",
                *normalized_paths,
            ],
        )
        if repo_error:
            return {"ok": False, "error": repo_error, "changes": [], "touchedPaths": []}
        if result is None or not result.ok or result.truncated:
            return {
                "ok": False,
                "error": (
                    "git path change output was truncated"
                    if result is not None and result.truncated
                    else result.error if result else "git command failed"
                ),
                "command": result.to_dict() if result else None,
                "changes": [],
                "touchedPaths": [],
            }
        changes: list[dict[str, str]] = []
        current_commit: str | None = None
        for line in result.stdout.splitlines():
            value = line.strip()
            if not value:
                continue
            if value.startswith("commit:"):
                current_commit = value.removeprefix("commit:")
                if validate_commit_ref(current_commit):
                    return {"ok": False, "error": "git path history contained an invalid commit", "changes": [], "touchedPaths": []}
                continue
            if current_commit is None or validate_git_path(value):
                return {"ok": False, "error": "git path history contained malformed output", "changes": [], "touchedPaths": []}
            changes.append({"commit": current_commit, "path": value})
        touched_paths = list(dict.fromkeys(item["path"] for item in changes))
        return {"ok": True, "changes": changes, "touchedPaths": touched_paths}

    def list_tags(self, repo: str) -> dict:
        """List tag names without assuming normal repository sync fetched tags."""
        result, repo_error = self._run_git(repo, ["for-each-ref", "--format=%(refname:short)", "refs/tags"])
        if repo_error:
            return {"ok": False, "error": repo_error, "tags": []}
        if result is None or not result.ok:
            return {
                "ok": False,
                "error": result.error if result else "git command failed",
                "command": result.to_dict() if result else None,
                "tags": [],
            }
        if result.truncated:
            return {
                "ok": False,
                "error": "git tag list output was truncated",
                "command": result.to_dict(),
                "tags": [],
            }
        return {"ok": True, "tags": [line.strip() for line in result.stdout.splitlines() if line.strip()]}

    def peel_tag_to_commit(self, repo: str, tag: str) -> dict:
        """Resolve an existing tag to its commit, including annotated tags."""
        if not tag or "\x00" in tag or tag.startswith("-"):
            return {"ok": False, "error": "invalid git tag name", "commit": None}
        result, repo_error = self._run_git(repo, ["rev-parse", "--verify", f"refs/tags/{tag}^{{commit}}"])
        if repo_error:
            return {"ok": False, "error": repo_error, "commit": None}
        if result is None or not result.ok or result.truncated:
            return {
                "ok": False,
                "error": (
                    "git tag resolution output was truncated"
                    if result is not None and result.truncated
                    else result.error if result else "git command failed"
                ),
                "command": result.to_dict() if result else None,
                "commit": None,
            }
        commit = result.stdout.strip()
        if validate_commit_ref(commit):
            return {"ok": False, "error": "tag did not resolve to a valid commit", "commit": None}
        return {"ok": True, "tag": tag, "commit": commit}

    def get_merge_base(self, repo: str, left_commit: str, right_commit: str) -> dict:
        """Return one merge-base only when Git produced a complete commit SHA."""
        error = validate_commit_ref(left_commit) or validate_commit_ref(right_commit)
        if error:
            return {"ok": False, "error": error, "commit": None}
        result, repo_error = self._run_git(repo, ["merge-base", left_commit, right_commit])
        if repo_error:
            return {"ok": False, "error": repo_error, "commit": None}
        if result is None or not result.ok or result.truncated:
            return {
                "ok": False,
                "error": (
                    "git merge-base output was truncated"
                    if result is not None and result.truncated
                    else result.error if result else "git command failed"
                ),
                "command": result.to_dict() if result else None,
                "commit": None,
            }
        commit = result.stdout.strip()
        if validate_commit_ref(commit):
            return {"ok": False, "error": "git merge-base returned an invalid commit", "commit": None}
        return {"ok": True, "commit": commit}

    def list_paths(self, repo: str, commit: str, paths: list[str] | None = None) -> dict:
        error = validate_commit_ref(commit)
        if error:
            return {"ok": False, "error": error, "paths": []}
        for path in paths or []:
            path_error = validate_git_path(path)
            if path_error:
                return {"ok": False, "error": path_error, "paths": []}
        args = ["ls-tree", "-r", "--name-only", commit]
        if paths:
            args.extend(["--", *paths])
        result, repo_error = self._run_git(repo, args)
        if repo_error:
            return {"ok": False, "error": repo_error, "paths": []}
        if result is None or not result.ok:
            return {"ok": False, "error": result.error if result else "git command failed", "command": result.to_dict() if result else None, "paths": []}
        return {"ok": True, "paths": [line for line in result.stdout.splitlines() if line], "truncated": result.truncated}
