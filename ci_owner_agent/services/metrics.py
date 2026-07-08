from __future__ import annotations

import contextlib
import contextvars
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from ci_owner_agent.schemas import CiResponsibilityNotice

try:
    from langchain_core.callbacks import BaseCallbackHandler
except Exception:  # pragma: no cover - optional at import time for fake-provider tests.
    BaseCallbackHandler = object

_CURRENT_RECORDER: contextvars.ContextVar[AnalysisMetricsRecorder | None] = contextvars.ContextVar("ci_analysis_metrics_recorder", default=None)


def current_metrics_recorder() -> "AnalysisMetricsRecorder | None":
    return _CURRENT_RECORDER.get()


@contextlib.contextmanager
def use_metrics_recorder(recorder: "AnalysisMetricsRecorder | None") -> Iterator[None]:
    token = _CURRENT_RECORDER.set(recorder)
    try:
        yield
    finally:
        _CURRENT_RECORDER.reset(token)


@dataclass
class AnalysisMetricsRecorder:
    enabled: bool
    model_provider: str | None = None
    model_name: str | None = None
    job: str | None = None
    buildNumber: int | None = None
    repo: str | None = None
    command: str | None = None
    branch: str | None = None
    result: str | None = None
    status: str = "running"
    startedAt: str | None = None
    finishedAt: str | None = None
    durationMs: int | None = None
    stages: list[dict[str, Any]] = field(default_factory=list)
    llmCalls: int = 0
    inputTokens: int = 0
    outputTokens: int = 0
    totalTokens: int = 0
    tokenWarning: str | None = None
    hasHighConfidenceOwner: bool | None = None
    responsibilityItemCount: int | None = None
    inheritedOwnerCount: int | None = None
    currentBuildOwnerCount: int | None = None
    error: str | None = None
    warnings: list[str] = field(default_factory=list)
    _started_monotonic: float | None = None

    @classmethod
    def start(
        cls,
        *,
        enabled: bool,
        job: str,
        buildNumber: int,
        repo: str,
        command: str,
        model_provider: str | None = None,
        model_name: str | None = None,
    ) -> "AnalysisMetricsRecorder":
        recorder = cls(enabled=enabled, model_provider=model_provider, model_name=model_name)
        recorder.job = job
        recorder.buildNumber = buildNumber
        recorder.repo = repo
        recorder.command = command
        recorder.startedAt = _now_iso()
        recorder._started_monotonic = time.perf_counter()
        return recorder

    @contextlib.contextmanager
    def stage(self, name: str) -> Iterator[None]:
        if not self.enabled:
            yield
            return
        started = time.perf_counter()
        record: dict[str, Any] = {"name": name, "startedAt": _now_iso(), "status": "ok"}
        try:
            yield
        except Exception as exc:
            record["status"] = "error"
            record["error"] = str(exc)
            raise
        finally:
            record["durationMs"] = int((time.perf_counter() - started) * 1000)
            record["finishedAt"] = _now_iso()
            self.stages.append(record)

    def record_notice(self, notice: CiResponsibilityNotice) -> None:
        if not self.enabled:
            return
        self.branch = notice.branch
        self.result = notice.result
        self.hasHighConfidenceOwner = notice.hasHighConfidenceOwner
        self.responsibilityItemCount = len(notice.responsibilityItems)
        self.inheritedOwnerCount = sum(1 for item in notice.responsibilityItems if item.responsibilityType == "inherited_failure_owner")
        self.currentBuildOwnerCount = sum(1 for item in notice.responsibilityItems if item.responsibilityType == "current_build_owner")

    def record_error(self, exc: BaseException | str) -> None:
        if not self.enabled:
            return
        self.status = "error"
        self.error = str(exc)

    def record_token_usage(self, usage: dict[str, int | None]) -> None:
        if not self.enabled:
            return
        self.llmCalls += 1
        input_tokens = usage.get("inputTokens")
        output_tokens = usage.get("outputTokens")
        total_tokens = usage.get("totalTokens")
        if input_tokens is None and output_tokens is None and total_tokens is None:
            self.tokenWarning = "provider did not return token usage"
            return
        self.inputTokens += int(input_tokens or 0)
        self.outputTokens += int(output_tokens or 0)
        if total_tokens is not None:
            self.totalTokens += int(total_tokens or 0)
        else:
            self.totalTokens += int(input_tokens or 0) + int(output_tokens or 0)

    def finish(self) -> None:
        if not self.enabled:
            return
        if self.status == "running":
            self.status = "ok" if self.error is None else "error"
        self.finishedAt = _now_iso()
        if self._started_monotonic is not None:
            self.durationMs = int((time.perf_counter() - self._started_monotonic) * 1000)

    def to_record(self) -> dict[str, Any]:
        return {
            "job": self.job,
            "buildNumber": self.buildNumber,
            "repo": self.repo,
            "branch": self.branch,
            "result": self.result,
            "command": self.command,
            "status": self.status,
            "startedAt": self.startedAt,
            "finishedAt": self.finishedAt,
            "durationMs": self.durationMs,
            "modelProvider": self.model_provider,
            "modelName": self.model_name,
            "llmCalls": self.llmCalls,
            "inputTokens": self.inputTokens,
            "outputTokens": self.outputTokens,
            "totalTokens": self.totalTokens,
            "tokenWarning": self.tokenWarning,
            "stages": self.stages,
            "toolCalls": None,
            "hasHighConfidenceOwner": self.hasHighConfidenceOwner,
            "responsibilityItemCount": self.responsibilityItemCount,
            "inheritedOwnerCount": self.inheritedOwnerCount,
            "currentBuildOwnerCount": self.currentBuildOwnerCount,
            "error": self.error,
            "warnings": self.warnings,
        }

    def append_jsonl(self, path: str | Path) -> None:
        if not self.enabled:
            return
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as fp:
            fp.write(json.dumps(self.to_record(), ensure_ascii=False, default=str) + "\n")


