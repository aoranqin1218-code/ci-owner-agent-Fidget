from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

_SCRIPT_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_SCRIPT_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_REPO_ROOT))
from scripts._runtime import REPO_ROOT, build_subprocess_env, ensure_repo_on_sys_path, resolve_repo_default_path, resolve_user_path, run_process_bounded
ensure_repo_on_sys_path()
from ci_owner_agent.schemas import CiResponsibilityNotice

from scripts.batch_analyze_company_logs import (
    cleanup_previous_outputs,
    extract_first_json_object,
    extract_responsibility_stats_from_notice,
    load_env_file,
    slug,
    to_jsonable,
)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


SUMMARY_FIELDS = [
    "build",
    "job",
    "repo",
    "result",
    "branch",
    "baseCommit",
    "headCommit",
    "lastSuccessfulBuildNumber",
    "buildUrl",
    "returnCode",
    "durationSeconds",
    "ownerType",
    "ownerName",
    "ownerEmail",
    "ownerCommit",
    "hasHighConfidenceOwner",
    "responsibilityItemCount",
    "responsibleOwners",
    "inheritedOwners",
    "currentBuildOwners",
    "unresolvedFailureCount",
    "aiHistoryEligibleCurrentFactsCount",
    "aiHistoryHistoricalBuildsCount",
    "aiHistoryHistoricalFactsCount",
    "aiHistoryRankedPairsCount",
    "aiHistoryComparedPairsCount",
    "aiHistoryAcceptedCandidatesCount",
    "aiHistoryQueryStage",
    "aiHistorySkipped",
    "noticeFile",
    "stdoutFile",
    "stderrFile",
    "traceFile",
    "traceOk",
    "traceUrl",
    "failureReason",
    "skipped",
    "skipReason",
    "noticeValid",
    "resumeValidated",
    "resumeInvalidReason",
    "errorKind",
    "cleanupWarning",
    "noticeValidationError",
    "terminationReaped",
    "terminationWarning",
    "error",
]


def parse_builds(builds: str | None, build_from: int | None, build_to: int | None) -> list[int]:
    selected: set[int] = set()
    if builds:
        for raw in builds.split(","):
            value = raw.strip()
            if not value:
                continue
            selected.add(int(value))
    if build_from is not None or build_to is not None:
        if build_from is None or build_to is None:
            raise ValueError("--build-from and --build-to must be provided together")
        if build_from > build_to:
            raise ValueError("--build-from must be <= --build-to")
        selected.update(range(build_from, build_to + 1))
    if not selected:
        raise ValueError("--builds or --build-from/--build-to is required")
    return sorted(selected)


def build_analyze_command(
    *,
    build: int,
    repo: str,
    job: str,
    log_tail_lines: int,
    notify: bool,
    notify_dry_run: bool,
    force_notify: bool,
    notice_path: Path | None = None,
    python_executable: str = sys.executable,
) -> list[str]:
    command = [
        python_executable,
        "-m",
        "ci_owner_agent",
        "analyze",
        "--job",
        job,
        "--build",
        str(build),
        "--repo",
        repo,
        "--log-tail-lines",
        str(log_tail_lines),
    ]
    if notify:
        command.append("--notify")
    if notify_dry_run:
        command.append("--notify-dry-run")
    if force_notify:
        command.append("--force-notify")
    if notice_path is not None:
        command.extend(["--output-file", str(notice_path)])
    return command


def get_run_metadata(run: Any) -> dict[str, Any]:
    direct = getattr(run, "metadata", None)
    if isinstance(direct, dict):
        return direct
    extra = getattr(run, "extra", None)
    if isinstance(extra, dict):
        metadata = extra.get("metadata")
        if isinstance(metadata, dict):
            return metadata
    return {}


def metadata_matches_jenkins(md: dict[str, Any], *, build: int, repo: str, job: str) -> bool:
    return str(md.get("job")) == job and str(md.get("repo")) == repo and str(md.get("buildNumber")) == str(build)


