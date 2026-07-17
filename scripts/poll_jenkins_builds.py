from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

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
from ci_owner_agent.services.history_store import get_history_store
from ci_owner_agent.services.jenkins_client import JenkinsClient

try:
    import fcntl
except ImportError:  # pragma: no cover - Ubuntu production has fcntl.
    fcntl = None


def _emit(event: str, **fields: Any) -> None:
    payload = {
        "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
        "event": event,
        **fields,
    }
    print(json.dumps(payload, ensure_ascii=False, default=str), flush=True)


def _slug(value: str) -> str:
    return "".join(char if char.isalnum() or char in {"-", "_", "."} else "-" for char in value).strip("-") or "default"


@contextmanager
def single_instance_lock(path: Path) -> Iterator[bool]:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+", encoding="utf-8")
    acquired = True
    try:
        if fcntl is not None:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                acquired = False
        yield acquired
    finally:
        if acquired and fcntl is not None:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        handle.close()


def is_build_analyzed(store: Any, *, repo: str, job: str, build_number: int) -> bool:
    """Require both final build and notice records, not a partial or notification-only snapshot."""
    notice = store.notices.find_one(
        {
            "repo": repo,
            "job": job,
            "buildNumber": build_number,
            "analyzedAt": {"$exists": True},
        }
    )
    if not notice:
        return False
    return store.builds.find_one(
        {
            "repo": repo,
            "job": job,
            "branch": notice.get("branch"),
            "buildNumber": build_number,
            "analyzedAt": {"$exists": True},
        }
    ) is not None


def candidate_build_numbers(latest_completed: int, lookback_builds: int) -> list[int]:
    if latest_completed <= 0:
        return []
    first = max(1, latest_completed - lookback_builds + 1)
    return list(range(first, latest_completed + 1))


def build_analyze_command(
    *,
    python_executable: str,
    repo: str,
    job: str,
    build_number: int,
    log_tail_lines: int,
    notify: bool,
    notify_dry_run: bool,
    force_notify: bool,
) -> list[str]:
    command = [
        python_executable,
        "-m",
        "ci_owner_agent",
        "analyze",
        "--repo",
        repo,
        "--job",
        job,
        "--build",
        str(build_number),
        "--log-tail-lines",
        str(log_tail_lines),
    ]
    if notify:
        command.append("--notify")
    if notify_dry_run:
        command.append("--notify-dry-run")
    if force_notify:
        command.append("--force-notify")
    return command


def _tail(text: str | None, limit: int = 2000) -> str | None:
    value = (text or "").strip()
    return value[-limit:] if value else None


