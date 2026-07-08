from __future__ import annotations

import json
from typing import Any

from ci_owner_agent.config import Settings, validate_model_settings
from ci_owner_agent.schemas import FailureFact, FailureFactComparison
from ci_owner_agent.services.llm_client import build_chat_model
from ci_owner_agent.services.metrics import llm_invoke_with_metrics


def compare_failure_facts_with_ai(
    *,
    settings: Settings,
    current_fact: FailureFact,
    historical_fact: FailureFact,
) -> FailureFactComparison:
    if not settings.ai_history_compare_enabled:
        return _comparison(False, 0, "unclear", "AI history compare disabled")
    if settings.model_provider.lower() == "fake":
        return _comparison(False, 0, "unclear", "AI history compare disabled for fake provider")
    if (
        not current_fact.historyEligible
        or not historical_fact.historyEligible
        or current_fact.isGenericWrapper
        or historical_fact.isGenericWrapper
    ):
        return _comparison(
            False,
            0,
            "blocked_by_generic_wrapper",
            "AI history compare blocked by generic wrapper or historyEligible=false fact",
        )
    if (
        current_fact.confidence < settings.ai_failure_fact_min_confidence
        or historical_fact.confidence < settings.ai_failure_fact_min_confidence
    ):
        return _comparison(False, 0, "blocked_by_low_confidence", "AI history compare blocked by low confidence fact")
    config_error = validate_model_settings(settings)
    if config_error:
        return _comparison(False, 0, "unclear", f"LLM 配置错误：{config_error}")

    try:
        model = build_chat_model(settings)
        response = llm_invoke_with_metrics(model, _build_prompt(current_fact=current_fact, historical_fact=historical_fact))
        raw = str(getattr(response, "content", response))
        data = _parse_json_object(raw)
        if data is None:
            return _comparison(False, 0, "unclear", "AI failure fact comparison invalid JSON")
        comparison = FailureFactComparison.model_validate(data)
    except Exception as exc:
        return _comparison(False, 0, "unclear", f"AI failure fact comparison validation failed: {exc}")

    if comparison.sameFailure and comparison.confidence < settings.ai_history_compare_threshold:
        comparison.sameFailure = False
        comparison.relationship = "unclear"
        comparison.reason = (
            f"{comparison.reason}；confidence {comparison.confidence:.2f} "
            f"is below threshold {settings.ai_history_compare_threshold:.2f}"
        )
    return comparison


def _build_prompt(*, current_fact: FailureFact, historical_fact: FailureFact) -> str:
    payload = {
        "currentFact": current_fact.model_dump(mode="json"),
        "historicalFact": historical_fact.model_dump(mode="json"),
    }
    return (
        "你是 CI 历史失败语义比对器。请判断 current_fact 和 historical_fact 是否是同一个历史持续失败。\n"
        "必须依据“内层真实 root cause”判断。禁止仅因为二者都发生在 Docker/Jenkins/npm/build 阶段就判断为 sameFailure。\n"
        "外层 wrapper 包括：ERROR: process \"/bin/sh -c ...\"、ERROR: failed to solve、Dockerfile:xx、make: ***、exit status 1。"
        "这些只能作为上下文，不能作为 failure identity。\n"
        "sameFailure=true 的强要求：错误类型/错误码相同或高度等价；关键对象相同，例如 packageName、filePath、symbol、依赖名、测试名；"
        "rootCauseSummary 表达的是同一个根因；不只是同一构建阶段或同一命令。\n"
        "示例：TS2305 classifyErrorMessage 未导出 vs npm ETARGET @ai-sdk/provider 版本不存在 => sameFailure=false。\n"
        "示例：TS2305 classifyErrorMessage 未导出 vs 同样的 TS2305 classifyErrorMessage 未导出 => sameFailure=true。\n"
        "只输出严格 JSON object，不要 Markdown，不要解释。格式：\n"
        "{\n"
        '  "sameFailure": false,\n'
        '  "confidence": 0.96,\n'
        '  "relationship": "different_root_cause",\n'
        '  "samePoints": ["都发生在 Docker build 中"],\n'
        '  "differentPoints": ["历史失败是 TypeScript TS2305 导出缺失", "当前失败是 npm ETARGET 依赖版本不存在"],\n'
        '  "reason": "二者外层阶段相似，但内层错误码、对象和根因不同，不能视为同一历史失败"\n'
        "}\n\n"
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


def _comparison(same: bool, confidence: float, relationship: str, reason: str) -> FailureFactComparison:
    return FailureFactComparison(
        sameFailure=same,
        confidence=confidence,
        relationship=relationship,  # type: ignore[arg-type]
        reason=reason,
        samePoints=[],
        differentPoints=[],
    )
