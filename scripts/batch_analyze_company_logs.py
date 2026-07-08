from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

BUILD_NO_RE = re.compile(r"(\d+)(?=\.log$)", re.I)
STATUS_RE = re.compile(r"Finished:\s+([A-Z]+)")

HEAD_COMMIT_PATTERNS = [
    re.compile(r"Checking out Revision\s+([0-9a-f]{40})", re.I),
    re.compile(r"git checkout(?:\s+-f)?\s+([0-9a-f]{40})", re.I),
    re.compile(r"\bGIT_COMMIT=([0-9a-f]{40})\b", re.I),
    re.compile(r"\bHEAD_COMMIT=([0-9a-f]{40})\b", re.I),
]


@dataclass
class BuildLog:
    build: int
    path: Path
    status: str | None
    head_commit: str | None
    base_commit: str | None = None
    last_success_build_number: int | None = None
    previous_build_number: int | None = None
    previous_commit: str | None = None
    skip_reason: str | None = None


def load_env_file(env_file: Path, override: bool = False) -> None:
    if not env_file.exists():
        print(f"env file not found, skip: {env_file}")
        return

    try:
        from dotenv import load_dotenv

        load_dotenv(env_file, override=override)
        print(f"loaded env file: {env_file}")
        return
    except Exception as exc:
        print(f"python-dotenv unavailable, using simple .env parser: {exc}")

    for raw_line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()

        if value.startswith(("'", '"')) and value.endswith(("'", '"')) and len(value) >= 2:
            value = value[1:-1]

        if override or key not in os.environ:
            os.environ[key] = value

    print(f"loaded env file with fallback parser: {env_file}")


def slug(value: str) -> str:
    value = value.replace("/", "_").replace("\\", "_")
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_")


def short(commit: str | None, n: int = 12) -> str:
    return commit[:n] if commit else "none"


def extract_build_number(path: Path) -> int | None:
    match = BUILD_NO_RE.search(path.name)
    return int(match.group(1)) if match else None


def parse_status(text: str) -> str | None:
    matches = STATUS_RE.findall(text)
    return matches[-1] if matches else None


def parse_head_commit(text: str) -> str | None:
    commits: list[str] = []
    for pattern in HEAD_COMMIT_PATTERNS:
        commits.extend(pattern.findall(text))

    # Jenkins 日志里可能出现多次 checkout，取最后一次更接近实际构建 head。
    return commits[-1] if commits else None


def parse_build_log(path: Path) -> BuildLog | None:
    build = extract_build_number(path)
    if build is None:
        return None

    text = path.read_text(encoding="utf-8", errors="replace")
    return BuildLog(
        build=build,
        path=path,
        status=parse_status(text),
        head_commit=parse_head_commit(text),
    )


def load_logs(log_dir: Path, log_glob: str, initial_base_commit: str | None = None) -> list[BuildLog]:
    logs: list[BuildLog] = []

    for path in sorted(log_dir.glob(log_glob)):
        if not path.is_file():
            continue
        item = parse_build_log(path)
        if item:
            logs.append(item)

    logs.sort(key=lambda x: x.build)

    last_success_commit = initial_base_commit
    last_success_build_number: int | None = None
    previous_build_number: int | None = None
    previous_commit: str | None = None

    for item in logs:
        if item.status == "SUCCESS":
            if item.head_commit:
                last_success_commit = item.head_commit
                last_success_build_number = item.build
                previous_build_number = item.build
                previous_commit = item.head_commit
            else:
                item.skip_reason = "success build missing head commit"
            continue

        if item.status in {"ABORTED", "NOT_BUILT"}:
            item.skip_reason = f"skip status {item.status}"
            continue

        if item.status != "FAILURE":
            item.skip_reason = f"unknown status {item.status}"
            continue

        if not item.head_commit:
            item.skip_reason = "missing head commit"
            continue

        if not last_success_commit:
            item.skip_reason = "missing previous successful commit"
            continue

        item.base_commit = last_success_commit
        item.last_success_build_number = last_success_build_number
        if previous_build_number is not None:
            item.previous_build_number = previous_build_number
            item.previous_commit = previous_commit

        previous_build_number = item.build
        previous_commit = item.head_commit

    return logs


