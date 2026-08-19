"""Shared LangSmith root-run polling for batch and rerun utilities."""
from __future__ import annotations

import datetime as dt
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from scripts._batch_common import to_jsonable
from scripts._runtime import get_langsmith_run_metadata


def find_matching_root_run(
    *,
    project_name: str,
    started_at: dt.datetime,
    wait_seconds: int,
    metadata_matches: Callable[[dict[str, Any]], bool],
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
                key=lambda run: getattr(
                    run,
                    "start_time",
                    dt.datetime.min.replace(tzinfo=dt.timezone.utc),
                ),
                reverse=True,
            )
            for run in runs:
                full = client.read_run(getattr(run, "id"), load_child_runs=True)
                if not metadata_matches(get_langsmith_run_metadata(full)):
                    continue
                try:
                    url = client.get_run_url(run=full, project_name=project_name)
                except Exception:
                    url = None
                return {"ok": True, "run": full, "url": url}
        except Exception as exc:
            last_error = str(exc)
        time.sleep(2)
    return {"ok": False, "error": last_error or "trace not found before timeout"}


def serialize_langsmith_run(run: Any) -> dict[str, Any]:
    """Convert a LangSmith run to a bounded, JSON-compatible trace payload."""
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
    payload: dict[str, Any] = {}
    for field in fields:
        try:
            value = getattr(run, field, None)
        except Exception:
            continue
        if value is not None:
            payload[field] = _to_trace_jsonable(value)
    if "metadata" not in payload:
        metadata = get_langsmith_run_metadata(run)
        if metadata:
            payload["metadata"] = _to_trace_jsonable(metadata)
    return payload


def write_trace_artifact(
    trace_path: Path,
    run: Any,
    url: str | None,
    *,
    serializer: Callable[[Any], dict[str, Any]] = to_jsonable,
) -> dict[str, Any]:
    """Write one root-run artifact and return the exact payload written."""
    payload = serializer(run)
    if url:
        payload["_langsmith_url"] = url
    trace_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return payload


def summarize_trace_payload(payload: dict[str, Any]) -> dict[str, Any]:
    runs = _flatten_runs(payload)
    usage_dicts = _find_usage_dicts(payload)
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


def _to_trace_jsonable(obj: Any, depth: int = 0) -> Any:
    if depth > 12:
        return str(obj)
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, (dt.datetime, dt.date)):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {str(key): _to_trace_jsonable(value, depth + 1) for key, value in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_to_trace_jsonable(value, depth + 1) for value in obj]
    for method_name in ("model_dump", "dict"):
        method = getattr(obj, method_name, None)
        if callable(method):
            try:
                return _to_trace_jsonable(method(), depth + 1)
            except Exception:
                pass
    if hasattr(obj, "__dict__"):
        try:
            return {
                str(key): _to_trace_jsonable(value, depth + 1)
                for key, value in vars(obj).items()
                if not str(key).startswith("_")
            }
        except Exception:
            pass
    return str(obj)


def _flatten_runs(payload: dict[str, Any]) -> list[dict[str, Any]]:
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

    walk(payload)
    return result


def _find_usage_dicts(payload: dict[str, Any]) -> list[dict[str, Any]]:
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
    found: list[dict[str, Any]] = []

    def walk(value: Any, depth: int = 0) -> None:
        if depth > 14:
            return
        if isinstance(value, dict):
            if any(key in value for key in usage_keys):
                found.append(value)
            for child in value.values():
                walk(child, depth + 1)
        elif isinstance(value, list):
            for child in value:
                walk(child, depth + 1)

    walk(payload)
    return found
