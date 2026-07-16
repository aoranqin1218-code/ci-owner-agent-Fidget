"""Runtime conventions shared by command-line helper scripts."""
from __future__ import annotations

import os
import hashlib
import json
import platform
import signal
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, ValidationError, model_validator

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


def success_marker_path(notice_path: Path) -> Path:
    return notice_path.with_name(notice_path.name + ".success.json")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_success_marker_atomic(marker_path: Path, payload: dict) -> None:
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    temp = marker_path.with_name(f".{marker_path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temp.open("w", encoding="utf-8", newline="\n") as file:
            json.dump(payload, file, ensure_ascii=False, sort_keys=True, indent=2)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp, marker_path)
    finally:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass


Sha256Text = Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]


class ResumeSuccessMarker(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schemaVersion: StrictInt
    kind: StrictStr
    workflow: Literal["company", "jenkins"]
    noticeFile: StrictStr
    noticeSha256: Sha256Text
    returnCode: StrictInt
    repo: StrictStr
    job: StrictStr
    buildNumber: StrictInt
    completedAt: StrictStr
    branch: StrictStr | None = None
    result: StrictStr | None = None
    baseCommit: StrictStr | None = None
    headCommit: StrictStr | None = None

    @model_validator(mode="after")
    def require_company_metadata(self):
        if self.workflow == "company":
            missing = [
                field for field in ("branch", "result", "baseCommit", "headCommit")
                if getattr(self, field) is None
            ]
            if missing:
                raise ValueError(f"company success marker missing metadata: {', '.join(missing)}")
        return self


def load_success_marker(marker_path: Path) -> ResumeSuccessMarker:
    value = json.loads(marker_path.read_text(encoding="utf-8"))
    return ResumeSuccessMarker.model_validate(value, strict=True)


def validate_success_marker(marker_path: Path, notice_path: Path, expected: dict) -> tuple[bool, str | None]:
    try:
        notice_exists = notice_path.exists()
    except Exception as exc:
        return False, f"failed to inspect notice for success marker: {type(exc).__name__}: {exc}"
    if not notice_exists:
        return False, "notice missing for success marker"
    try:
        marker_exists = marker_path.exists()
    except Exception as exc:
        return False, f"failed to inspect success marker: {type(exc).__name__}: {exc}"
    if not marker_exists:
        return False, "success marker missing"
    try:
        marker = load_success_marker(marker_path)
    except json.JSONDecodeError as exc:
        return False, f"invalid success marker JSON: {type(exc).__name__}: {exc}"
    except ValidationError as exc:
        return False, f"invalid success marker schema: {type(exc).__name__}: {exc}"
    except Exception as exc:
        return False, f"failed to read success marker: {type(exc).__name__}: {exc}"
    required = {"schemaVersion": 1, "kind": "ci-owner-agent-resume-success", "returnCode": 0,
                "noticeFile": notice_path.name, **expected}
    for field, value in required.items():
        if getattr(marker, field) != value:
            return False, f"success marker metadata mismatch: {field}"
    try:
        notice_digest = sha256_file(notice_path)
    except Exception as exc:
        return False, f"failed to hash notice for success marker: {type(exc).__name__}: {exc}"
    if marker.noticeSha256 != notice_digest:
        return False, "success marker notice digest mismatch"
    return True, None


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
    creationflags = int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)) if windows else 0
    process = subprocess.Popen(command, cwd=str(cwd), env=env, text=True, encoding="utf-8", errors="replace",
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               creationflags=creationflags,
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
        grace_failed = False
        try:
            out2, err2 = process.communicate(timeout=grace_seconds)
            stdout, stderr, reaped = _text(out2) or stdout, _text(err2) or stderr, True
        except subprocess.TimeoutExpired as exc:
            warnings.append(f"grace reap timed out: {exc}")
            grace_failed = True
        except Exception as exc:
            warnings.append(f"grace reap failed: {type(exc).__name__}: {exc}")
            grace_failed = True
        if grace_failed:
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
