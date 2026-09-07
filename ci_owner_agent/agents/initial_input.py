"""Pure payload construction for the LangChain responsibility agent."""

from __future__ import annotations

from typing import Any

from ci_owner_agent.agents.context import AgentRuntimeContext
from ci_owner_agent.agents.prompts import INITIAL_INPUT_INSTRUCTION




def build_initial_input_payload(
    context: AgentRuntimeContext,
    failure_summaries: dict[str, Any] | None,
) -> dict[str, Any]:
    initial_scope = "focus" if context.investigation_scope and context.investigation_scope.has_focus_range else "full"
    return {
        "job": context.job,
        "buildNumber": context.build_number,
        "buildUrl": context.build_url,
        "result": context.result,
        "branch": context.branch,
        "baseCommit": context.base_commit,
        "headCommit": context.head_commit,
        "lastSuccessfulBuildNumber": context.last_successful_build_number,
        "integrationBaseline": context.integration_baseline,
        "integrationSuiteBaselines": context.integration_suite_baselines,
        "investigationScope": compact_investigation_scope(context),
        "initialDiffScope": initial_scope,
        "changedFilesScope": initial_scope,
        "changedFiles": [item.model_dump() for item in context.changed_files[:30]],
        "changedFilesTotal": len(context.changed_files),
        "changedFilesTruncated": len(context.changed_files) > 30,
        "commitsScope": initial_scope,
        "commits": [item.model_dump() for item in context.commits[:20]],
        "commitsTotal": len(context.commits),
        "commitsTruncated": len(context.commits) > 20,
        "failureSummaries": compact_failure_summaries(failure_summaries),
        "failureSummaryWarning": failure_summaries.get("warning") if isinstance(failure_summaries, dict) else None,
        "failureFacts": compact_failure_facts(context.failure_facts),
        "logTailMeta": log_tail_meta(context.build_info.logTail),
        "historyPrecheck": compact_history_precheck(context.history_precheck),
        "aiHistoryPrecheck": compact_ai_history_precheck(context.ai_history_precheck),
        "instruction": INITIAL_INPUT_INSTRUCTION,
    }


def compact_investigation_scope(context: AgentRuntimeContext) -> dict[str, Any] | None:
    scope = context.investigation_scope
    if scope is None:
        return None
    return {
        "mode": scope.mode,
        "fullBaseCommit": scope.full_base_commit,
        "fullHeadCommit": scope.full_head_commit,
        "focusBaseCommit": scope.focus_base_commit,
        "focusHeadCommit": scope.focus_head_commit,
        "previousBuildNumber": scope.previous_build_number,
        "previousBuildUrl": scope.previous_build_url,
        "previousBuildResult": scope.previous_build_result,
        "reason": scope.reason,
    }


def compact_failure_summaries(summaries: dict[str, Any] | None) -> list[dict[str, Any]]:
    chunks = summaries.get("chunks", []) if isinstance(summaries, dict) else []
    compact: list[dict[str, Any]] = []
    for idx, chunk in enumerate(chunks[:5]):
        # 方案 X：coverage 责任项由确定性 reconciler 单一写入，不把 coverage 失败块
        # 作为线索喂给 Agent（Agent 只处理 Japa 等非 coverage 失败）。
        if isinstance(chunk, dict) and chunk.get("anchorType") == "coverage_failure_block":
            continue
        content = str(chunk.get("content") or "")
        compact.append(
            {
                "chunkIndex": chunk.get("chunkIndex", idx),
                "chunkSource": chunk.get("chunkSource"),
                "startLine": chunk.get("startLine"),
                "endLine": chunk.get("endLine"),
                "signature": chunk.get("signature") or {},
                "integrationSuite": chunk.get("integrationSuite"),
                "content": truncate_summary_content(content),
            }
        )
    return compact


def truncate_summary_content(content: str, limit: int = 4000) -> str:
    if len(content) <= limit:
        return content
    return content[:limit] + "\n...[truncated]"


