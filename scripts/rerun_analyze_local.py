from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

_SCRIPT_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_SCRIPT_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_REPO_ROOT))
from scripts._runtime import (
    BoundedProcessTimeout,
    REPO_ROOT,
    build_subprocess_env,
    ensure_repo_on_sys_path,
    read_last_jsonl,
    resolve_repo_default_path,
    resolve_user_path,
    run_process_bounded,
)
ensure_repo_on_sys_path()
from scripts._batch_common import cleanup_previous_outputs, load_env_file
from scripts._langsmith_trace import (
    find_matching_root_run,
    serialize_langsmith_run,
    summarize_trace_payload,
    write_trace_artifact,
)
from ci_owner_agent.schemas import CiResponsibilityNotice
from ci_owner_agent.services.branch_normalization import normalize_branch_name
from ci_owner_agent.services.log_parsing import resolve_final_status_from_console_log

SUMMARY_FIELDS = [
    "run", "status", "exitCode", "durationSec", "errorKind", "error", "noticeValid", "noticeValidationError",
    "noticeResult", "noticeOwner", "hasHighConfidenceOwner", "responsibilityItemCount", "inheritedOwnerCount",
    "currentBuildOwnerCount", "noOwnerItemCount", "metricsDurationMs", "llmCalls", "inputTokens", "outputTokens",
    "totalTokens", "tokenWarning", "stageCount", "traceOk", "traceUrl", "traceError", "stdoutFile",
    "noticeFile", "stderrFile", "metricsFile", "traceFile", "traceSummaryFile", "traceChildRunCount",
    "traceToolRunCount", "traceLlmRunCount", "traceUsageDictCount",
    "terminationReaped", "terminationWarning", "cleanupWarning",
]


class RunPreparationError(RuntimeError):
    pass


class RunCleanupError(RuntimeError):
    pass

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Rerun ci-owner-agent analyze-local for a local build log N times."
    )

    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--timeout-sec", type=int, default=900)

    parser.add_argument("--repo", required=True)
    parser.add_argument("--job", required=True)
    parser.add_argument("--build", type=int, required=True)
    parser.add_argument("--branch", default=None)
    parser.add_argument("--base-commit", required=True)
    parser.add_argument("--head-commit", required=True)
    parser.add_argument("--console-file", required=True)
    parser.add_argument("--build-url", default=None)
    parser.add_argument("--last-success-build", type=int, default=None)
    parser.add_argument("--previous-build", type=int, default=None)
    parser.add_argument("--previous-commit", default=None)
    parser.add_argument(
        "--result",
        choices=["SUCCESS", "FAILURE", "UNSTABLE", "ABORTED", "NOT_BUILT", "UNKNOWN"],
        default=None,
    )

    parser.add_argument("--out-dir", default=None)
    parser.add_argument("--env-file", default=None)
    parser.add_argument("--env-override", action="store_true")
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--ignore-checkout-commit-mismatch", action="store_true")
    parser.add_argument("--notify", action="store_true")
    parser.add_argument("--force-notify", action="store_true")

    # LangSmith trace fetching
    parser.add_argument("--fetch-trace", action="store_true")
    parser.add_argument("--trace-wait-sec", type=int, default=45)
    parser.add_argument("--langsmith-project", default=None)

    return parser.parse_args()