class TokenUsageCallbackHandler(BaseCallbackHandler):
    def __init__(self, recorder: AnalysisMetricsRecorder) -> None:
        self.recorder = recorder

    def on_llm_end(self, response: Any, **kwargs: Any) -> None:
        try:
            self.recorder.record_token_usage(_extract_token_usage(response))
        except Exception as exc:
            self.recorder.warnings.append(f"token usage callback failed: {exc}")


def llm_invoke_with_metrics(model: Any, input: Any, **kwargs: Any) -> Any:
    recorder = current_metrics_recorder()
    if recorder is None or not recorder.enabled:
        return model.invoke(input, **kwargs)
    config = dict(kwargs.pop("config", {}) or {})
    callbacks = list(config.get("callbacks") or [])
    handler = TokenUsageCallbackHandler(recorder)
    callbacks.append(handler)
    config["callbacks"] = callbacks
    before_calls = recorder.llmCalls
    response = model.invoke(input, config=config, **kwargs)
    if recorder.llmCalls == before_calls:
        handler.on_llm_end(response)
    return response


def _extract_token_usage(obj: Any) -> dict[str, int | None]:
    usage = _find_usage(obj)
    if not usage:
        return {"inputTokens": None, "outputTokens": None, "totalTokens": None}
    input_tokens = _first_int(usage, ["input_tokens", "prompt_tokens", "inputTokens", "promptTokens"])
    output_tokens = _first_int(usage, ["output_tokens", "completion_tokens", "outputTokens", "completionTokens"])
    total_tokens = _first_int(usage, ["total_tokens", "totalTokens"])
    return {"inputTokens": input_tokens, "outputTokens": output_tokens, "totalTokens": total_tokens}


def _find_usage(obj: Any, depth: int = 0) -> dict[str, Any] | None:
    if obj is None or depth > 6:
        return None
    if isinstance(obj, dict):
        for key in ("usage_metadata", "token_usage", "usage", "llm_output", "response_metadata"):
            value = obj.get(key)
            if isinstance(value, dict):
                if _looks_like_usage(value):
                    return value
                found = _find_usage(value, depth + 1)
                if found:
                    return found
        if _looks_like_usage(obj):
            return obj
        for value in obj.values():
            found = _find_usage(value, depth + 1)
            if found:
                return found
        return None
    for attr in ("usage_metadata", "response_metadata", "llm_output", "generations", "message", "text", "content"):
        value = getattr(obj, attr, None)
        if isinstance(value, dict):
            if _looks_like_usage(value):
                return value
            found = _find_usage(value, depth + 1)
            if found:
                return found
        elif value is not None and not isinstance(value, (str, bytes)):
            found = _find_usage(value, depth + 1)
            if found:
                return found
    if isinstance(obj, (list, tuple)):
        for item in obj:
            found = _find_usage(item, depth + 1)
            if found:
                return found
    return None


def _looks_like_usage(value: dict[str, Any]) -> bool:
    keys = {
        "input_tokens",
        "prompt_tokens",
        "output_tokens",
        "completion_tokens",
        "total_tokens",
        "inputTokens",
        "outputTokens",
        "totalTokens",
    }
    return any(key in value for key in keys)


def _first_int(value: dict[str, Any], keys: list[str]) -> int | None:
    for key in keys:
        item = value.get(key)
        if item is None:
            continue
        try:
            return int(item)
        except (TypeError, ValueError):
            continue
    return None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