def fetch_langsmith_trace(
    *,
    build: int,
    repo: str,
    job: str,
    project_name: str,
    started_at: dt.datetime,
    trace_path: Path,
    wait_seconds: int,
) -> dict[str, Any]:
    try:
        from langsmith import Client
    except Exception as exc:
        return {"ok": False, "error": f"langsmith import failed: {exc}"}

    client = Client()
    deadline = time.time() + wait_seconds
    last_error: str | None = None

    while time.time() < deadline:
        try:
            runs = list(
                client.list_runs(
                    project_name=project_name,
                    is_root=True,
                    start_time=started_at - dt.timedelta(minutes=2),
                    limit=100,
                )
            )
            runs.sort(
                key=lambda run: getattr(run, "start_time", dt.datetime.min.replace(tzinfo=dt.timezone.utc)),
                reverse=True,
            )
            for run in runs:
                full = client.read_run(getattr(run, "id"), load_child_runs=True)
                if not metadata_matches_jenkins(get_run_metadata(full), build=build, repo=repo, job=job):
                    continue
                payload = to_jsonable(full)
                try:
                    payload["_langsmith_url"] = client.get_run_url(run=full, project_name=project_name)
                except Exception:
                    pass
                trace_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
                return {
                    "ok": True,
                    "traceFile": str(trace_path),
                    "runId": str(getattr(full, "id", "")),
                    "url": payload.get("_langsmith_url"),
                }
        except Exception as exc:
            last_error = str(exc)
        time.sleep(2)
    return {"ok": False, "error": last_error or "trace not found before timeout"}


def find_key_recursive(obj: Any, key: str) -> Any | None:
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for value in obj.values():
            found = find_key_recursive(value, key)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for value in obj:
            found = find_key_recursive(value, key)
            if found is not None:
                return found
    elif isinstance(obj, str) and key in obj:
        try:
            parsed = json.loads(obj)
        except Exception:
            parsed = extract_first_json_object(obj)
        if parsed is not None:
            return find_key_recursive(parsed, key)
    return None


def extract_ai_history_stats_from_trace_or_notice(notice: dict[str, Any] | None, trace: dict[str, Any] | None) -> dict[str, Any]:
    empty = {
        "aiHistoryEligibleCurrentFactsCount": None,
        "aiHistoryHistoricalBuildsCount": None,
        "aiHistoryHistoricalFactsCount": None,
        "aiHistoryRankedPairsCount": None,
        "aiHistoryComparedPairsCount": None,
        "aiHistoryAcceptedCandidatesCount": None,
        "aiHistoryQueryStage": None,
        "aiHistorySkipped": None,
    }
    ai_precheck = find_key_recursive(trace, "aiHistoryPrecheck") if isinstance(trace, dict) else None
    diagnostics = ai_precheck.get("diagnostics") if isinstance(ai_precheck, dict) else None
    if not isinstance(diagnostics, dict):
        return empty
    skipped = diagnostics.get("skipped")
    return {
        "aiHistoryEligibleCurrentFactsCount": diagnostics.get("eligibleCurrentFactsCount"),
        "aiHistoryHistoricalBuildsCount": diagnostics.get("historicalBuildsCount"),
        "aiHistoryHistoricalFactsCount": diagnostics.get("historicalFactsCount"),
        "aiHistoryRankedPairsCount": diagnostics.get("rankedPairsCount"),
        "aiHistoryComparedPairsCount": diagnostics.get("comparedPairsCount"),
        "aiHistoryAcceptedCandidatesCount": diagnostics.get("acceptedCandidatesCount"),
        "aiHistoryQueryStage": diagnostics.get("queryStage"),
        "aiHistorySkipped": json.dumps(skipped, ensure_ascii=False, sort_keys=True) if isinstance(skipped, dict) else skipped,
    }


def notice_record_fields(notice: dict[str, Any]) -> dict[str, Any]:
    owner = notice.get("owner") if isinstance(notice.get("owner"), dict) else {}
    return {
        "result": notice.get("result"),
        "buildUrl": notice.get("buildUrl"),
        "branch": notice.get("branch"),
        "baseCommit": notice.get("baseCommit"),
        "headCommit": notice.get("headCommit"),
        "lastSuccessfulBuildNumber": notice.get("lastSuccessfulBuildNumber"),
        "ownerType": owner.get("type"),
        "ownerName": owner.get("name"),
        "ownerEmail": owner.get("email"),
        "ownerCommit": owner.get("commit"),
        "hasHighConfidenceOwner": notice.get("hasHighConfidenceOwner"),
        "failureReason": notice.get("failureReason"),
        **extract_responsibility_stats_from_notice(notice),
    }


