from __future__ import annotations

import argparse
import csv
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
    parser.add_argument("--result", choices=["SUCCESS", "FAILURE", "UNSTABLE", "ABORTED", "UNKNOWN"], default=None)

    parser.add_argument("--out-dir", default="./runs/rerun-analyze-local")
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--ignore-checkout-commit-mismatch", action="store_true")
    parser.add_argument("--notify", action="store_true")
    parser.add_argument("--force-notify", action="store_true")

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

    lines = [line.strip() for line in path.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip()]
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
        elif responsibility_type in {"no_high_confidence_owner", "unknown"} or owner_type == "no_high_confidence_owner":
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

    rows: list[dict[str, Any]] = []
    command = build_command(args)

    print(f"Command template:")
    print(" ".join(f'"{part}"' if " " in part else part for part in command))
    print()

    for run_index in range(1, args.runs + 1):
        run_name = f"run-{run_index:02d}"
        run_dir = out_dir / run_name
        run_dir.mkdir(parents=True, exist_ok=True)

        stdout_file = run_dir / "stdout.json"
        stderr_file = run_dir / "stderr.log"
        metrics_file = run_dir / "metrics.jsonl"
        command_file = run_dir / "command.txt"

        command_file.write_text(
            " ".join(f'"{part}"' if " " in part else part for part in command),
            encoding="utf-8",
        )

        env = os.environ.copy()
        env["CI_AGENT_METRICS_ENABLED"] = "true"
        env["CI_AGENT_METRICS_FILE"] = str(metrics_file)

        if not args.notify:
            env["CI_AGENT_WECOM_NOTIFY_ENABLED"] = "false"

        print(f"========== {run_name} / {args.runs} ==========")
        print(f"stdout : {stdout_file}")
        print(f"stderr : {stderr_file}")
        print(f"metrics: {metrics_file}")

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
            "stdoutFile": str(stdout_file),
            "stderrFile": str(stderr_file),
            "metricsFile": str(metrics_file),
        }

        rows.append(row)

        print(
            f"{run_name} result: {status}, "
            f"duration={duration_sec}s, "
            f"exitCode={exit_code}, "
            f"llmCalls={row['llmCalls']}, "
            f"totalTokens={row['totalTokens']}"
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