def in_build_range(item: BuildLog, build_from: int | None, build_to: int | None) -> bool:
    if build_from is not None and item.build < build_from:
        return False
    if build_to is not None and item.build > build_to:
        return False
    return True


def filter_logs_by_build_range(logs: list[BuildLog], build_from: int | None, build_to: int | None) -> list[BuildLog]:
    return [item for item in logs if in_build_range(item, build_from, build_to)]


def cleanup_previous_outputs(paths: list[Path]) -> list[str]:
    warnings: list[str] = []
    for path in paths:
        try:
            if path.exists():
                path.unlink()
        except Exception as exc:
            warnings.append(f"failed to remove {path}: {exc}")
    return warnings


def should_skip_for_resume(resume: bool, notice_path: Path) -> bool:
    return resume and notice_path.exists()


def extract_first_json_object(text: str) -> dict[str, Any] | None:
    decoder = json.JSONDecoder()
    for idx, ch in enumerate(text):
        if ch != "{":
            continue
        try:
            obj, _end = decoder.raw_decode(text[idx:])
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


def extract_history_stats_from_notice(notice: dict[str, Any]) -> dict[str, Any]:
    empty = {
        "historicalMatchCount": None,
        "topHistoricalMatchBuild": None,
        "topHistoricalMatchSimilarity": None,
        "topHistoricalMatchType": None,
        "topHistoricalRelationship": None,
    }
    history = notice.get("history_search_similar_failures")
    if isinstance(history, dict):
        return _history_stats_from_result(history, empty)
    for key in ["historySearchSimilarFailures", "history"]:
        value = notice.get(key)
        if isinstance(value, dict) and "candidates" in value:
            return _history_stats_from_result(value, empty)

    for evidence in notice.get("evidence") or []:
        if not isinstance(evidence, dict):
            continue
        if evidence.get("source") != "history_search_similar_failures":
            continue
        structured = evidence.get("history_search_similar_failures") or evidence.get("result")
        if isinstance(structured, dict):
            return _history_stats_from_result(structured, empty)
        parsed = _history_stats_from_text(" ".join(str(evidence.get(field) or "") for field in ["summary", "detail"]))
        if parsed is not None:
            return {**empty, **parsed}
    return empty


def extract_responsibility_stats_from_notice(notice: dict[str, Any]) -> dict[str, Any]:
    items = notice.get("responsibilityItems")
    if not isinstance(items, list):
        items = []
    responsible_owners: list[str] = []
    inherited_owners: list[str] = []
    current_build_owners: list[str] = []
    unresolved = 0

    for item in items:
        if not isinstance(item, dict):
            continue
        owner = item.get("owner") if isinstance(item.get("owner"), dict) else {}
        owner_name = str(owner.get("name") or "")
        owner_type = str(owner.get("type") or "")
        responsibility_type = str(item.get("responsibilityType") or "")
        is_unresolved = (
            responsibility_type in {"no_high_confidence_owner", "unknown"}
            or owner_type == "no_high_confidence_owner"
            or not owner_name
            or owner_name == "无高可信责任人"
        )
        if is_unresolved:
            unresolved += 1
            continue
        if responsibility_type == "inherited_failure_owner":
            inherited_owners.append(owner_name)
            source_build = item.get("sourceBuildNumber")
            suffix = f"inherited from #{source_build}" if source_build is not None else "inherited"
            responsible_owners.append(f"{owner_name}({suffix})")
        elif responsibility_type == "current_build_owner":
            current_build_owners.append(owner_name)
            responsible_owners.append(f"{owner_name}({owner_type})")
        else:
            responsible_owners.append(f"{owner_name}({owner_type or responsibility_type})")

    return {
        "responsibilityItemCount": len(items),
        "responsibleOwners": "; ".join(_unique_in_order(responsible_owners)),
        "inheritedOwners": "; ".join(_unique_in_order(inherited_owners)),
        "currentBuildOwners": "; ".join(_unique_in_order(current_build_owners)),
        "unresolvedFailureCount": unresolved,
    }