def compact_failure_facts(facts_result: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(facts_result, dict):
        return None
    return {
        "ok": facts_result.get("ok"),
        "warning": facts_result.get("warning"),
        "facts": [
            {
                "factId": fact.get("factId"),
                "signatureKey": fact.get("signatureKey"),
                "historyEligible": fact.get("historyEligible"),
                "isGenericWrapper": fact.get("isGenericWrapper"),
                "failureKind": fact.get("failureKind"),
                "phase": fact.get("phase"),
                "command": fact.get("command"),
                "errorCode": fact.get("errorCode"),
                "errorType": fact.get("errorType"),
                "packageName": fact.get("packageName"),
                "filePath": fact.get("filePath"),
                "symbol": fact.get("symbol"),
                "message": fact.get("message"),
                "rootCauseSummary": fact.get("rootCauseSummary"),
                "confidence": fact.get("confidence"),
            }
            for fact in (facts_result.get("facts") or [])[:5]
            if isinstance(fact, dict)
        ],
    }


def compact_ai_history_precheck(precheck: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(precheck, dict):
        return None
    return {
        "ok": precheck.get("ok"),
        "mode": precheck.get("mode"),
        "previousBuildNumber": precheck.get("previousBuildNumber"),
        "threshold": precheck.get("threshold"),
        "warning": precheck.get("warning"),
        "error": precheck.get("error"),
        "diagnostics": compact_ai_history_diagnostics(precheck.get("diagnostics")),
        "currentFacts": [
            {
                "factId": item.get("factId"),
                "signatureKey": item.get("signatureKey"),
                "failureKind": item.get("failureKind"),
                "errorCode": item.get("errorCode"),
                "packageName": item.get("packageName"),
                "filePath": item.get("filePath"),
                "symbol": item.get("symbol"),
                "confidence": item.get("confidence"),
                "blockedReason": item.get("blockedReason"),
                "inheritedOwner": compact_inherited_owner(item.get("inheritedOwner")),
            }
            for item in (precheck.get("currentFacts") or [])[:5]
            if isinstance(item, dict)
        ],
        "candidates": [
            {
                "currentFactId": item.get("currentFactId"),
                "historicalFactId": item.get("historicalFactId"),
                "buildNumber": item.get("buildNumber"),
                "buildUrl": item.get("buildUrl"),
                "sameFailure": item.get("sameFailure"),
                "confidence": item.get("confidence"),
                "relationship": item.get("relationship"),
                "matchType": item.get("matchType"),
                "reason": item.get("reason"),
                "ownerName": item.get("ownerName"),
                "ownerEmail": item.get("ownerEmail"),
                "ownerCommit": item.get("ownerCommit"),
                "feedbackOverride": compact_feedback_override(item.get("feedbackOverride")),
            }
            for item in (precheck.get("candidates") or [])[:5]
            if isinstance(item, dict)
        ],
    }


def compact_ai_history_diagnostics(diagnostics: Any) -> dict[str, Any] | None:
    if not isinstance(diagnostics, dict):
        return None
    return {
        "eligibleCurrentFactsCount": diagnostics.get("eligibleCurrentFactsCount"),
        "historicalBuildsCount": diagnostics.get("historicalBuildsCount"),
        "historicalFactsCount": diagnostics.get("historicalFactsCount"),
        "rankedPairsCount": diagnostics.get("rankedPairsCount"),
        "comparedPairsCount": diagnostics.get("comparedPairsCount"),
        "acceptedCandidatesCount": diagnostics.get("acceptedCandidatesCount"),
        "skipped": diagnostics.get("skipped"),
        "queryStage": diagnostics.get("queryStage"),
        "historicalBuildNumbers": diagnostics.get("historicalBuildNumbers"),
        "historicalFactBuildNumbers": diagnostics.get("historicalFactBuildNumbers"),
        "compareResults": [
            {
                "currentFactId": item.get("currentFactId"),
                "currentSignatureKey": item.get("currentSignatureKey"),
                "historicalFactId": item.get("historicalFactId"),
                "historicalSignatureKey": item.get("historicalSignatureKey"),
                "historicalBuildNumber": item.get("historicalBuildNumber"),
                "sameFailure": item.get("sameFailure"),
                "confidence": item.get("confidence"),
                "relationship": item.get("relationship"),
                "accepted": item.get("accepted"),
                "skipReason": item.get("skipReason"),
                "reason": item.get("reason"),
            }
            for item in (diagnostics.get("compareResults") or [])[:5]
            if isinstance(item, dict)
        ],
    }


def log_tail_meta(log_tail: Any | None) -> dict[str, Any] | None:
    if log_tail is None:
        return None
    content = str(getattr(log_tail, "content", "") or "")
    return {
        "startLine": getattr(log_tail, "startLine", None),
        "endLine": getattr(log_tail, "endLine", None),
        "contentChars": len(content),
        "omitted": True,
        "reason": "full logTail.content omitted from initial prompt; use log_read_tail/log_search/log_read_range tools if needed",
    }


def compact_history_precheck(precheck: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(precheck, dict):
        return precheck
    compact: dict[str, Any] = {
        "ok": precheck.get("ok"),
        "historyEnabled": precheck.get("historyEnabled"),
        "currentBuild": precheck.get("currentBuild"),
        "previousBuildNumber": precheck.get("previousBuildNumber"),
        "lastSuccessfulBuildNumber": precheck.get("lastSuccessfulBuildNumber"),
        "lastSuccessfulBuildNumberMissing": precheck.get("lastSuccessfulBuildNumberMissing"),
        "warning": precheck.get("warning"),
        "error": precheck.get("error"),
        "stage": precheck.get("stage"),
        "candidateCount": len(precheck.get("candidates") or []),
        "currentChunks": [
            {
                "chunkIndex": item.get("chunkIndex"),
                "normalizedHash": item.get("normalizedHash"),
                "signature": item.get("signature") or {},
                "inheritedOwner": compact_inherited_owner(item.get("inheritedOwner")),
            }
            for item in (precheck.get("currentChunks") or [])[:5]
            if isinstance(item, dict)
        ],
        "candidates": [],
        "instruction": (
            "只有 currentChunks[*].inheritedOwner.found=true，当前 failure item 才属于历史持续失败。"
            "历史候选可以来自更早构建；continuityEligible=true 表示 Git 已确认该失败相关的致因或测试文件"
            "从历史失败后未被修改，中间构建未执行到该测试不会切断责任链。"
            "continuityEligible=false 表示相关文件已被改动或连续性无法可信验证，必须按当前 build 重新分析。"
            "如果 inheritedOwner.found=true，应在 responsibilityItems 中输出 responsibilityType=inherited_failure_owner，"
            "owner 使用 inheritedOwner；顶层 owner 不要因为 inherited owner 而输出 high_confidence，"
            "顶层 owner 通常保持 no_high_confidence_owner。"
            "如果 failureSummaries 只有 1 个，historyPrecheck.currentChunks 只有 1 个，"
            "且 currentChunks[0].inheritedOwner.found=true，且没有其他独立失败迹象，"
            "应直接输出最终 CiResponsibilityNotice JSON：顶层 owner 使用 no_high_confidence_owner，"
            "hasHighConfidenceOwner=false，responsibilityItems 只包含 1 个 item，"
            "responsibilityType=inherited_failure_owner，owner 使用 inheritedOwner，"
            "sourceBuildNumber 使用 inheritedOwner.sourceBuildNumber，matchType / relationship 使用 inheritedOwner 或候选中的值。"
            "不要继续调用 repo/log/ts 工具补充当前 build diff 证据；"
            "inherited failure 的责任来自首次失败 build，不需要重新证明当前 build diff。"
            "如果还有其他独立新失败，应继续分别分析并生成独立 responsibilityItems。"
        ),
    }
    for candidate in (precheck.get("candidates") or [])[:5]:
        if not isinstance(candidate, dict):
            continue
        compact["candidates"].append(
            {
                "buildNumber": candidate.get("buildNumber"),
                "headCommit": candidate.get("headCommit"),
                "similarity": candidate.get("similarity"),
                "relationship": candidate.get("relationship"),
                "matchType": candidate.get("matchType"),
                "continuityEligible": candidate.get("continuityEligible"),
                "continuityReason": candidate.get("continuityReason"),
                "continuityRelevantPaths": candidate.get("continuityRelevantPaths") or [],
                "continuityTouchedPaths": candidate.get("continuityTouchedPaths") or [],
                "ownerType": candidate.get("ownerType"),
                "ownerName": candidate.get("ownerName"),
                "ownerCommit": candidate.get("ownerCommit"),
                "hasHighConfidenceOwner": candidate.get("hasHighConfidenceOwner"),
                "failureReason": candidate.get("failureReason"),
                "signature": candidate.get("signature"),
                "historicalSignature": candidate.get("historicalSignature"),
                "historicalSignatureHash": candidate.get("historicalSignatureHash"),
                "inheritedOwner": compact_inherited_owner(candidate.get("inheritedOwner")),
            }
        )
    return compact


def compact_inherited_owner(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or not value.get("found"):
        return {"found": False}
    return {
        "found": True,
        "sourceBuildNumber": value.get("sourceBuildNumber"),
        "sourceBuildUrl": value.get("sourceBuildUrl"),
        "ownerType": value.get("ownerType"),
        "ownerName": value.get("ownerName"),
        "ownerEmail": value.get("ownerEmail"),
        "ownerCommit": value.get("ownerCommit"),
        "confidence": value.get("confidence"),
        "matchType": value.get("matchType"),
        "relationship": value.get("relationship"),
        "feedbackVerified": value.get("feedbackVerified"),
        "feedbackCorrected": value.get("feedbackCorrected"),
    }


def compact_feedback_override(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    return {
        "action": value.get("action"),
        "reviewer": value.get("reviewer"),
        "note": value.get("note"),
        "correctedOwner": value.get("correctedOwner"),
    }
