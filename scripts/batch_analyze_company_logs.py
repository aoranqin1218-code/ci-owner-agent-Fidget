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
from typing import Any, Literal

_SCRIPT_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_SCRIPT_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_REPO_ROOT))
from scripts._runtime import REPO_ROOT, build_subprocess_env, ensure_repo_on_sys_path, resolve_repo_default_path, resolve_user_path, run_process_bounded, sha256_file, success_marker_path, validate_success_marker, write_success_marker_atomic

ensure_repo_on_sys_path()
from ci_owner_agent.schemas import CiResponsibilityNotice
from ci_owner_agent.services.branch_normalization import normalize_branch_name
from ci_owner_agent.services.log_provider import log_detect_final_status, resolve_checkout_revision_from_console_log, resolve_final_status_from_console_log

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

BUILD_NO_RE = re.compile(r"(\d+)(?=\.log$)", re.I)
SHA_RE = re.compile(r"^[0-9a-f]{40}$", re.I)


@dataclass
class BuildLog:
    build: int
    console_path: Path | None
    status: str
    head_commit: str | None
    branch: str | None = None
    checkout_refs: tuple[str, ...] = ()
    normalized_checkout_branches: tuple[str, ...] = ()
    invalid_checkout_refs: tuple[str, ...] = ()
    branch_error: str | None = None
    checkout_ambiguous: bool = False
    checkout_error: str | None = None
    branch_ambiguous: bool = False
    status_error: str | None = None
    status_detected: bool = False
    status_raw: str | None = None
    build_timestamp: str | None = None
    source: Literal["log", "manifest", "log+manifest"] = "log"
    baseline_source: str | None = None
    baseline_from_observed_success: bool = False
    base_commit: str | None = None
    last_success_build_number: int | None = None
    previous_build_number: int | None = None
    previous_commit: str | None = None
    skip_reason: str | None = None
    skip_kind: str | None = None
    validation_failed: bool = False
    error_kind: str | None = None


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


def parse_status(text: str) -> str:
    return log_detect_final_status(text)


def parse_head_commit(text: str) -> str | None:
    return resolve_checkout_revision_from_console_log(text).commit


def parse_build_log(path: Path) -> BuildLog | None:
    build = extract_build_number(path)
    if build is None:
        return None

    text = path.read_text(encoding="utf-8", errors="replace")
    resolution = resolve_checkout_revision_from_console_log(text)
    status_resolution = resolve_final_status_from_console_log(text)
    normalized = tuple(sorted({branch for branch in (normalize_branch_name(ref) for ref in resolution.refs) if branch}))
    invalid = tuple(sorted(ref for ref in resolution.refs if normalize_branch_name(ref) is None))
    branch_error = "invalid checkout ref" if invalid else ("ambiguous checkout branch" if len(normalized) > 1 else None)
    return BuildLog(
        build=build,
        console_path=path,
        status=status_resolution.status,
        head_commit=resolution.commit,
        branch=normalized[0] if len(normalized) == 1 else None,
        checkout_refs=resolution.refs,
        normalized_checkout_branches=normalized,
        invalid_checkout_refs=invalid,
        branch_error=branch_error,
        checkout_ambiguous=resolution.ambiguous,
        checkout_error="ambiguous trusted checkout commit" if resolution.ambiguous else ("missing trusted checkout commit" if not resolution.commit else None),
        branch_ambiguous=len(normalized) > 1,
        status_error=status_resolution.error,
        status_detected=status_resolution.detected,
        status_raw=status_resolution.raw_status,
    )