def _history_stats_from_result(result: dict[str, Any], empty: dict[str, Any]) -> dict[str, Any]:
    if result.get("warning") or result.get("ok") is False:
        return {**empty, "historicalMatchCount": 0}
    candidates = result.get("candidates")
    if not isinstance(candidates, list):
        return empty
    if not candidates:
        return {**empty, "historicalMatchCount": 0}
    top = candidates[0] if isinstance(candidates[0], dict) else {}
    return {
        "historicalMatchCount": len(candidates),
        "topHistoricalMatchBuild": top.get("buildNumber"),
        "topHistoricalMatchSimilarity": top.get("similarity"),
        "topHistoricalMatchType": top.get("matchType"),
        "topHistoricalRelationship": top.get("relationship"),
    }


def _history_stats_from_text(text: str) -> dict[str, Any] | None:
    if "history_search_similar_failures" not in text:
        return None
    result: dict[str, Any] = {}
    count_match = re.search(r"(?:返回|returned)\s*(\d+)\s*(?:个)?候选|(\d+)\s+candidates", text, re.I)
    if count_match:
        result["historicalMatchCount"] = int(next(group for group in count_match.groups() if group))
    elif "warning" in text.lower():
        result["historicalMatchCount"] = 0
    build_match = re.search(r"(?:top\s*)?build(?:Number)?\s*[=: ]\s*(\d+)|build\s+(\d+)", text, re.I)
    if build_match:
        result["topHistoricalMatchBuild"] = int(next(group for group in build_match.groups() if group))
    similarity_match = re.search(r"similarity\s*[=: ]\s*([0-9]+(?:\.[0-9]+)?)", text, re.I)
    if similarity_match:
        result["topHistoricalMatchSimilarity"] = float(similarity_match.group(1))
    match_type = re.search(r"matchType\s*[=: ]\s*([A-Za-z_]+)", text)
    if match_type:
        result["topHistoricalMatchType"] = match_type.group(1)
    relationship = re.search(r"relationship\s*[=: ]\s*([A-Za-z_]+)", text)
    if relationship:
        result["topHistoricalRelationship"] = relationship.group(1)
    return result or None


def _unique_in_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def to_jsonable(obj: Any) -> Any:
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj

    if isinstance(obj, (dt.datetime, dt.date)):
        return obj.isoformat()

    if isinstance(obj, dict):
        return {str(k): to_jsonable(v) for k, v in obj.items()}

    if isinstance(obj, (list, tuple, set)):
        return [to_jsonable(v) for v in obj]

    if hasattr(obj, "model_dump"):
        try:
            return obj.model_dump(mode="json")
        except Exception:
            try:
                return obj.model_dump()
            except Exception:
                pass

    if hasattr(obj, "dict"):
        try:
            return obj.dict()
        except Exception:
            pass

    return str(obj)


def build_analyze_command(
    *,
    item: BuildLog,
    repo: str,
    job: str,
    branch: str,
    build_url_prefix: str,
) -> list[str]:
    assert item.base_commit
    assert item.head_commit

    command = [
        sys.executable,
        "-m",
        "ci_owner_agent",
        "analyze-local",
        "--repo",
        repo,
        "--job",
        job,
        "--build",
        str(item.build),
        "--branch",
        branch,
        "--base-commit",
        item.base_commit,
        "--head-commit",
        item.head_commit,
        "--console-file",
        str(item.path),
        "--build-url",
        f"{build_url_prefix.rstrip('/')}/{item.build}",
    ]
    if item.last_success_build_number is not None:
        command.extend(["--last-success-build", str(item.last_success_build_number)])
    if item.previous_build_number is not None:
        command.extend(["--previous-build", str(item.previous_build_number)])
    if item.previous_commit is not None:
        command.extend(["--previous-commit", item.previous_commit])
    return command


def run_analyze_local(
    *,
    item: BuildLog,
    repo: str,
    job: str,
    branch: str,
    build_url_prefix: str,
    cwd: Path,
    env: dict[str, str],
    timeout_seconds: int,
) -> subprocess.CompletedProcess[str]:
    cmd = build_analyze_command(
        item=item,
        repo=repo,
        job=job,
        branch=branch,
        build_url_prefix=build_url_prefix,
    )

    return subprocess.run(
        cmd,
        cwd=str(cwd),
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout_seconds,
        check=False,
    )


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


