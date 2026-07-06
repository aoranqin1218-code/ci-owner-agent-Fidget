from __future__ import annotations

import json
from typing import Any

from ci_owner_agent.config import Settings, validate_model_settings
from ci_owner_agent.schemas import ChangedFile, CommitInfo, FailureFactExtractionResult
from ci_owner_agent.services.llm_client import build_chat_model


def extract_failure_facts_with_ai(
    *,
    settings: Settings,
    job: str,
    build_number: int,
    build_url: str,
    branch: str | None,
    log_excerpt: str,
    changed_files: list[ChangedFile],
    commits: list[CommitInfo],
) -> FailureFactExtractionResult:
    if not settings.ai_failure_facts_enabled:
        return FailureFactExtractionResult(ok=False, warning="AI failure facts disabled")
    if settings.model_provider.lower() == "fake":
        return FailureFactExtractionResult(ok=False, warning="AI failure facts disabled for fake provider")
    if not log_excerpt.strip():
        return FailureFactExtractionResult(ok=False, warning="empty log excerpt")
    config_error = validate_model_settings(settings)
    if config_error:
        return FailureFactExtractionResult(ok=False, warning=f"LLM 配置错误：{config_error}")

    prompt = _build_prompt(
        job=job,
        build_number=build_number,
        build_url=build_url,
        branch=branch,
        log_excerpt=log_excerpt[: settings.ai_failure_fact_max_log_chars],
        changed_files=changed_files,
        commits=commits,
    )
    try:
        model = build_chat_model(settings)
        response = model.invoke(prompt)
        raw = str(getattr(response, "content", response))
        data = _parse_json_object(raw)
        if data is None:
            return FailureFactExtractionResult(ok=False, warning="AI failure facts invalid JSON")
        result = FailureFactExtractionResult.model_validate(data)
    except Exception as exc:
        return FailureFactExtractionResult(ok=False, warning=f"AI failure facts extraction failed: {exc}")

    if result.ok is False:
        return FailureFactExtractionResult(
            ok=False,
            facts=[],
            warning=result.warning or "AI failure facts extraction returned ok=false",
        )

    kept = [
        fact
        for fact in result.facts
        if fact.signatureKey.strip() and fact.confidence >= settings.ai_failure_fact_min_confidence
    ]
    return FailureFactExtractionResult(ok=True, facts=kept, warning=result.warning)


def _build_prompt(
    *,
    job: str,
    build_number: int,
    build_url: str,
    branch: str | None,
    log_excerpt: str,
    changed_files: list[ChangedFile],
    commits: list[CommitInfo],
) -> str:
    payload = {
        "job": job,
        "buildNumber": build_number,
        "buildUrl": build_url,
        "branch": branch,
        "changedFiles": [item.model_dump() for item in changed_files[:30]],
        "commits": [item.model_dump() for item in commits[:20]],
        "logExcerpt": log_excerpt,
    }
    return (
        "你是 CI 日志失败事实提取器。请从日志中提取“内层真实失败事实”，"
        "不要把 Jenkins、Docker、BuildKit、make、shell 的外层 wrapper 当作 failure identity。\n"
        "外层 wrapper 只能作为上下文，不能单独成为可历史继承事实。\n"
        "请只输出严格 JSON object，不要 Markdown，不要解释。格式：\n"
        "{\n"
        '  "ok": true,\n'
        '  "facts": [\n'
        "    {\n"
        '      "schemaVersion": 1,\n'
        '      "signatureKey": "...",\n'
        '      "historyEligible": true,\n'
        '      "isGenericWrapper": false,\n'
        '      "failureKind": "...",\n'
        '      "phase": "...",\n'
        '      "command": "...",\n'
        '      "errorCode": "...",\n'
        '      "errorType": "...",\n'
        '      "packageName": "...",\n'
        '      "filePath": "...",\n'
        '      "symbol": "...",\n'
        '      "message": "...",\n'
        '      "rootCauseSummary": "...",\n'
        '      "evidenceLines": [],\n'
        '      "startLine": null,\n'
        '      "endLine": null,\n'
        '      "confidence": 0.9\n'
        "    }\n"
        "  ],\n"
        '  "warning": null\n'
        "}\n"
        "如果只能看到外层 Docker/Jenkins wrapper，请输出 historyEligible=false, "
        "isGenericWrapper=true, failureKind=generic_wrapper, confidence<=0.5。\n\n"
        f"输入：\n{json.dumps(payload, ensure_ascii=False)}"
    )


def _parse_json_object(raw: str) -> dict[str, Any] | None:
    text = raw.strip()
    if text.startswith("```"):
        import re

        match = re.search(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.S | re.I)
        if match:
            text = match.group(1).strip()
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else None
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        for idx, char in enumerate(raw):
            if char != "{":
                continue
            try:
                value, _end = decoder.raw_decode(raw[idx:])
            except json.JSONDecodeError:
                continue
            return value if isinstance(value, dict) else None
    return None
