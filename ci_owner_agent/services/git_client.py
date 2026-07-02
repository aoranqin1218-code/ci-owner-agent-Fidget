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
            return {"ok": False, "error": result.error if result else "git command failed", "command": result.to_dict() if result else None, "path": path, "content": "", "truncated": False}
        lines = result.stdout.splitlines()
        if start_line is not None or end_line is not None:
            start = max(1, start_line or 1)
            end = min(len(lines), end_line or len(lines))
            content = "\n".join(lines[start - 1 : end])
        else:
            start, end, content = 1, len(lines), result.stdout
        content, truncated = truncate_text(content, self.max_output_chars)
        return {"ok": True, "path": path, "startLine": start, "endLine": end, "content": content, "truncated": truncated}