def metadata_matches(md: dict[str, Any], *, item: BuildLog, repo: str, job: str) -> bool:
    return (
        str(md.get("job")) == job
        and str(md.get("repo")) == repo
        and str(md.get("buildNumber")) == str(item.build)
        and str(md.get("baseCommit")) == str(item.base_commit)
        and str(md.get("headCommit")) == str(item.head_commit)
    )


def fetch_langsmith_trace(
    *,
    item: BuildLog,
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
                key=lambda r: getattr(
                    r,
                    "start_time",
                    dt.datetime.min.replace(tzinfo=dt.timezone.utc),
                ),
                reverse=True,
            )

            for run in runs:
                full = client.read_run(getattr(run, "id"), load_child_runs=True)
                md = get_run_metadata(full)

                if not metadata_matches(md, item=item, repo=repo, job=job):
                    continue

                payload = to_jsonable(full)

                try:
                    payload["_langsmith_url"] = client.get_run_url(
                        run=full,
                        project_name=project_name,
                    )
                except Exception:
                    pass

                trace_path.write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2, default=str),
                    encoding="utf-8",
                )

                return {
                    "ok": True,
                    "traceFile": str(trace_path),
                    "runId": str(getattr(full, "id", "")),
                    "url": payload.get("_langsmith_url"),
                }

        except Exception as exc:
            last_error = str(exc)

        time.sleep(2)

    return {
        "ok": False,
        "error": last_error or "trace not found before timeout",
    }


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


