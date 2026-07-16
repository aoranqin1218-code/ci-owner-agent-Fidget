"""Runtime conventions shared by command-line helper scripts."""
from __future__ import annotations

import os
import platform
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
INVOCATION_CWD = Path.cwd()


def ensure_repo_on_sys_path() -> None:
    root = str(REPO_ROOT)
    if root in sys.path:
        sys.path.remove(root)
    sys.path.insert(0, root)


def build_subprocess_env(base: dict[str, str] | None = None) -> dict[str, str]:
    env = (base or os.environ).copy()
    root = str(REPO_ROOT)
    previous = env.get("PYTHONPATH")
    env["PYTHONPATH"] = root if not previous else root + os.pathsep + previous
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def resolve_user_path(value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else INVOCATION_CWD / path


def resolve_repo_default_path(value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else REPO_ROOT / path


@dataclass(frozen=True)
class ProcessTerminationResult:
    reaped: bool
    warning: str | None = None


class BoundedProcessTimeout(subprocess.TimeoutExpired):
    def __init__(self, cmd, timeout, *, output: str, stderr: str, termination: ProcessTerminationResult):
        super().__init__(cmd, timeout, output=output, stderr=stderr)
        self.termination = termination


def _text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value


def run_process_bounded(command: list[str], *, cwd: Path, env: dict[str, str], timeout_seconds: float, grace_seconds: float = 5) -> subprocess.CompletedProcess[str]:
    windows = platform.system().lower().startswith("win")
    process = subprocess.Popen(command, cwd=str(cwd), env=env, text=True, encoding="utf-8", errors="replace",
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if windows else 0,
                               start_new_session=not windows)
    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    except subprocess.TimeoutExpired as initial:
        warnings: list[str] = []
        if windows:
            try:
                killed = subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, text=True,
                                        encoding="utf-8", errors="replace", timeout=grace_seconds, check=False)
                if killed.returncode != 0:
                    warnings.append(f"taskkill failed ({killed.returncode}): {killed.stderr.strip()}")
            except Exception as exc:
                warnings.append(f"taskkill failed: {type(exc).__name__}: {exc}")
        else:
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
            except (PermissionError, OSError) as exc:
                warnings.append(f"tree termination failed: {type(exc).__name__}: {exc}")
        reaped = False
        stdout = _text(initial.output)
        stderr = _text(initial.stderr)
        try:
            out2, err2 = process.communicate(timeout=grace_seconds)
            stdout, stderr, reaped = _text(out2) or stdout, _text(err2) or stderr, True
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except (ProcessLookupError, ChildProcessError):
                pass
            except Exception as exc:
                warnings.append(f"direct kill failed: {type(exc).__name__}: {exc}")
            try:
                out3, err3 = process.communicate(timeout=grace_seconds)
                stdout, stderr, reaped = _text(out3) or stdout, _text(err3) or stderr, True
            except subprocess.TimeoutExpired as exc:
                warnings.append(f"final reap timed out: {exc}")
            except Exception as exc:
                warnings.append(f"final reap failed: {type(exc).__name__}: {exc}")
        raise BoundedProcessTimeout(command, timeout_seconds, output=stdout, stderr=stderr,
                                    termination=ProcessTerminationResult(reaped, "; ".join(warnings) or None)) from initial
