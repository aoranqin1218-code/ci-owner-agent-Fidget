from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


DEFAULT_TIMEOUT_SECONDS = 30
DEFAULT_MAX_OUTPUT_CHARS = 20000
COMMIT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/\-]{0,255}$")
REPO_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,127}$")


@dataclass(frozen=True)
class CommandResult:
    ok: bool
    args: list[str]
    returncode: int | None
    stdout: str
    stderr: str
    error: str | None = None
    timedOut: bool = False
    truncated: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "args": self.args,
            "returncode": self.returncode,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "error": self.error,
            "timedOut": self.timedOut,
            "truncated": self.truncated,
        }


def truncate_text(text: str, max_chars: int = DEFAULT_MAX_OUTPUT_CHARS) -> tuple[str, bool]:
    if max_chars <= 0 or len(text) <= max_chars:
        return text, False
    marker = "\n...[truncated]..."
    keep = max(0, max_chars - len(marker))
    return text[:keep] + marker, True


def truncate_tail_text(text: str, max_chars: int = DEFAULT_MAX_OUTPUT_CHARS) -> tuple[str, bool]:
    if max_chars <= 0 or len(text) <= max_chars:
        return text, False
    marker = "...[truncated head]...\n"
    keep = max(0, max_chars - len(marker))
    return marker + text[-keep:], True


def run_command(
    args: Sequence[str],
    cwd: str | Path | None = None,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    max_output_chars: int = DEFAULT_MAX_OUTPUT_CHARS,
) -> CommandResult:
    safe_args = [str(arg) for arg in args]
    try:
        completed = subprocess.run(
            safe_args,
            cwd=str(cwd) if cwd is not None else None,
            shell=False,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        stdout, out_truncated = truncate_text(exc.stdout or "", max_output_chars)
        stderr, err_truncated = truncate_text(exc.stderr or "", max_output_chars)
        return CommandResult(
            ok=False,
            args=safe_args,
            returncode=None,
            stdout=stdout,
            stderr=stderr,
            error=f"command timed out after {timeout}s",
            timedOut=True,
            truncated=out_truncated or err_truncated,
        )
    except FileNotFoundError as exc:
        return CommandResult(
            ok=False,
            args=safe_args,
            returncode=None,
            stdout="",
            stderr="",
            error=f"command not found: {exc.filename}",
        )
    except OSError as exc:
        return CommandResult(
            ok=False,
            args=safe_args,
            returncode=None,
            stdout="",
            stderr="",
            error=str(exc),
        )

    stdout, out_truncated = truncate_text(completed.stdout, max_output_chars)
    stderr, err_truncated = truncate_text(completed.stderr, max_output_chars)
    return CommandResult(
        ok=completed.returncode == 0,
        args=safe_args,
        returncode=completed.returncode,
        stdout=stdout,
        stderr=stderr,
        error=None if completed.returncode == 0 else "command failed",
        truncated=out_truncated or err_truncated,
    )


def validate_repo_name(repo: str) -> str | None:
    if not REPO_NAME_RE.match(repo):
        return "repo must be a simple cache name, not a path or URL"
    return None


def validate_commit_ref(ref: str) -> str | None:
    if not ref or ref.startswith("-") or not COMMIT_RE.match(ref):
        return "commit/ref contains unsupported characters"
    return None


def validate_git_path(path: str) -> str | None:
    if not path or "\x00" in path:
        return "path is empty or contains NUL"
    normalized = path.replace("\\", "/")
    if normalized.startswith("/") or normalized.startswith("../") or "/../" in normalized:
        return "path must be relative and stay inside the repository"
    return None
