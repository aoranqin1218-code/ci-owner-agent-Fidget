"""Runtime conventions shared by command-line helper scripts."""
from __future__ import annotations

import os
import sys
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