def run_analyze(
    *,
    build: int,
    repo: str,
    job: str,
    log_tail_lines: int,
    notify: bool,
    notify_dry_run: bool,
    force_notify: bool,
    cwd: Path,
    env: dict[str, str],
    timeout_seconds: int,
    notice_path: Path | None = None,
    python_executable: str = sys.executable,
) -> subprocess.CompletedProcess[str]:
    command = build_analyze_command(
            build=build,
            repo=repo,
            job=job,
            log_tail_lines=log_tail_lines,
            notify=notify,
            notify_dry_run=notify_dry_run,
            force_notify=force_notify,
            notice_path=notice_path,
            python_executable=python_executable,
        )
    return run_process_bounded(command, cwd=cwd, env=env, timeout_seconds=timeout_seconds)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--job", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--build-from", type=int, default=None)
    parser.add_argument("--build-to", type=int, default=None)
    parser.add_argument("--builds", default=None)
    parser.add_argument("--log-tail-lines", type=int, default=200)
    parser.add_argument("--out-dir", default=None)
    parser.add_argument("--env-file", default=None)
    parser.add_argument("--env-override", action="store_true")
    parser.add_argument("--langsmith-project", default=None)
    parser.add_argument("--fetch-trace", action="store_true")
    parser.add_argument("--trace-wait-seconds", type=int, default=30)
    parser.add_argument("--timeout-seconds", type=int, default=900)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--notify", action="store_true")
    parser.add_argument("--notify-dry-run", action="store_true")
    parser.add_argument("--force-notify", action="store_true")

    args = parser.parse_args()
    try:
        builds = parse_builds(args.builds, args.build_from, args.build_to)
    except ValueError as exc:
        parser.error(str(exc))

    repo_root = REPO_ROOT
    env_file = resolve_user_path(args.env_file) if args.env_file else resolve_repo_default_path(".env")
    load_env_file(env_file, override=args.env_override)

    batch_id = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = resolve_user_path(args.out_dir) if args.out_dir else resolve_repo_default_path(f"runs/jenkins-batch-{batch_id}")
    notices_dir = out_dir / "notices"
    stdout_dir = out_dir / "stdout"
    stderr_dir = out_dir / "stderr"
    traces_dir = out_dir / "traces"
    for directory in [out_dir, notices_dir, stdout_dir, stderr_dir, traces_dir]:
        directory.mkdir(parents=True, exist_ok=True)

    project_name = (
        args.langsmith_project
        or os.environ.get("LANGCHAIN_PROJECT")
        or os.environ.get("LANGSMITH_PROJECT")
        or f"ci-owner-agent-jenkins-batch-{batch_id}"
    )
    env = build_subprocess_env()
    env["LANGCHAIN_PROJECT"] = project_name
    env["LANGSMITH_PROJECT"] = project_name
    env.setdefault("LANGCHAIN_TRACING_V2", "true")
    env.setdefault("LANGSMITH_TRACING", "true")

    index_path = out_dir / "index.jsonl"
    summary_path = out_dir / "summary.csv"
    rows: list[dict[str, Any]] = []

    print(f"repo_root={repo_root}")
    print(f"out_dir={out_dir}")
    print(f"env_file={env_file}")
    print(f"langsmith_project={project_name}")
    print(f"job={args.job}")
    print(f"repo={args.repo}")
    print(f"builds={','.join(str(item) for item in builds)}")

    with index_path.open("w", encoding="utf-8") as index_file:
        for build in builds:
            name = f"{slug(args.job)}_{build}"
            notice_path = notices_dir / f"{name}.notice.json"
            stdout_path = stdout_dir / f"{name}.stdout.txt"
            stderr_path = stderr_dir / f"{name}.stderr.txt"
            trace_path = traces_dir / f"{name}.trace.json"
            command = build_analyze_command(
                build=build,
                repo=args.repo,
                job=args.job,
                log_tail_lines=args.log_tail_lines,
                notify=args.notify,
                notify_dry_run=args.notify_dry_run,
                force_notify=args.force_notify,
                notice_path=notice_path,
                python_executable=args.python,
            )
            record: dict[str, Any] = {
                "build": build,
                "job": args.job,
                "repo": args.repo,
                "noticeFile": str(notice_path),
                "stdoutFile": str(stdout_path),
                "stderrFile": str(stderr_path),
                "traceFile": str(trace_path),
                "command": command,
                "skipped": False,
            }
            print(f"\n=== build {build} ===")
            print("cmd=" + " ".join(command))

            if args.dry_run:
                record["dryRun"] = True
                index_file.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
                rows.append(record)
                continue
            if args.resume and notice_path.exists():
                try:
                    notice = CiResponsibilityNotice.model_validate_json(notice_path.read_text(encoding="utf-8"))
                    valid = notice.repo == args.repo and notice.job == args.job and notice.buildNumber == build
                except Exception:
                    valid = False
                record["resumeValidated"] = valid
                if valid:
                    record["skipped"] = True
                    record["skipReason"] = "resume: validated notice"
                    index_file.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
                    rows.append(record)
                    continue
                record["resumeInvalidReason"] = "invalid notice or metadata mismatch"
                resume_cleanup = cleanup_previous_outputs([notice_path])
                if resume_cleanup:
                    message = "; ".join(resume_cleanup)
                    record.update({"cleanupWarning": message, "errorKind": "cleanup", "error": message, "noticeValid": False})
                    index_file.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
                    rows.append(record)
                    continue

            cleanup_warnings = cleanup_previous_outputs([notice_path, stdout_path, stderr_path, trace_path])
            if cleanup_warnings:
                message = "; ".join(cleanup_warnings)
                record.update({"cleanupWarning": message, "errorKind": "cleanup", "error": message, "noticeValid": False})
                index_file.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
                rows.append(record)
                continue

            started_at = dt.datetime.now(dt.timezone.utc)
            started_monotonic = time.monotonic()
            notice: dict[str, Any] | None = None
            trace_payload: dict[str, Any] | None = None
            try:
                cp = run_analyze(
                    build=build,
                    repo=args.repo,
                    job=args.job,
                    log_tail_lines=args.log_tail_lines,
                    notify=args.notify,
                    notify_dry_run=args.notify_dry_run,
                    force_notify=args.force_notify,
                    cwd=repo_root,
                    env=env,
                    timeout_seconds=args.timeout_seconds,
                    notice_path=notice_path,
                    python_executable=args.python,
                )
                record["durationSeconds"] = round(time.monotonic() - started_monotonic, 3)
                record["returnCode"] = cp.returncode
                if cp.returncode != 0:
                    record["errorKind"] = "execution"
                    record["error"] = f"analyze exited with {cp.returncode}"
                stdout_path.write_text(cp.stdout, encoding="utf-8")
                stderr_path.write_text(cp.stderr, encoding="utf-8")
                try:
                    notice = CiResponsibilityNotice.model_validate_json(notice_path.read_text(encoding="utf-8")).model_dump(mode="json")
                except Exception as exc:
                    notice = None
                    record["noticeValid"] = False
                    notice_kind = "notice_schema" if notice_path.exists() else "notice_missing"
                    message = f"invalid output notice: {type(exc).__name__}: {exc}"
                    record["noticeValidationError"] = message
                    if not record.get("errorKind"):
                        record["errorKind"] = notice_kind
                        record["error"] = message
                if notice is not None:
                    mismatches = [field for field, expected in (("repo", args.repo), ("job", args.job), ("buildNumber", build)) if notice[field] != expected]
                    if mismatches:
                        record["noticeValid"] = False
                        message = f"notice metadata mismatch: {mismatches[0]}"
                        record["noticeValidationError"] = message
                        if not record.get("errorKind"):
                            record["errorKind"] = "notice_metadata"
                            record["error"] = message
                    else:
                        record["noticeValid"] = True
                        record.update(notice_record_fields(notice))

                if args.fetch_trace:
                    trace_result = fetch_langsmith_trace(
                        build=build,
                        repo=args.repo,
                        job=args.job,
                        project_name=project_name,
                        started_at=started_at,
                        trace_path=trace_path,
                        wait_seconds=args.trace_wait_seconds,
                    )
                    record["trace"] = trace_result
                    record["traceOk"] = trace_result.get("ok")
                    record["traceUrl"] = trace_result.get("url")
                    if trace_path.exists():
                        try:
                            trace_payload = json.loads(trace_path.read_text(encoding="utf-8"))
                        except Exception:
                            trace_payload = None
                    record.update(extract_ai_history_stats_from_trace_or_notice(notice, trace_payload))
            except subprocess.TimeoutExpired as exc:
                record["durationSeconds"] = round(time.monotonic() - started_monotonic, 3)
                record["error"] = f"analyze timeout after {args.timeout_seconds}s"
                record["errorKind"] = "timeout"
                record["noticeValid"] = False
                termination = getattr(exc, "termination", None)
                record["terminationReaped"] = getattr(termination, "reaped", None)
                record["terminationWarning"] = getattr(termination, "warning", None)
                stdout_path.write_text(exc.stdout or "", encoding="utf-8")
                stderr_path.write_text(exc.stderr or "", encoding="utf-8")
                timeout_cleanup = cleanup_previous_outputs([notice_path, trace_path])
                if timeout_cleanup:
                    record["cleanupWarning"] = "; ".join(timeout_cleanup)
            except Exception as exc:
                record["durationSeconds"] = round(time.monotonic() - started_monotonic, 3)
                record["error"] = str(exc)
                record["errorKind"] = "execution"

            if "aiHistoryEligibleCurrentFactsCount" not in record:
                record.update(extract_ai_history_stats_from_trace_or_notice(notice, trace_payload))
            index_file.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
            index_file.flush()
            rows.append(record)

    with summary_path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=SUMMARY_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    print("\nDone.")
    print(f"index:   {index_path}")
    print(f"summary: {summary_path}")
    return 1 if any(not row.get("skipped") and (row.get("error") or row.get("returnCode") not in (None, 0) or row.get("noticeValid") is False) for row in rows) else 0


if __name__ == "__main__":
    raise SystemExit(main())
