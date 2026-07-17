from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_SCRIPT_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_SCRIPT_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_REPO_ROOT))

from scripts._runtime import (
    BoundedProcessTimeout,
    REPO_ROOT,
    build_subprocess_env,
    resolve_user_path,
    run_process_bounded,
)
from ci_owner_agent.config import load_settings


def build_weekly_report_command(
    *,
    python_executable: str,
    repo: str,
    jobs: list[str],
    branches: list[str],
    config_file: str | None,
    top: int | None,
    dry_run: bool,
    force: bool,
) -> list[str]:
    command = [
        python_executable,
        "-m",
        "ci_owner_agent",
        "weekly-test-report",
        "--repo",
        repo,
        "--period",
        "previous-week",
    ]
    for job in jobs:
        command.extend(["--job", job])
    for branch in branches:
        command.extend(["--branch", branch])
    if config_file:
        command.extend(["--config-file", config_file])
    if top is not None:
        command.extend(["--top", str(top)])
    if dry_run:
        command.append("--dry-run")
    else:
        command.append("--notify")
    if force:
        command.append("--force")
    return command


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Send the previous week's high-frequency Jenkins failure report")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--job", action="append", default=[])
    parser.add_argument("--branch", action="append", default=[])
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--env-file", default=None)
    parser.add_argument("--config-file", default=None)
    parser.add_argument("--top", type=int, default=None)
    parser.add_argument("--timeout-seconds", type=int, default=300)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)

    if args.top is not None and args.top <= 0:
        parser.error("--top must be positive")
    if args.timeout_seconds <= 0:
        parser.error("--timeout-seconds must be positive")

    env_file = resolve_user_path(args.env_file) if args.env_file else REPO_ROOT / ".env"
    config_file = str(resolve_user_path(args.config_file)) if args.config_file else None
    os.chdir(REPO_ROOT)
    try:
        settings = load_settings(env_file)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    if not settings.history_enabled:
        print("ERROR: CI_AGENT_HISTORY_ENABLED=true is required", file=sys.stderr)
        return 2

    command = build_weekly_report_command(
        python_executable=args.python,
        repo=args.repo,
        jobs=args.job,
        branches=args.branch,
        config_file=config_file,
        top=args.top,
        dry_run=args.dry_run,
        force=args.force,
    )
    try:
        completed = run_process_bounded(
            command,
            cwd=REPO_ROOT,
            env=build_subprocess_env(),
            timeout_seconds=args.timeout_seconds,
        )
    except BoundedProcessTimeout as exc:
        if exc.stdout:
            print(exc.stdout, end="" if str(exc.stdout).endswith("\n") else "\n")
        if exc.stderr:
            print(exc.stderr, file=sys.stderr, end="" if str(exc.stderr).endswith("\n") else "\n")
        print(f"ERROR: weekly report timed out after {args.timeout_seconds} seconds", file=sys.stderr)
        return 1

    if completed.stdout:
        print(completed.stdout, end="" if completed.stdout.endswith("\n") else "\n")
    if completed.stderr:
        print(completed.stderr, file=sys.stderr, end="" if completed.stderr.endswith("\n") else "\n")
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