def load_manifest(path: Path) -> list[BuildLog]:
    """Load a deliberately small, strictly validated supplemental build history."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"invalid manifest JSON: {exc}") from exc
    if not isinstance(payload, list):
        raise ValueError("manifest must be a JSON list")
    results = {"SUCCESS", "FAILURE", "UNSTABLE", "ABORTED", "NOT_BUILT", "UNKNOWN"}
    seen: set[int] = set()
    items: list[BuildLog] = []
    for entry in payload:
        if not isinstance(entry, dict) or set(entry) - {"build", "result", "branch", "headCommit", "buildTimestamp"}:
            raise ValueError("manifest entry has invalid fields")
        build, result, raw_branch, commit = entry.get("build"), entry.get("result"), entry.get("branch"), entry.get("headCommit")
        if type(build) is not int or build < 0 or build in seen:
            raise ValueError("manifest build must be a unique non-negative integer")
        if not isinstance(result, str) or result.upper() not in results:
            raise ValueError("manifest result is invalid")
        branch = normalize_branch_name(raw_branch if isinstance(raw_branch, str) else None)
        if branch is None:
            raise ValueError("manifest branch is invalid")
        if not isinstance(commit, str) or not SHA_RE.fullmatch(commit):
            raise ValueError("manifest headCommit must be a 40-character SHA")
        timestamp = entry.get("buildTimestamp")
        if timestamp is not None and not isinstance(timestamp, str):
            raise ValueError("manifest buildTimestamp must be a string")
        seen.add(build)
        items.append(BuildLog(build, None, result.upper(), commit.lower(), branch=branch, build_timestamp=timestamp, source="manifest"))
    return items


def merge_build_history(logs: list[BuildLog], manifest: list[BuildLog]) -> list[BuildLog]:
    by_build = {item.build: item for item in logs}
    for item in manifest:
        existing = by_build.get(item.build)
        if existing is not None:
            if (existing.head_commit != item.head_commit or existing.branch != item.branch
                    or existing.status != item.status
                    or (existing.build_timestamp and item.build_timestamp and existing.build_timestamp != item.build_timestamp)):
                raise ValueError(f"manifest conflicts with log for build {item.build}")
            existing.source = "log+manifest"
            existing.build_timestamp = existing.build_timestamp or item.build_timestamp
            continue
        by_build[item.build] = item
    return sorted(by_build.values(), key=lambda item: item.build)


def _apply_history(logs: list[BuildLog], initial_base_commit: str | None, branch: str) -> list[BuildLog]:
    """Apply the same branch-scoped history rules after a manifest merge."""
    for item in logs:
        item.base_commit = item.previous_commit = None
        item.last_success_build_number = item.previous_build_number = None
        item.baseline_source = item.skip_reason = item.skip_kind = item.error_kind = None
        item.validation_failed = False
    return _assign_history(logs, initial_base_commit, branch)


def _assign_history(logs: list[BuildLog], initial_base_commit: str | None, branch: str | None) -> list[BuildLog]:
    configured_branch = normalize_branch_name(branch) if branch else "dev"
    baselines: dict[str, tuple[str, int | None, str]] = {}
    previous: dict[str, tuple[int, str]] = {}
    if initial_base_commit:
        baselines[configured_branch] = (initial_base_commit, None, "initial-base-commit")

    def validation_failure(item: BuildLog, kind: str, reason: str) -> None:
        item.skip_reason, item.skip_kind, item.error_kind = reason, "validation", kind
        item.validation_failed = True

    for item in sorted(logs, key=lambda value: value.build):
        if item.status_error:
            validation_failure(item, "status_validation", item.status_error)
            continue
        if item.branch_error:
            validation_failure(item, "branch_validation", item.branch_error)
            continue
        effective_branch = item.branch or configured_branch
        if item.branch and item.branch != configured_branch:
            validation_failure(item, "branch_validation", f"branch mismatch: log={item.branch} requested={configured_branch}")
            continue
        item.branch, item.baseline_from_observed_success = effective_branch, False
        if item.checkout_ambiguous or item.branch_ambiguous:
            validation_failure(item, "checkout_validation" if item.checkout_ambiguous else "branch_validation", item.checkout_error or "ambiguous branch"); continue
        if item.status == "SUCCESS":
            if item.head_commit:
                baselines[effective_branch] = (item.head_commit, item.build, f"success-{item.source}")
                previous[effective_branch] = (item.build, item.head_commit)
            else: validation_failure(item, "checkout_validation", "success build missing trusted checkout commit")
            continue
        if item.status in {"ABORTED", "NOT_BUILT"}:
            item.skip_reason, item.skip_kind = f"skip status {item.status}", "status"; continue
        if not item.head_commit:
            validation_failure(item, "checkout_validation", "missing trusted checkout commit"); continue
        if item.console_path is None:
            validation_failure(item, "manifest_validation", "failure build missing real console log"); continue
        baseline = baselines.get(effective_branch)
        if not baseline:
            validation_failure(item, "baseline_validation", "missing reliable successful baseline"); continue
        item.base_commit, item.last_success_build_number, item.baseline_source = baseline
        item.baseline_from_observed_success = item.baseline_source.startswith("success-")
        if effective_branch in previous: item.previous_build_number, item.previous_commit = previous[effective_branch]
        previous[effective_branch] = (item.build, item.head_commit)
    return logs


def load_logs(log_dir: Path, log_glob: str, initial_base_commit: str | None = None, branch: str | None = None) -> list[BuildLog]:
    logs: list[BuildLog] = []

    for path in sorted(log_dir.glob(log_glob)):
        if not path.is_file():
            continue
        item = parse_build_log(path)
        if item:
            logs.append(item)

    logs.sort(key=lambda x: x.build)

    return _assign_history(logs, initial_base_commit, branch)


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
            if path.exists():
                warnings.append(f"failed to remove {path}: path still exists")
        except Exception as exc:
            warnings.append(f"failed to remove {path}: {exc}")
    return warnings


def validate_resume_notice(path: Path, *, marker_path: Path, item: BuildLog, repo: str, job: str, branch: str) -> tuple[bool, str | None]:
    expected = {
        "repo": repo, "job": job, "buildNumber": item.build, "branch": branch,
        "result": item.status, "baseCommit": item.base_commit, "headCommit": item.head_commit,
    }
    try:
        notice = CiResponsibilityNotice.model_validate_json(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return validate_success_marker(marker_path, path, {"workflow": "company", **expected})
    except Exception as exc:
        return False, f"invalid notice: {type(exc).__name__}"
    for field, value in expected.items():
        if getattr(notice, field) != value:
            return False, f"resume metadata mismatch: {field}"
    return validate_success_marker(marker_path, path, {"workflow": "company", **expected})


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
    notice_path: Path | None = None,
    python_executable: str = sys.executable,
) -> list[str]:
    assert item.base_commit
    assert item.head_commit
    assert item.console_path

    command = [
        python_executable,
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
        str(item.console_path),
        "--build-url",
        f"{build_url_prefix.rstrip('/')}/{item.build}",
        "--result",
        item.status,
    ]
    if item.last_success_build_number is not None:
        command.extend(["--last-success-build", str(item.last_success_build_number)])
    if item.previous_build_number is not None:
        command.extend(["--previous-build", str(item.previous_build_number)])
    if item.previous_commit is not None:
        command.extend(["--previous-commit", item.previous_commit])
    if item.build_timestamp:
        command.extend(["--build-timestamp", item.build_timestamp])
    if notice_path is not None:
        command.extend(["--output-file", str(notice_path)])
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
    notice_path: Path | None = None,
    python_executable: str = sys.executable,
) -> subprocess.CompletedProcess[str]:
    cmd = build_analyze_command(
        item=item,
        repo=repo,
        job=job,
        branch=branch,
        build_url_prefix=build_url_prefix,
        notice_path=notice_path,
        python_executable=python_executable,
    )

    return run_process_bounded(cmd, cwd=cwd, env=env, timeout_seconds=timeout_seconds)


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
    parser.add_argument("--env-file", default=None)
    parser.add_argument("--env-override", action="store_true")

    parser.add_argument("--repo", default="fx-code")
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--job", default="services/fx-code-unittest")
    parser.add_argument("--branch", default="dev")
    parser.add_argument("--build-url-prefix", default="local://services/fx-code-unittest")

    parser.add_argument("--initial-base-commit", default=None)
    parser.add_argument("--manifest-file", default=None)

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

    branch = normalize_branch_name(args.branch)
    if not branch:
        parser.error("--branch must be a logical branch name")
    repo_root = REPO_ROOT
    log_dir = resolve_user_path(args.log_dir)

    env_file = resolve_user_path(args.env_file) if args.env_file else resolve_repo_default_path(".env")

    load_env_file(env_file, override=args.env_override)

    batch_id = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = resolve_user_path(args.out_dir) if args.out_dir else resolve_repo_default_path(f"runs/company-log-batch-{batch_id}")

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
        branch=branch,
    )
    if args.manifest_file:
        manifest_path = resolve_user_path(args.manifest_file)
        try:
            manifest = load_manifest(manifest_path)
            logs_all = merge_build_history(logs_all, manifest)
            # Recalculate history using the merged, branch-scoped timeline.
            logs_all = _apply_history(logs_all, args.initial_base_commit, branch)
        except ValueError as exc:
            parser.error(str(exc))
    logs = filter_logs_by_build_range(logs_all, args.build_from, args.build_to)

    runnable_builds_list = [item for item in logs if item.status in {"FAILURE", "UNSTABLE", "UNKNOWN"} and not item.skip_reason]

    if args.limit > 0:
        runnable_builds_list = runnable_builds_list[: args.limit]

    runnable_builds = {item.build for item in runnable_builds_list}

    index_path = out_dir / "index.jsonl"
    summary_path = out_dir / "summary.csv"

    env = build_subprocess_env()

    # 关键：强制子进程 Python 使用 UTF-8 输出，避免 Windows GBK 导致中文乱码或 UnicodeEncodeError

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
    print(f"runnable_builds={len(runnable_builds_list)}")

    rows: list[dict[str, Any]] = []

    with index_path.open("w", encoding="utf-8") as index_file:
        for item in logs:
            if item.status not in {"FAILURE", "UNSTABLE", "UNKNOWN"} or item.skip_reason:
                record = {
                    "build": item.build,
                    "status": item.status,
                    "headCommit": item.head_commit,
                    "branch": item.branch,
                    "checkoutAmbiguous": item.checkout_ambiguous,
                    "statusDetected": item.status_detected,
                    "statusRaw": item.status_raw,
                    "statusError": item.status_error,
                    "baselineSource": item.baseline_source,
                    "baselineFromObservedSuccess": item.baseline_from_observed_success,
                    "baseCommit": item.base_commit,
                    "lastSuccessfulBuildNumber": item.last_success_build_number,
                    "previousBuildNumber": item.previous_build_number,
                    "previousCommit": item.previous_commit,
                    "consoleFile": str(item.console_path) if item.console_path else None,
                    "skipped": not item.validation_failed,
                    "validationFailed": item.validation_failed,
                    "errorKind": item.error_kind,
                    "error": item.skip_reason if item.validation_failed else None,
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
            marker_path = success_marker_path(notice_path)
            stdout_path = stdout_dir / f"{name}.stdout.txt"
            stderr_path = stderr_dir / f"{name}.stderr.txt"
            trace_path = traces_dir / f"{name}.trace.json"

            command = build_analyze_command(
                item=item,
                repo=args.repo,
                job=args.job,
                branch=branch,
                build_url_prefix=args.build_url_prefix,
                notice_path=notice_path,
                python_executable=args.python,
            )

            record: dict[str, Any] = {
                "build": item.build,
                "status": item.status,
                "baseCommit": item.base_commit,
                "headCommit": item.head_commit,
                "branch": item.branch,
                "checkoutAmbiguous": item.checkout_ambiguous,
                "statusDetected": item.status_detected,
                "statusRaw": item.status_raw,
                "statusError": item.status_error,
                "baselineSource": item.baseline_source,
                "baselineFromObservedSuccess": item.baseline_from_observed_success,
                "lastSuccessfulBuildNumber": item.last_success_build_number,
                "previousBuildNumber": item.previous_build_number,
                "previousCommit": item.previous_commit,
                "consoleFile": str(item.console_path) if item.console_path else None,
                "noticeFile": str(notice_path),
                "successMarkerFile": str(marker_path),
                "resumable": False,
                "stdoutFile": str(stdout_path),
                "stderrFile": str(stderr_path),
                "traceFile": str(trace_path),
                "command": command,
                "skipped": False,
                "executionSkipped": False,
            }

            print(f"\n=== build {item.build} ===")
            print(f"base={item.base_commit}")
            print(f"head={item.head_commit}")
            print("cmd=" + " ".join(command))

            if args.dry_run:
                record["dryRun"] = True
                record["executionSkipped"] = True
                index_file.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
                rows.append(record)
                continue

            metrics_file = metrics_dir / f"{name}.metrics.jsonl"

            if args.resume:
                valid, reason = validate_resume_notice(notice_path, marker_path=marker_path, item=item, repo=args.repo, job=args.job, branch=branch)
                record["resumeValidated"] = valid
                record["resumeInvalidReason"] = reason
                if valid:
                    record.update({"resumable": True, "successMarkerValid": True, "noticeValid": True, "returnCode": 0,
                                   "skipped": True, "executionSkipped": True,
                                   "skipReason": "resume: validated successful execution", "resumeInvalidReason": None})
                    index_file.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
                    rows.append(record)
                    continue
                resume_cleanup = cleanup_previous_outputs([notice_path, marker_path])
                if resume_cleanup:
                    message = "; ".join(resume_cleanup)
                    record.update({"cleanupWarning": message, "errorKind": "cleanup", "error": message, "noticeValid": False})
                    index_file.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
                    rows.append(record)
                    continue

            cleanup_warnings = cleanup_previous_outputs([notice_path, marker_path, stdout_path, stderr_path, trace_path, metrics_file])
            if cleanup_warnings:
                message = "; ".join(cleanup_warnings)
                record.update({"cleanupWarning": message, "errorKind": "cleanup", "error": message, "noticeValid": False})
                index_file.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
                rows.append(record)
                continue

            started_at = dt.datetime.now(dt.timezone.utc)

            env["CI_AGENT_METRICS_FILE"] = str(metrics_file)

            started = time.perf_counter()
            timed_out = False
            try:
                cp = run_analyze_local(
                    item=item,
                    repo=args.repo,
                    job=args.job,
                    branch=branch,
                    build_url_prefix=args.build_url_prefix,
                    cwd=repo_root,
                    env=env,
                    timeout_seconds=args.timeout_seconds,
                    notice_path=notice_path,
                    python_executable=args.python,
                )

                stdout_path.write_text(cp.stdout, encoding="utf-8")
                stderr_path.write_text(cp.stderr, encoding="utf-8")

                record["returnCode"] = cp.returncode
                if cp.returncode != 0:
                    record["errorKind"] = "execution"
                    record["error"] = f"analyze-local exited with {cp.returncode}"
                record["durationSec"] = round(time.perf_counter() - started, 3)

                try:
                    notice_model = CiResponsibilityNotice.model_validate_json(notice_path.read_text(encoding="utf-8"))
                    notice = notice_model.model_dump(mode="json")
                    if (notice_model.repo != args.repo or notice_model.job != args.job or notice_model.buildNumber != item.build
                            or notice_model.branch != branch or notice_model.result != item.status
                            or notice_model.baseCommit != item.base_commit or notice_model.headCommit != item.head_commit):
                        raise ValueError("notice metadata mismatch")
                    record["noticeValid"] = True

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
                    if cp.returncode == 0 and not record.get("errorKind"):
                        payload = {"schemaVersion": 1, "kind": "ci-owner-agent-resume-success", "workflow": "company",
                                   "noticeFile": notice_path.name, "noticeSha256": sha256_file(notice_path), "returnCode": 0,
                                   "repo": args.repo, "job": args.job, "buildNumber": item.build, "branch": branch,
                                   "result": item.status, "baseCommit": item.base_commit, "headCommit": item.head_commit,
                                   "completedAt": dt.datetime.now(dt.timezone.utc).isoformat()}
                        try:
                            write_success_marker_atomic(marker_path, payload)
                            record.update({"resumable": True, "successMarkerValid": True})
                        except Exception as marker_exc:
                            record.update({"errorKind": "resume_marker", "error": f"failed to write success marker: {marker_exc}",
                                           "successMarkerValid": False, "successMarkerError": str(marker_exc)})
                except Exception as exc:
                    record["noticeValid"] = False
                    record["noticeValidationError"] = f"invalid output notice: {type(exc).__name__}: {exc}"
                    if not record.get("errorKind"):
                        record["errorKind"] = "notice_validation"
                        record["error"] = record["noticeValidationError"]

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
                timed_out = True
                record["durationSec"] = round(time.perf_counter() - started, 3)
                record["error"] = f"analyze-local timeout after {args.timeout_seconds}s"
                record["errorKind"] = "timeout"
                record["noticeValid"] = False
                termination = getattr(exc, "termination", None)
                record["terminationReaped"] = getattr(termination, "reaped", None)
                record["terminationWarning"] = getattr(termination, "warning", None)
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
                record["errorKind"] = "execution"

            # Read metrics after each build (success, timeout, or exception)
            metrics = None if timed_out else read_last_jsonl(metrics_file)
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
        "branch",
        "checkoutAmbiguous",
        "statusDetected",
        "statusRaw",
        "statusError",
        "baselineSource",
        "baselineFromObservedSuccess",
        "validationFailed",
        "noticeValid",
        "resumable",
        "successMarkerFile",
        "successMarkerValid",
        "successMarkerError",
        "executionSkipped",
        "resumeValidated",
        "resumeInvalidReason",
        "errorKind",
        "noticeValidationError",
        "terminationReaped",
        "terminationWarning",
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

    return 1 if any(row.get("validationFailed") or (not row.get("skipped") and (row.get("error") or row.get("returnCode") not in (None, 0) or row.get("noticeValid") is False)) for row in rows) else 0


if __name__ == "__main__":
    raise SystemExit(main())
