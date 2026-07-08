from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import platform
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


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
    parser.add_argument(
        "--result",
        choices=["SUCCESS", "FAILURE", "UNSTABLE", "ABORTED", "UNKNOWN"],
        default=None,
    )

    parser.add_argument("--out-dir", default="./runs/rerun-analyze-local")
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--ignore-checkout-commit-mismatch", action="store_true")
    parser.add_argument("--notify", action="store_true")
    parser.add_argument("--force-notify", action="store_true")

    # LangSmith trace fetching
    parser.add_argument("--fetch-trace", action="store_true")
    parser.add_argument("--trace-wait-sec", type=int, default=45)
    parser.add_argument("--langsmith-project", default=None)

    return parser.parse_args()


def kill_process_tree(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return

    if platform.system().lower().startswith("win"):
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    else:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except Exception:
            process.kill()


def read_last_jsonl(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None

    lines = [
        line.strip()
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines()
        if line.strip()
    ]
    if not lines:
        return None

    try:
        return json.loads(lines[-1])
    except Exception:
        return None


def extract_json_object(text: str) -> dict[str, Any] | None:
    decoder = json.JSONDecoder()
    for index, ch in enumerate(text):
        if ch != "{":
            continue
        try:
            obj, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


def read_notice(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8", errors="replace")
    return extract_json_object(text)


def build_command(args: argparse.Namespace) -> list[str]:
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

    if args.result:
        command += ["--result", args.result]

    if args.ignore_checkout_commit_mismatch:
        command += ["--ignore-checkout-commit-mismatch"]

    if args.notify:
        command += ["--notify"]

    if args.force_notify:
        command += ["--force-notify"]

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


def to_jsonable(obj: Any, depth: int = 0) -> Any:
    if depth > 12:
        return str(obj)

    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj

    if isinstance(obj, (dt.datetime, dt.date)):
        return obj.isoformat()

    if isinstance(obj, dict):
        return {str(k): to_jsonable(v, depth + 1) for k, v in obj.items()}

    if isinstance(obj, (list, tuple, set)):
        return [to_jsonable(v, depth + 1) for v in obj]

    if hasattr(obj, "model_dump"):
        try:
            return to_jsonable(obj.model_dump(), depth + 1)
        except Exception:
            pass

    if hasattr(obj, "dict"):
        try:
            return to_jsonable(obj.dict(), depth + 1)
        except Exception:
            pass

    if hasattr(obj, "__dict__"):
        try:
            return {
                str(k): to_jsonable(v, depth + 1)
                for k, v in vars(obj).items()
                if not str(k).startswith("_")
            }
        except Exception:
            pass

    return str(obj)


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


def run_to_dict(run: Any) -> dict[str, Any]:
    fields = [
        "id",
        "name",
        "run_type",
        "start_time",
        "end_time",
        "status",
        "error",
        "inputs",
        "outputs",
        "extra",
        "metadata",
        "events",
        "serialized",
        "tags",
        "execution_order",
        "dotted_order",
        "parent_run_id",
        "trace_id",
        "child_runs",
    ]

    data: dict[str, Any] = {}
    for field in fields:
        try:
            value = getattr(run, field, None)
        except Exception:
            continue
        if value is not None:
            data[field] = to_jsonable(value)

    # 有些 LangSmith Run 把 metadata 放在 extra.metadata
    if "metadata" not in data:
        metadata = get_run_metadata(run)
        if metadata:
            data["metadata"] = to_jsonable(metadata)

    return data


def flatten_runs(payload: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []

    def walk(node: Any, depth: int = 0) -> None:
        if not isinstance(node, dict):
            return

        result.append(
            {
                "depth": depth,
                "id": node.get("id"),
                "name": node.get("name"),
                "run_type": node.get("run_type"),
                "start_time": node.get("start_time"),
                "end_time": node.get("end_time"),
                "error": node.get("error"),
            }
        )

        children = node.get("child_runs")
        if isinstance(children, list):
            for child in children:
                walk(child, depth + 1)

    walk(payload, 0)
    return result


def find_usage_dicts(obj: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []

    usage_keys = {
        "input_tokens",
        "prompt_tokens",
        "output_tokens",
        "completion_tokens",
        "total_tokens",
        "inputTokens",
        "outputTokens",
        "totalTokens",
    }

    def looks_like_usage(value: dict[str, Any]) -> bool:
        return any(key in value for key in usage_keys)

    def walk(value: Any, depth: int = 0) -> None:
        if depth > 14:
            return
        if isinstance(value, dict):
            if looks_like_usage(value):
                found.append(value)
            for child in value.values():
                walk(child, depth + 1)
        elif isinstance(value, list):
            for child in value:
                walk(child, depth + 1)

    walk(obj)
    return found


def summarize_trace_payload(payload: dict[str, Any]) -> dict[str, Any]:
    runs = flatten_runs(payload)
    usage_dicts = find_usage_dicts(payload)

    root = runs[0] if runs else {}
    tool_runs = [
        item
        for item in runs
        if str(item.get("run_type") or "").lower() == "tool"
        or str(item.get("name") or "").startswith(("log_", "repo_", "history_", "ts_"))
    ]
    llm_runs = [
        item
        for item in runs
        if str(item.get("run_type") or "").lower() in {"llm", "chat_model"}
        or "chat" in str(item.get("name") or "").lower()
        or "model" in str(item.get("name") or "").lower()
    ]
    error_runs = [item for item in runs if item.get("error")]

    return {
        "rootRunId": root.get("id"),
        "rootName": root.get("name"),
        "totalRuns": len(runs),
        "childRunCount": max(0, len(runs) - 1),
        "toolRunCount": len(tool_runs),
        "llmRunCount": len(llm_runs),
        "errorRunCount": len(error_runs),
        "usageDictCount": len(usage_dicts),
        "runs": runs,
    }


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
                key=lambda item: getattr(
                    item,
                    "start_time",
                    dt.datetime.min.replace(tzinfo=dt.timezone.utc),
                ),
                reverse=True,
            )

            for run in runs:
                full = client.read_run(getattr(run, "id"), load_child_runs=True)
                metadata = get_run_metadata(full)

                if not metadata_matches(
                    metadata,
                    job=job,
                    repo=repo,
                    build=build,
                    base_commit=base_commit,
                    head_commit=head_commit,
                ):
                    continue

                payload = run_to_dict(full)

                try:
                    payload["_langsmith_url"] = client.get_run_url(
                        run=full,
                        project_name=project_name,
                    )
                except Exception:
                    pass

                summary = summarize_trace_payload(payload)

                trace_path.write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2, default=str),
                    encoding="utf-8",
                )
                trace_summary_path.write_text(
                    json.dumps(summary, ensure_ascii=False, indent=2, default=str),
                    encoding="utf-8",
                )

                return {
                    "ok": True,
                    "traceFile": str(trace_path),
                    "traceSummaryFile": str(trace_summary_path),
                    "runId": str(getattr(full, "id", "")),
                    "url": payload.get("_langsmith_url"),
                    "childRunCount": summary.get("childRunCount"),
                    "toolRunCount": summary.get("toolRunCount"),
                    "llmRunCount": summary.get("llmRunCount"),
                    "usageDictCount": summary.get("usageDictCount"),
                }

        except Exception as exc:
            last_error = str(exc)

        time.sleep(2)

    return {"ok": False, "error": last_error or "trace not found before timeout"}


def main() -> int:
    args = parse_args()

    if args.runs <= 0:
        raise SystemExit("--runs must be positive")
    if args.timeout_sec <= 0:
        raise SystemExit("--timeout-sec must be positive")

    console_file = Path(args.console_file)
    if not console_file.exists():
        raise SystemExit(f"console file not found: {console_file}")

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

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
        run_dir.mkdir(parents=True, exist_ok=True)

        stdout_file = run_dir / "stdout.json"
        stderr_file = run_dir / "stderr.log"
        metrics_file = run_dir / "metrics.jsonl"
        command_file = run_dir / "command.txt"
        trace_file = run_dir / "trace.json"
        trace_summary_file = run_dir / "trace-summary.json"

        command_file.write_text(
            " ".join(f'"{part}"' if " " in part else part for part in command),
            encoding="utf-8",
        )

        env = os.environ.copy()
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        env["CI_AGENT_METRICS_ENABLED"] = "true"
        env["CI_AGENT_METRICS_FILE"] = str(metrics_file)

        if not args.notify:
            env["CI_AGENT_WECOM_NOTIFY_ENABLED"] = "false"

        if args.fetch_trace:
            env["LANGCHAIN_PROJECT"] = project_name
            env["LANGSMITH_PROJECT"] = project_name
            env.setdefault("LANGCHAIN_TRACING_V2", "true")
            env.setdefault("LANGSMITH_TRACING", "true")

        print(f"========== {run_name} / {args.runs} ==========")
        print(f"stdout : {stdout_file}")
        print(f"stderr : {stderr_file}")
        print(f"metrics: {metrics_file}")
        if args.fetch_trace:
            print(f"trace  : {trace_file}")

        started_at = dt.datetime.now(dt.timezone.utc)
        started = time.perf_counter()

        with stdout_file.open("w", encoding="utf-8", errors="replace") as stdout_fp, stderr_file.open(
            "w", encoding="utf-8", errors="replace"
        ) as stderr_fp:
            creationflags = 0
            preexec_fn = None

            if platform.system().lower().startswith("win"):
                creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                preexec_fn = os.setsid

            process = subprocess.Popen(
                command,
                stdout=stdout_fp,
                stderr=stderr_fp,
                env=env,
                text=True,
                creationflags=creationflags,
                preexec_fn=preexec_fn,
            )

            timed_out = False
            try:
                exit_code = process.wait(timeout=args.timeout_sec)
            except subprocess.TimeoutExpired:
                timed_out = True
                exit_code = None
                kill_process_tree(process)

        duration_sec = round(time.perf_counter() - started, 3)

        if timed_out:
            status = "TIMEOUT"
        elif exit_code == 0:
            status = "OK"
        else:
            status = "FAILED"

        trace_result: dict[str, Any] | None = None
        if args.fetch_trace:
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

        metrics = read_last_jsonl(metrics_file)
        notice = read_notice(stdout_file)
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
            "stderrFile": str(stderr_file),
            "metricsFile": str(metrics_file),
            "traceFile": str(trace_file) if args.fetch_trace else None,
            "traceSummaryFile": str(trace_summary_file) if args.fetch_trace else None,
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

    fieldnames = list(rows[0].keys()) if rows else []
    with summary_csv.open("w", encoding="utf-8-sig", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    summary_json.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")

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

    return 0


if __name__ == "__main__":
    raise SystemExit(main())