def main() -> int:
    parser = argparse.ArgumentParser()

    parser.add_argument("--log-dir", required=True)
    parser.add_argument("--log-glob", default="*.log")

    parser.add_argument("--out-dir", default=None)
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--env-override", action="store_true")

    parser.add_argument("--repo", default="fx-code")
    parser.add_argument("--job", default="services/fx-code-unittest")
    parser.add_argument("--branch", default="dev")
    parser.add_argument("--build-url-prefix", default="local://services/fx-code-unittest")

    parser.add_argument("--initial-base-commit", default=None)

    parser.add_argument("--langsmith-project", default=None)
    parser.add_argument("--fetch-trace", action="store_true")
    parser.add_argument("--trace-wait-seconds", type=int, default=30)

    parser.add_argument("--timeout-seconds", type=int, default=900)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--build-from", type=int, default=None)
    parser.add_argument("--build-to", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--resume", action="store_true")

    args = parser.parse_args()
    if args.build_from is not None and args.build_to is not None and args.build_from > args.build_to:
        parser.error("--build-from must be <= --build-to")

    repo_root = Path.cwd()
    log_dir = Path(args.log_dir).resolve()

    env_file = Path(args.env_file)
    if not env_file.is_absolute():
        env_file = repo_root / env_file

    load_env_file(env_file, override=args.env_override)

    batch_id = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = Path(args.out_dir or f"runs/company-log-batch-{batch_id}").resolve()

    notices_dir = out_dir / "notices"
    stdout_dir = out_dir / "stdout"
    stderr_dir = out_dir / "stderr"
    traces_dir = out_dir / "traces"
    metrics_dir = out_dir / "metrics"

    for d in [out_dir, notices_dir, stdout_dir, stderr_dir, traces_dir, metrics_dir]:
        d.mkdir(parents=True, exist_ok=True)

    project_name = (
        args.langsmith_project
        or os.environ.get("LANGCHAIN_PROJECT")
        or os.environ.get("LANGSMITH_PROJECT")
        or f"ci-owner-agent-batch-{batch_id}"
    )

    logs_all = load_logs(
        log_dir=log_dir,
        log_glob=args.log_glob,
        initial_base_commit=args.initial_base_commit,
    )
    logs = filter_logs_by_build_range(logs_all, args.build_from, args.build_to)

    runnable_failures = [
        item for item in logs if item.status == "FAILURE" and not item.skip_reason
    ]

    if args.limit > 0:
        runnable_failures = runnable_failures[: args.limit]

    runnable_builds = {item.build for item in runnable_failures}

    index_path = out_dir / "index.jsonl"
    summary_path = out_dir / "summary.csv"

    env = os.environ.copy()

    # 关键：强制子进程 Python 使用 UTF-8 输出，避免 Windows GBK 导致中文乱码或 UnicodeEncodeError
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"

    env["LANGCHAIN_PROJECT"] = project_name
    env["LANGSMITH_PROJECT"] = project_name
    env.setdefault("LANGCHAIN_TRACING_V2", "true")
    env.setdefault("LANGSMITH_TRACING", "true")

    # Metrics: enabled per-build, each build writes its own metrics file
    env["CI_AGENT_METRICS_ENABLED"] = "true" 

    print(f"repo_root={repo_root}")
    print(f"log_dir={log_dir}")
    print(f"out_dir={out_dir}")
    print(f"env_file={env_file}")
    print(f"langsmith_project={project_name}")
    print(f"build_from={args.build_from}")
    print(f"build_to={args.build_to}")
    print(f"total_logs_all={len(logs_all)}")
    print(f"total_logs_selected={len(logs)}")
    print(f"runnable_failures={len(runnable_failures)}")

    rows: list[dict[str, Any]] = []

    with index_path.open("w", encoding="utf-8") as index_file:
        for item in logs:
            if item.status != "FAILURE" or item.skip_reason:
                record = {
                    "build": item.build,
                    "status": item.status,
                    "headCommit": item.head_commit,
                    "baseCommit": item.base_commit,
                    "lastSuccessfulBuildNumber": item.last_success_build_number,
                    "previousBuildNumber": item.previous_build_number,
                    "previousCommit": item.previous_commit,
                    "consoleFile": str(item.path),
                    "skipped": True,
                    "skipReason": item.skip_reason or f"skip status {item.status}",
                }
                index_file.write(json.dumps(record, ensure_ascii=False) + "\n")
                rows.append(record)
                continue

            if item.build not in runnable_builds:
                continue

            name = (
                f"{slug(args.job)}_{item.build}_{item.status}_"
                f"{short(item.base_commit)}_{short(item.head_commit)}"
            )

            notice_path = notices_dir / f"{name}.notice.json"
            stdout_path = stdout_dir / f"{name}.stdout.txt"
            stderr_path = stderr_dir / f"{name}.stderr.txt"
            trace_path = traces_dir / f"{name}.trace.json"

            command = build_analyze_command(
                item=item,
                repo=args.repo,
                job=args.job,
                branch=args.branch,
                build_url_prefix=args.build_url_prefix,
            )

            record: dict[str, Any] = {
                "build": item.build,
                "status": item.status,
                "baseCommit": item.base_commit,
                "headCommit": item.head_commit,
                "lastSuccessfulBuildNumber": item.last_success_build_number,
                "previousBuildNumber": item.previous_build_number,
                "previousCommit": item.previous_commit,
                "consoleFile": str(item.path),
                "noticeFile": str(notice_path),
                "stdoutFile": str(stdout_path),
                "stderrFile": str(stderr_path),
                "traceFile": str(trace_path),
                "command": command,
                "skipped": False,
            }

            print(f"\n=== build {item.build} ===")
            print(f"base={item.base_commit}")
            print(f"head={item.head_commit}")
            print("cmd=" + " ".join(command))

            if args.dry_run:
                record["dryRun"] = True
                index_file.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
                rows.append(record)
                continue

            if should_skip_for_resume(args.resume, notice_path):
                record["skipped"] = True
                record["skipReason"] = "resume: notice already exists"
                index_file.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
                rows.append(record)
                print(f"skip existing: {notice_path}")
                continue

            cleanup_warnings = cleanup_previous_outputs([notice_path, stdout_path, stderr_path, trace_path])
            if cleanup_warnings:
                record["cleanupWarning"] = "; ".join(cleanup_warnings)

            started_at = dt.datetime.now(dt.timezone.utc)

            # Per-build metrics file
            metrics_file = metrics_dir / f"{name}.metrics.jsonl"
            env["CI_AGENT_METRICS_FILE"] = str(metrics_file)

            started = time.perf_counter()
            try:
                cp = run_analyze_local(
                    item=item,
                    repo=args.repo,
                    job=args.job,
                    branch=args.branch,
                    build_url_prefix=args.build_url_prefix,
                    cwd=repo_root,
                    env=env,
                    timeout_seconds=args.timeout_seconds,
                )

                stdout_path.write_text(cp.stdout, encoding="utf-8")
                stderr_path.write_text(cp.stderr, encoding="utf-8")

                record["returnCode"] = cp.returncode
                record["durationSec"] = round(time.perf_counter() - started, 3)

                notice = extract_first_json_object(cp.stdout)
                if notice is not None:
                    notice_path.write_text(
                        json.dumps(notice, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )

                    owner = notice.get("owner") or {}
                    record["ownerType"] = owner.get("type")
                    record["ownerName"] = owner.get("name")
                    record["ownerEmail"] = owner.get("email")
                    record["ownerCommit"] = owner.get("commit")
                    record["hasHighConfidenceOwner"] = notice.get(
                        "hasHighConfidenceOwner"
                    )
                    record["failureReason"] = notice.get("failureReason")
                    record["historyEnabled"] = os.environ.get("CI_AGENT_HISTORY_ENABLED", "false").lower() in {"1", "true", "yes", "on"}
                    record.update(extract_history_stats_from_notice(notice))
                    record.update(extract_responsibility_stats_from_notice(notice))
                else:
                    record["noticeParseError"] = True

                if args.fetch_trace:
                    trace_result = fetch_langsmith_trace(
                        item=item,
                        repo=args.repo,
                        job=args.job,
                        project_name=project_name,
                        started_at=started_at,
                        trace_path=trace_path,
                        wait_seconds=args.trace_wait_seconds,
                    )
                    record["trace"] = trace_result

            except subprocess.TimeoutExpired as exc:
                record["durationSec"] = round(time.perf_counter() - started, 3)
                record["error"] = f"analyze-local timeout after {args.timeout_seconds}s"
                stdout_path.write_text(exc.stdout or "", encoding="utf-8")
                stderr_path.write_text(exc.stderr or "", encoding="utf-8")
                cleanup_warnings = cleanup_previous_outputs([notice_path, trace_path])
                if cleanup_warnings:
                    existing = record.get("cleanupWarning")
                    record["cleanupWarning"] = "; ".join(
                        [str(existing)] + cleanup_warnings if existing else cleanup_warnings
                    )

            except Exception as exc:
                record["durationSec"] = round(time.perf_counter() - started, 3)
                record["error"] = str(exc)

            # Read metrics after each build (success, timeout, or exception)
            metrics = read_last_jsonl(metrics_file)
            if metrics:
                record["metricsDurationMs"] = metrics.get("durationMs")
                record["llmCalls"] = metrics.get("llmCalls")
                record["inputTokens"] = metrics.get("inputTokens")
                record["outputTokens"] = metrics.get("outputTokens")
                record["totalTokens"] = metrics.get("totalTokens")
                record["tokenWarning"] = metrics.get("tokenWarning")
                record["stageCount"] = len(metrics.get("stages") or []) if metrics else None
            record["metricsFile"] = str(metrics_file)

            index_file.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
            index_file.flush()
            rows.append(record)

    fieldnames = [
        "build",
        "status",
        "skipped",
        "skipReason",
        "baseCommit",
        "headCommit",
        "lastSuccessfulBuildNumber",
        "previousBuildNumber",
        "previousCommit",
        "durationSec",
        "historyEnabled",
        "historicalMatchCount",
        "topHistoricalMatchBuild",
        "topHistoricalMatchSimilarity",
        "topHistoricalMatchType",
        "topHistoricalRelationship",
        "responsibilityItemCount",
        "responsibleOwners",
        "inheritedOwners",
        "currentBuildOwners",
        "unresolvedFailureCount",
        "returnCode",
        "ownerType",
        "ownerName",
        "ownerEmail",
        "ownerCommit",
        "hasHighConfidenceOwner",
        "noticeFile",
        "traceFile",
        "failureReason",
        "metricsDurationMs",
        "llmCalls",
        "inputTokens",
        "outputTokens",
        "totalTokens",
        "tokenWarning",
        "stageCount",
        "metricsFile",
        "cleanupWarning",
        "error",
    ]

    with summary_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    print("\nDone.")
    print(f"index:   {index_path}")
    print(f"summary: {summary_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