def _positive(parser: argparse.ArgumentParser, name: str, value: int) -> None:
    if value <= 0:
        parser.error(f"{name} must be positive")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Poll completed Jenkins builds and analyze builds not yet stored in MongoDB")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--job", required=True)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--env-file", default=None)
    parser.add_argument("--lookback-builds", type=int, default=50)
    parser.add_argument("--max-builds-per-run", type=int, default=3)
    parser.add_argument("--log-tail-lines", type=int, default=None)
    parser.add_argument("--jenkins-timeout-seconds", type=int, default=20)
    parser.add_argument("--analysis-timeout-seconds", type=int, default=900)
    parser.add_argument("--lock-file", default=None)
    parser.add_argument("--notify", action="store_true")
    parser.add_argument("--notify-dry-run", action="store_true")
    parser.add_argument("--force-notify", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    _positive(parser, "--lookback-builds", args.lookback_builds)
    _positive(parser, "--max-builds-per-run", args.max_builds_per_run)
    _positive(parser, "--jenkins-timeout-seconds", args.jenkins_timeout_seconds)
    _positive(parser, "--analysis-timeout-seconds", args.analysis_timeout_seconds)
    if args.log_tail_lines is not None:
        _positive(parser, "--log-tail-lines", args.log_tail_lines)

    env_file = resolve_user_path(args.env_file) if args.env_file else REPO_ROOT / ".env"
    lock_file = (
        resolve_user_path(args.lock_file)
        if args.lock_file
        else REPO_ROOT / "runs" / "locks" / f"jenkins-poll-{_slug(args.repo)}-{_slug(args.job)}.lock"
    )

    os.chdir(REPO_ROOT)
    try:
        settings = load_settings(env_file)
    except ValueError as exc:
        _emit("configuration_error", error=str(exc))
        return 2
    if not settings.jenkins_url:
        _emit("configuration_error", error="JENKINS_URL is required")
        return 2
    if not settings.history_enabled:
        _emit("configuration_error", error="CI_AGENT_HISTORY_ENABLED=true is required for analysis deduplication")
        return 2

    with single_instance_lock(lock_file) as acquired:
        if not acquired:
            _emit("poll_skipped", reason="another poll process holds the lock", lockFile=str(lock_file))
            return 0

        store = get_history_store(settings)
        if store is None:
            _emit("configuration_error", error="MongoDB history store is unavailable")
            return 2
        try:
            admin = getattr(store.client, "admin", None)
            if admin is not None:
                admin.command("ping")
        except Exception as exc:
            _emit("history_error", error=f"{type(exc).__name__}: {exc}")
            return 2

        jenkins = JenkinsClient(
            base_url=settings.jenkins_url,
            user=settings.jenkins_user,
            token=settings.jenkins_token,
            timeout=args.jenkins_timeout_seconds,
            max_output_chars=settings.max_tool_output_chars,
        )
        latest_result = jenkins.get_build_json(args.job, "lastCompletedBuild")
        if not latest_result.get("ok"):
            _emit("jenkins_error", operation="lastCompletedBuild", error=latest_result.get("error"))
            return 1
        try:
            latest_completed = int(latest_result["data"]["number"])
        except (KeyError, TypeError, ValueError):
            _emit("jenkins_error", operation="lastCompletedBuild", error="completed build number is missing")
            return 1

        stats = {
            "latestCompletedBuild": latest_completed,
            "scanned": 0,
            "alreadyAnalyzed": 0,
            "notCompleted": 0,
            "preflightErrors": 0,
            "attempted": 0,
            "succeeded": 0,
            "failed": 0,
            "dryRun": bool(args.dry_run),
        }
        log_tail_lines = args.log_tail_lines or settings.default_log_tail_lines
        child_env = build_subprocess_env()

        for build_number in candidate_build_numbers(latest_completed, args.lookback_builds):
            stats["scanned"] += 1
            if is_build_analyzed(store, repo=args.repo, job=args.job, build_number=build_number):
                stats["alreadyAnalyzed"] += 1
                _emit("build_skipped", buildNumber=build_number, reason="already analyzed")
                continue
            if stats["attempted"] >= args.max_builds_per_run:
                break

            preflight = jenkins.get_build_json(args.job, build_number)
            if not preflight.get("ok"):
                stats["preflightErrors"] += 1
                _emit("build_skipped", buildNumber=build_number, reason="Jenkins build metadata unavailable", error=preflight.get("error"))
                continue
            build_data = preflight.get("data") or {}
            if build_data.get("building") or build_data.get("result") is None:
                stats["notCompleted"] += 1
                _emit("build_skipped", buildNumber=build_number, reason="build is not completed")
                continue

            stats["attempted"] += 1
            command = build_analyze_command(
                python_executable=args.python,
                repo=args.repo,
                job=args.job,
                build_number=build_number,
                log_tail_lines=log_tail_lines,
                notify=args.notify,
                notify_dry_run=args.notify_dry_run,
                force_notify=args.force_notify,
            )
            if args.dry_run:
                _emit("build_planned", buildNumber=build_number, result=build_data.get("result"), command=command)
                continue

            _emit("analysis_started", buildNumber=build_number, result=build_data.get("result"))
            try:
                completed = run_process_bounded(
                    command,
                    cwd=REPO_ROOT,
                    env=child_env,
                    timeout_seconds=args.analysis_timeout_seconds,
                )
            except BoundedProcessTimeout as exc:
                stats["failed"] += 1
                _emit(
                    "analysis_failed",
                    buildNumber=build_number,
                    reason="timeout",
                    timeoutSeconds=args.analysis_timeout_seconds,
                    stderr=_tail(exc.stderr),
                    terminationReaped=exc.termination.reaped,
                    terminationWarning=exc.termination.warning,
                )
                continue
            except Exception as exc:
                stats["failed"] += 1
                _emit("analysis_failed", buildNumber=build_number, reason="execution error", error=f"{type(exc).__name__}: {exc}")
                continue

            if completed.returncode != 0:
                stats["failed"] += 1
                _emit(
                    "analysis_failed",
                    buildNumber=build_number,
                    reason="non-zero exit",
                    returnCode=completed.returncode,
                    stderr=_tail(completed.stderr),
                    stdout=_tail(completed.stdout),
                )
                continue
            if not is_build_analyzed(store, repo=args.repo, job=args.job, build_number=build_number):
                stats["failed"] += 1
                _emit(
                    "analysis_failed",
                    buildNumber=build_number,
                    reason="analysis returned success but MongoDB completion markers are missing",
                )
                continue

            stats["succeeded"] += 1
            _emit("analysis_succeeded", buildNumber=build_number)

        _emit("poll_summary", repo=args.repo, job=args.job, **stats)
        return 1 if stats["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