def read_notice(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    if not path.exists():
        return None, "notice file missing"
    try:
        notice = CiResponsibilityNotice.model_validate_json(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return None, f"invalid notice: {type(exc).__name__}: {exc}"
    return notice.model_dump(mode="json"), None


def validate_notice(notice: dict[str, Any] | None, args: argparse.Namespace) -> str | None:
    if notice is None:
        return "notice unavailable"
    expected = {"repo": args.repo, "job": args.job, "buildNumber": args.build, "baseCommit": args.base_commit, "headCommit": args.head_commit, "result": args.result}
    if args.branch is not None:
        expected["branch"] = args.branch
    for field, value in expected.items():
        if notice.get(field) != value:
            return f"notice metadata mismatch: {field}"
    return None


def write_summaries(out_dir: Path, rows: list[dict[str, Any]]) -> bool:
    try:
        with (out_dir / "summary.csv").open("w", encoding="utf-8-sig", newline="") as fp:
            writer = csv.DictWriter(fp, fieldnames=SUMMARY_FIELDS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        (out_dir / "summary.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        return True
    except Exception as exc:
        print(f"ERROR: failed to write rerun summaries: {type(exc).__name__}: {exc}", file=sys.stderr)
        return False


def _write_process_logs(stdout_path: Path, stderr_path: Path, stdout: str, stderr: str) -> None:
    stdout_path.write_text(stdout, encoding="utf-8", errors="replace")
    stderr_path.write_text(stderr, encoding="utf-8", errors="replace")


def prepare_run(*, run_index: int, args: argparse.Namespace, console_file: Path, out_dir: Path, project_name: str) -> dict[str, Any]:
    run_dir = out_dir / f"run-{run_index:02d}"
    paths = {
        "notice_file": run_dir / "notice.json", "stdout_file": run_dir / "stdout.log",
        "stderr_file": run_dir / "stderr.log", "metrics_file": run_dir / "metrics.jsonl",
        "command_file": run_dir / "command.txt", "trace_file": run_dir / "trace.json",
        "trace_summary_file": run_dir / "trace-summary.json",
    }
    try:
        run_dir.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        raise RunPreparationError(f"run directory creation failed: {exc}") from exc
    try:
        warnings = cleanup_previous_outputs(list(paths.values()))
    except Exception as exc:
        raise RunCleanupError(f"output cleanup failed: {exc}") from exc
    if warnings:
        raise RunCleanupError("; ".join(warnings))
    try:
        run_args = argparse.Namespace(**vars(args))
        run_args.console_file = str(console_file)
        command = build_command(run_args, paths["notice_file"])
        paths["command_file"].write_text(" ".join(f'"{part}"' if " " in part else part for part in command), encoding="utf-8")
        env = build_subprocess_env()
    except Exception as exc:
        raise RunPreparationError(f"run preparation failed: {exc}") from exc
    env["CI_AGENT_METRICS_ENABLED"] = "true"
    env["CI_AGENT_METRICS_FILE"] = str(paths["metrics_file"])
    if not args.notify:
        env["CI_AGENT_WECOM_NOTIFY_ENABLED"] = "false"
    if args.fetch_trace:
        env["LANGCHAIN_PROJECT"] = project_name
        env["LANGSMITH_PROJECT"] = project_name
        env.setdefault("LANGCHAIN_TRACING_V2", "true")
        env.setdefault("LANGSMITH_TRACING", "true")
    return {**paths, "command": command, "env": env}


def build_command(args: argparse.Namespace, notice_path: Path | None = None) -> list[str]:
    build_url = args.build_url or f"local://{args.job}/{args.build}"

    command = [
        args.python,
        "-m",
        "ci_owner_agent",
        "analyze-local",
        "--repo",
        args.repo,
        "--job",
        args.job,
        "--build",
        str(args.build),
        "--base-commit",
        args.base_commit,
        "--head-commit",
        args.head_commit,
        "--console-file",
        args.console_file,
        "--build-url",
        build_url,
    ]

    if args.branch:
        command += ["--branch", args.branch]

    if args.last_success_build is not None:
        command += ["--last-success-build", str(args.last_success_build)]

    if args.previous_build is not None:
        command += ["--previous-build", str(args.previous_build)]

    if args.previous_commit:
        command += ["--previous-commit", args.previous_commit]

    if args.result:
        command += ["--result", args.result]

    if args.ignore_checkout_commit_mismatch:
        command += ["--ignore-checkout-commit-mismatch"]

    if args.notify:
        command += ["--notify"]

    if args.force_notify:
        command += ["--force-notify"]
    if notice_path is not None:
        command += ["--output-file", str(notice_path)]

    return command


def summarize_notice(notice: dict[str, Any] | None) -> dict[str, Any]:
    if not notice:
        return {
            "noticeResult": None,
            "noticeOwner": None,
            "hasHighConfidenceOwner": None,
            "responsibilityItemCount": None,
            "inheritedOwnerCount": None,
            "currentBuildOwnerCount": None,
            "noOwnerItemCount": None,
        }

    items = notice.get("responsibilityItems")
    if not isinstance(items, list):
        items = []

    inherited = 0
    current = 0
    no_owner = 0

    for item in items:
        if not isinstance(item, dict):
            continue
        responsibility_type = item.get("responsibilityType")
        owner = item.get("owner") if isinstance(item.get("owner"), dict) else {}
        owner_type = owner.get("type")

        if responsibility_type == "inherited_failure_owner":
            inherited += 1
        elif responsibility_type == "current_build_owner":
            current += 1
        elif (
            responsibility_type in {"no_high_confidence_owner", "unknown"}
            or owner_type == "no_high_confidence_owner"
        ):
            no_owner += 1

    owner = notice.get("owner") if isinstance(notice.get("owner"), dict) else {}

    return {
        "noticeResult": notice.get("result"),
        "noticeOwner": owner.get("name"),
        "hasHighConfidenceOwner": notice.get("hasHighConfidenceOwner"),
        "responsibilityItemCount": len(items),
        "inheritedOwnerCount": inherited,
        "currentBuildOwnerCount": current,
        "noOwnerItemCount": no_owner,
    }


def metadata_matches(
    metadata: dict[str, Any],
    *,
    job: str,
    repo: str,
    build: int,
    base_commit: str,
    head_commit: str,
) -> bool:
    return (
        str(metadata.get("job")) == job
        and str(metadata.get("repo")) == repo
        and str(metadata.get("buildNumber")) == str(build)
        and str(metadata.get("baseCommit")) == base_commit
        and str(metadata.get("headCommit")) == head_commit
    )


def fetch_langsmith_trace(
    *,
    job: str,
    repo: str,
    build: int,
    base_commit: str,
    head_commit: str,
    project_name: str,
    started_at: dt.datetime,
    trace_path: Path,
    trace_summary_path: Path,
    wait_seconds: int,
) -> dict[str, Any]:
    result = find_matching_root_run(
        project_name=project_name,
        started_at=started_at,
        wait_seconds=wait_seconds,
        metadata_matches=lambda metadata: metadata_matches(
            metadata,
            job=job,
            repo=repo,
            build=build,
            base_commit=base_commit,
            head_commit=head_commit,
        ),
    )
    if not result["ok"]:
        return result
    full = result["run"]
    payload = write_trace_artifact(trace_path, full, result["url"], serializer=serialize_langsmith_run)
    summary = summarize_trace_payload(payload)
    trace_summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return {
        "ok": True,
        "traceFile": str(trace_path),
        "traceSummaryFile": str(trace_summary_path),
        "runId": str(getattr(full, "id", "")),
        "url": result["url"],
        "childRunCount": summary.get("childRunCount"),
        "toolRunCount": summary.get("toolRunCount"),
        "llmRunCount": summary.get("llmRunCount"),
        "usageDictCount": summary.get("usageDictCount"),
    }


def main() -> int:
    args = parse_args()

    if args.runs <= 0:
        raise SystemExit("--runs must be positive")
    if args.timeout_sec <= 0:
        raise SystemExit("--timeout-sec must be positive")

    out_dir = resolve_user_path(args.out_dir) if args.out_dir else resolve_repo_default_path("runs/rerun-analyze-local")
    out_dir.mkdir(parents=True, exist_ok=True)

    def status_failure(error: str) -> int:
        row = {"run": 0, "status": "FAILED", "errorKind": "status_validation", "error": error,
               "noticeValid": False, "noticeValidationError": error}
        write_summaries(out_dir, [row])
        print(error, file=sys.stderr)
        return 1

    console_file = resolve_user_path(args.console_file)
    if not console_file.exists():
        raise SystemExit(f"console file not found: {console_file}")
    if args.branch is not None:
        args.branch = normalize_branch_name(args.branch)
        if args.branch is None:
            return status_failure("invalid branch")
    status_resolution = resolve_final_status_from_console_log(console_file.read_text(encoding="utf-8", errors="replace"))
    if status_resolution.error:
        return status_failure(status_resolution.error)
    detected_result = status_resolution.status
    if args.result is not None and args.result != detected_result:
        return status_failure(f"status validation failed: requested {args.result}, log reports {detected_result}")
    args.result = detected_result
    if args.result not in {"FAILURE", "UNSTABLE", "UNKNOWN"}:
        return status_failure("rerun-analyze-local only supports FAILURE, UNSTABLE and UNKNOWN")
    env_file = resolve_user_path(args.env_file) if args.env_file else resolve_repo_default_path(".env")
    load_env_file(env_file, override=args.env_override)


    batch_id = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    project_name = (
        args.langsmith_project
        or os.environ.get("LANGCHAIN_PROJECT")
        or os.environ.get("LANGSMITH_PROJECT")
        or f"ci-owner-agent-rerun-{args.build}-{batch_id}"
    )

    rows: list[dict[str, Any]] = []
    command = build_command(args)

    print("Command template:")
    print(" ".join(f'"{part}"' if " " in part else part for part in command))
    print(f"LangSmith project: {project_name}")
    print(f"Fetch trace: {args.fetch_trace}")
    print()

    for run_index in range(1, args.runs + 1):
        run_name = f"run-{run_index:02d}"
        run_dir = out_dir / run_name
        notice_file = run_dir / "notice.json"
        stdout_file = run_dir / "stdout.log"
        stderr_file = run_dir / "stderr.log"
        metrics_file = run_dir / "metrics.jsonl"
        trace_file = run_dir / "trace.json"
        trace_summary_file = run_dir / "trace-summary.json"
        try:
            prepared = prepare_run(run_index=run_index, args=args, console_file=console_file, out_dir=out_dir, project_name=project_name)
            run_command, env = prepared["command"], prepared["env"]
        except (RunCleanupError, RunPreparationError) as exc:
            message = f"{type(exc).__name__}: {exc}"
            kind = "cleanup" if isinstance(exc, RunCleanupError) else "prepare"
            rows.append({"run": run_index, "status": "FAILED", "errorKind": kind, "error": message,
                         "noticeValid": False, "noticeValidationError": f"{kind} failed",
                         "stdoutFile": str(stdout_file), "stderrFile": str(stderr_file), "noticeFile": str(notice_file),
                         "metricsFile": str(metrics_file), "traceFile": str(trace_file), "traceSummaryFile": str(trace_summary_file)})
            continue

        print(f"========== {run_name} / {args.runs} ==========")
        print(f"stdout : {stdout_file}")
        print(f"stderr : {stderr_file}")
        print(f"metrics: {metrics_file}")
        if args.fetch_trace:
            print(f"trace  : {trace_file}")

        started_at = dt.datetime.now(dt.timezone.utc)
        started = time.perf_counter()
        termination_warning: str | None = None
        termination_reaped: bool | None = None

        timed_out = False
        exit_code: int | None = None
        try:
            completed = run_process_bounded(
                run_command,
                cwd=REPO_ROOT,
                env=env,
                timeout_seconds=args.timeout_sec,
            )
            _write_process_logs(stdout_file, stderr_file, completed.stdout, completed.stderr)
            exit_code = completed.returncode
        except BoundedProcessTimeout as exc:
            timed_out = True
            termination_reaped = exc.termination.reaped
            termination_warning = exc.termination.warning
            _write_process_logs(stdout_file, stderr_file, str(exc.output or ""), str(exc.stderr or ""))
        except Exception as exc:
            duration_sec = round(time.perf_counter() - started, 3)
            rows.append({
                "run": run_index, "status": "FAILED", "durationSec": duration_sec,
                "errorKind": "execution", "error": f"{type(exc).__name__}: {exc}",
                "noticeValid": False, "noticeValidationError": "process execution failed",
                "stdoutFile": str(stdout_file), "noticeFile": str(notice_file), "stderrFile": str(stderr_file),
                "metricsFile": str(metrics_file), "traceFile": str(trace_file) if args.fetch_trace else None,
                "traceSummaryFile": str(trace_summary_file) if args.fetch_trace else None,
            })
            continue

        duration_sec = round(time.perf_counter() - started, 3)

        error_kind: str | None = None
        error: str | None = None
        if timed_out:
            status = "TIMEOUT"
            error_kind, error = "timeout", f"analyze-local timeout after {args.timeout_sec}s"
        elif exit_code == 0:
            status = "OK"
        else:
            status = "FAILED"
            error_kind, error = "execution", f"analyze-local exited with {exit_code}"

        trace_result: dict[str, Any] | None = None
        if args.fetch_trace and not timed_out:
            trace_result = fetch_langsmith_trace(
                job=args.job,
                repo=args.repo,
                build=args.build,
                base_commit=args.base_commit,
                head_commit=args.head_commit,
                project_name=project_name,
                started_at=started_at,
                trace_path=trace_file,
                trace_summary_path=trace_summary_file,
                wait_seconds=args.trace_wait_sec,
            )

        metrics = None if timed_out else read_last_jsonl(metrics_file)
        notice, read_error = (None, "analysis outputs are untrusted because execution timed out") if timed_out else read_notice(notice_file)
        notice_error = read_error or validate_notice(notice, args)
        if not timed_out and exit_code == 0 and notice_error:
            status = "FAILED"
            error = notice_error
            error_kind = "notice_missing" if read_error == "notice file missing" else ("notice_schema" if read_error else "notice_metadata")
        notice_summary = summarize_notice(notice)

        row = {
            "run": run_index,
            "status": status,
            "exitCode": exit_code,
            "durationSec": duration_sec,
            "metricsDurationMs": metrics.get("durationMs") if metrics else None,
            "llmCalls": metrics.get("llmCalls") if metrics else None,
            "inputTokens": metrics.get("inputTokens") if metrics else None,
            "outputTokens": metrics.get("outputTokens") if metrics else None,
            "totalTokens": metrics.get("totalTokens") if metrics else None,
            "tokenWarning": metrics.get("tokenWarning") if metrics else None,
            "stageCount": len(metrics.get("stages") or []) if metrics else None,
            **notice_summary,
            "traceOk": trace_result.get("ok") if trace_result else None,
            "traceUrl": trace_result.get("url") if trace_result else None,
            "traceChildRunCount": trace_result.get("childRunCount") if trace_result else None,
            "traceToolRunCount": trace_result.get("toolRunCount") if trace_result else None,
            "traceLlmRunCount": trace_result.get("llmRunCount") if trace_result else None,
            "traceUsageDictCount": trace_result.get("usageDictCount") if trace_result else None,
            "traceError": trace_result.get("error") if trace_result and not trace_result.get("ok") else None,
            "stdoutFile": str(stdout_file),
            "noticeFile": str(notice_file),
            "noticeValid": False if timed_out else notice_error is None,
            "noticeValidationError": notice_error,
            "errorKind": error_kind,
            "error": error,
            "stderrFile": str(stderr_file),
            "metricsFile": str(metrics_file),
            "traceFile": str(trace_file) if args.fetch_trace else None,
            "traceSummaryFile": str(trace_summary_file) if args.fetch_trace else None,
            "terminationWarning": termination_warning,
            "terminationReaped": termination_reaped,
        }

        rows.append(row)

        print(
            f"{run_name} result: {status}, "
            f"duration={duration_sec}s, "
            f"exitCode={exit_code}, "
            f"llmCalls={row['llmCalls']}, "
            f"totalTokens={row['totalTokens']}, "
            f"traceOk={row['traceOk']}, "
            f"childRuns={row['traceChildRunCount']}"
        )
        print()

    summary_csv = out_dir / "summary.csv"
    summary_json = out_dir / "summary.json"
    summaries_written = write_summaries(out_dir, rows)

    ok_count = sum(1 for row in rows if row["status"] == "OK")
    timeout_count = sum(1 for row in rows if row["status"] == "TIMEOUT")
    failed_count = sum(1 for row in rows if row["status"] == "FAILED")

    print("========== Summary ==========")
    print(f"OK      : {ok_count} / {args.runs}")
    print(f"TIMEOUT : {timeout_count} / {args.runs}")
    print(f"FAILED  : {failed_count} / {args.runs}")
    print(f"Timeout probability: {timeout_count} / {args.runs} = {timeout_count / args.runs * 100:.2f}%")
    print(f"Summary CSV : {summary_csv}")
    print(f"Summary JSON: {summary_json}")

    return 0 if summaries_written and rows and all(row.get("status") == "OK" for row in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
