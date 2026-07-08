from __future__ import annotations

import json
import os
import re
from typing import Any

from ci_owner_agent.agents.context import AgentRuntimeContext
from ci_owner_agent.agents.prompts import (
    CI_RESPONSIBILITY_NOTICE_JSON_SCHEMA_PROMPT,
    LANGCHAIN_RESPONSIBILITY_AGENT_SYSTEM_PROMPT,
)
from ci_owner_agent.config import Settings, validate_model_settings
from ci_owner_agent.schemas import CiResponsibilityNotice
from ci_owner_agent.services.llm_client import build_chat_model
from ci_owner_agent.services.metrics import TokenUsageCallbackHandler, current_metrics_recorder, llm_invoke_with_metrics
from ci_owner_agent.services.scorer import downgrade_to_no_high_confidence, validate_notice


class LangChainResponsibilityAgent:
    def __init__(self, settings: Settings, context: AgentRuntimeContext, tools: list[Any]) -> None:
        self.settings = settings
        self.context = context
        self.tools = tools

    def analyze(self) -> CiResponsibilityNotice:
        config_error = validate_model_settings(self.settings)
        if config_error:
            return self._failure_notice(f"LLM 配置错误：{config_error}")
        self._configure_langsmith()
        try:
            raw = self._invoke_agent()
            notice = self._parse_notice(raw)
            if notice is None:
                repaired = self._repair_output(raw)
                notice = self._parse_notice(repaired)
            if notice is None:
                return self._failure_notice("LLM 输出无法解析为 CiResponsibilityNotice，已降级为无高可信责任人。")
            return validate_notice(notice)
        except Exception as exc:
            return self._failure_notice(f"LLM 分析失败或超时，已降级为无高可信责任人：{exc}")

    def _configure_langsmith(self) -> None:
        if self.settings.langsmith_tracing and self.settings.langsmith_api_key:
            os.environ["LANGSMITH_TRACING"] = "true"
            os.environ["LANGSMITH_API_KEY"] = self.settings.langsmith_api_key
            os.environ["LANGSMITH_PROJECT"] = self.settings.langsmith_project
            os.environ["LANGSMITH_ENDPOINT"] = self.settings.langsmith_endpoint

    def _model(self):
        return build_chat_model(self.settings)

    def _invoke_agent(self) -> str:
        agent = self._create_v1_agent(self._model())
        config = {
            "metadata": self._metadata(),
            "run_name": "ci-owner-agent-langchain-v1",
            "recursion_limit": self.settings.agent_recursion_limit,
        }
        recorder = current_metrics_recorder()
        if recorder is not None and recorder.enabled:
            config["callbacks"] = [TokenUsageCallbackHandler(recorder)]
        result = agent.invoke(
            {"messages": [{"role": "user", "content": self._initial_input()}]},
            config=config,
        )
        structured = result.get("structured_response") if isinstance(result, dict) else None
        if structured is not None:
            if isinstance(structured, CiResponsibilityNotice):
                return structured.model_dump_json()
            if isinstance(structured, dict):
                return json.dumps(structured, ensure_ascii=False, default=str)
            return str(structured)

        messages = result.get("messages") if isinstance(result, dict) else None
        if messages:
            last = messages[-1]
            content = getattr(last, "content", None)
            if content is not None:
                return str(content)

        return json.dumps(result, ensure_ascii=False, default=str)

    def _create_v1_agent(self, model):
        try:
            from langchain.agents import create_agent
            from langchain.agents.structured_output import ToolStrategy
        except Exception as exc:
            raise RuntimeError(
                "LangChain v1 create_agent dependencies are not installed or incompatible. "
                "Please install langchain>=1.3,<2 and langchain-openai compatible with LangChain v1. "
                f"Original error: {exc}"
            ) from exc
        kwargs = {
            "model": model,
            "tools": self.tools,
            "system_prompt": LANGCHAIN_RESPONSIBILITY_AGENT_SYSTEM_PROMPT,
        }
        if self.settings.response_format == "tool":
            kwargs["response_format"] = ToolStrategy(
                schema=CiResponsibilityNotice,
                handle_errors=(
                    "请输出严格合法的 CiResponsibilityNotice。"
                    "不得新增 schema 之外的字段。"
                    "如果证据不足，必须输出 no_high_confidence_owner。"
                ),
            )
        return create_agent(**kwargs)

    def _repair_output(self, raw: str) -> str:
        try:
            model = self._model()
            prompt = (
                "请把下面模型输出修复为严格合法的 CiResponsibilityNotice JSON。\n"
                "不得新增 schema 之外的字段。\n"
                "不要输出 Markdown，不要解释，只输出 JSON object。\n"
                "如果原输出证据不足或无法判断，必须输出 no_high_confidence_owner。\n\n"
                f"{CI_RESPONSIBILITY_NOTICE_JSON_SCHEMA_PROMPT}\n\n"
                f"原始输出:\n{raw}"
            )
            response = llm_invoke_with_metrics(model, prompt)
            return str(getattr(response, "content", response))
        except Exception:
            return ""

    def _initial_input(self) -> str:
        failure_summaries = self.context.failure_summaries
        if failure_summaries is None:
            try:
                failure_summaries = self.context.log_provider.find_test_failure_summaries(
                    tail_lines=self.settings.failure_chunk_tail_lines,
                    max_chunks=5,
                )
            except Exception as exc:
                failure_summaries = {"chunks": [], "warning": f"failure summary extraction failed: {exc}"}
        log_tail = self.context.build_info.logTail
        initial_scope = "focus" if self.context.investigation_scope and self.context.investigation_scope.has_focus_range else "full"
        payload = {
            "job": self.context.job,
            "buildNumber": self.context.build_number,
            "buildUrl": self.context.build_url,
            "result": self.context.result,
            "branch": self.context.branch,
            "baseCommit": self.context.base_commit,
            "headCommit": self.context.head_commit,
            "lastSuccessfulBuildNumber": self.context.last_successful_build_number,
            "investigationScope": self._compact_investigation_scope(),
            "initialDiffScope": initial_scope,
            "changedFilesScope": initial_scope,
            "changedFiles": [item.model_dump() for item in self.context.changed_files[:30]],
            "changedFilesTotal": len(self.context.changed_files),
            "changedFilesTruncated": len(self.context.changed_files) > 30,
            "commitsScope": initial_scope,
            "commits": [item.model_dump() for item in self.context.commits[:20]],
            "commitsTotal": len(self.context.commits),
            "commitsTruncated": len(self.context.commits) > 20,
            "failureSummaries": self._compact_failure_summaries(failure_summaries),
            "failureSummaryWarning": failure_summaries.get("warning") if isinstance(failure_summaries, dict) else None,
            "failureFacts": self._compact_failure_facts(self.context.failure_facts),
            "logTailMeta": self._log_tail_meta(log_tail),
            "historyPrecheck": self._compact_history_precheck(self.context.history_precheck),
            "aiHistoryPrecheck": self._compact_ai_history_precheck(self.context.ai_history_precheck),
            "instruction": (
                "必须基于工具证据。证据不足输出 no_high_confidence_owner。最终只输出 JSON。"
                "如需更多 changed files 或 commits，请调用 repo_get_diff_files / repo_get_commits_between。"
                "当前 Agent 正常只处理 FAILURE / UNSTABLE / UNKNOWN；SUCCESS / ABORTED 已由 orchestrator 处理。"
                "investigationScope 描述调查范围：fullRange 是 lastSuccessfulBuild -> currentBuild，focusRange 是 previousBuild -> currentBuild。"
                "当前初始 changedFiles/commits 来自 initialDiffScope，首次出现 failure 必须优先基于 focusRange 分析。"
                "只有 focusRange 无法解释当前失败，才允许调用 repo_get_diff_files(scope=\"full\") / repo_get_commits_between(scope=\"full\") / repo_get_file_diff(scope=\"full\") 扩大到 fullRange。"
                "如果调用 fullRange，必须在 reason/evidence 中说明 focusRange 证据不足的原因；不要一开始就全量分析 fullRange。"
                "如果 focusRange 和 fullRange 都没有直接证据，输出 no_high_confidence_owner。"
                "仅凭 fullRange 中某个可疑提交，不能 high_confidence 定责，除非能和失败日志直接关联。"
                "failureSummaries 是当前构建最重要的失败摘要；如果存在，优先基于它判断失败测试名、错误类型、测试文件和栈。"
                "不要默认读取 log tail；只有 failureSummaries/failureFacts 不足、需要原文证据、或需要验证关键词/文件路径/测试名时，才调用日志工具。"
                "failureFacts 是 AI 从非结构化日志中提取的内层失败事实；如果 failureSummaries 为空但 failureFacts 非空，"
                "优先基于 failureFacts + log/diff 工具判断当前责任。Docker/Jenkins/BuildKit/shell wrapper 不能单独作为责任依据。"
                "failureFacts 来自构建日志中的内层失败事实；如果基于 failureFacts 输出 evidence，对应 evidence.type 应优先使用 \"log\"，"
                "不要使用 \"build_info\"；build_info 只用于 metadata / Jenkins build metadata，不用于具体错误证据。"
                "high_confidence current_build_owner 必须至少包含一个 log evidence 和一个 diff / keyword_match / ts_symbol evidence。"
                "如果 failureFacts[*].historyEligible=false 或 isGenericWrapper=true，不得基于它输出 inherited_failure_owner。"
                "如果 responsibilityItems 对应某个 failureFact，failureSignature 优先使用 failureFact.signatureKey，便于后续历史事实比对。"
                "当前版本 AI failureFacts 只辅助当前 build 定责，不代表历史继承结论。"
                "aiHistoryPrecheck 是 AI failure facts 历史语义比对结果。"
                "只有 aiHistoryPrecheck.currentFacts[*].inheritedOwner.found=true，且 matchType=\"ai_fact_semantic\"，"
                "且 relationship=\"same_root_cause\"，才允许基于 AI facts 输出 inherited_failure_owner。"
                "输出 inherited_failure_owner 时，responsibilityType=\"inherited_failure_owner\"，owner.type=\"inherited_failure_owner\"，"
                "owner.name/email/commit 使用 inheritedOwner 中的 ownerName/ownerEmail/ownerCommit，owner.confidence 使用 inheritedOwner.confidence，"
                "sourceBuildNumber/sourceBuildUrl 使用 inheritedOwner.sourceBuildNumber/sourceBuildUrl，matchType=\"ai_fact_semantic\"，"
                "relationship=\"same_root_cause\"，failureSignature 使用当前 currentFact.signatureKey。"
                "如果 aiHistoryPrecheck 没有 inheritedOwner.found=true，不得因为 historical facts 存在就继承。"
                "如果 currentFact.blockedReason 存在，不得继承；如果 feedbackOverride 是 mark_flaky 或 mark_no_owner，不得继承。"
                "Docker/Jenkins/BuildKit/shell wrapper 永远不能作为历史继承依据。"
                "deterministic historyPrecheck 和 aiHistoryPrecheck 都存在时，优先使用 deterministic historyPrecheck；"
                "AI history 只用于非 Mocha/Japa failure facts。"
                "如需更多日志，再调用 log_read_range / log_search / log_find_error_chunks / log_read_tail。"
                "不要仅凭 changedFiles 或 package.json 依赖升级输出 high_confidence。"
            ),
        }
        return json.dumps(payload, ensure_ascii=False)

    def _compact_investigation_scope(self) -> dict[str, Any] | None:
        scope = self.context.investigation_scope
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

    def _compact_failure_summaries(self, summaries: dict[str, Any] | None) -> list[dict[str, Any]]:
        chunks = summaries.get("chunks", []) if isinstance(summaries, dict) else []
        compact: list[dict[str, Any]] = []
        for idx, chunk in enumerate(chunks[:5]):
            content = str(chunk.get("content") or "")
            compact.append(
                {
                    "chunkIndex": chunk.get("chunkIndex", idx),
                    "chunkSource": chunk.get("chunkSource"),
                    "startLine": chunk.get("startLine"),
                    "endLine": chunk.get("endLine"),
                    "signature": chunk.get("signature") or {},
                    "content": self._truncate_summary_content(content),
                }
            )
        return compact

    def _truncate_summary_content(self, content: str, limit: int = 4000) -> str:
        if len(content) <= limit:
            return content
        return content[:limit] + "\n...[truncated]"

    def _compact_failure_facts(self, facts_result: dict[str, Any] | None) -> dict[str, Any] | None:
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

    def _compact_ai_history_precheck(self, precheck: dict[str, Any] | None) -> dict[str, Any] | None:
        if not isinstance(precheck, dict):
            return None
        return {
            "ok": precheck.get("ok"),
            "mode": precheck.get("mode"),
            "threshold": precheck.get("threshold"),
            "warning": precheck.get("warning"),
            "error": precheck.get("error"),
            "diagnostics": self._compact_ai_history_diagnostics(precheck.get("diagnostics")),
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
                    "inheritedOwner": self._compact_inherited_owner(item.get("inheritedOwner")),
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
                    "feedbackOverride": self._compact_feedback_override(item.get("feedbackOverride")),
                }
                for item in (precheck.get("candidates") or [])[:5]
                if isinstance(item, dict)
            ],
        }

    def _compact_ai_history_diagnostics(self, diagnostics: Any) -> dict[str, Any] | None:
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

    def _log_tail_meta(self, log_tail: Any | None) -> dict[str, Any] | None:
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

    def _compact_history_precheck(self, precheck: dict[str, Any] | None) -> dict[str, Any] | None:
        if not isinstance(precheck, dict):
            return precheck
        compact: dict[str, Any] = {
            "ok": precheck.get("ok"),
            "historyEnabled": precheck.get("historyEnabled"),
            "currentBuild": precheck.get("currentBuild"),
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
                    "inheritedOwner": self._compact_inherited_owner(item.get("inheritedOwner")),
                }
                for item in (precheck.get("currentChunks") or [])[:5]
                if isinstance(item, dict)
            ],
            "candidates": [],
            "instruction": (
                "如果 candidates 或 currentChunks 中存在 signature_exact / signature_structural + very_likely_same_failure或 currentChunks[*].inheritedOwner.found=true，"
                "且历史 buildNumber 小于当前 build，则当前 failure item 属于历史持续失败。"
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
                    "ownerType": candidate.get("ownerType"),
                    "ownerName": candidate.get("ownerName"),
                    "ownerCommit": candidate.get("ownerCommit"),
                    "hasHighConfidenceOwner": candidate.get("hasHighConfidenceOwner"),
                    "failureReason": candidate.get("failureReason"),
                    "signature": candidate.get("signature"),
                    "historicalSignature": candidate.get("historicalSignature"),
                    "historicalSignatureHash": candidate.get("historicalSignatureHash"),
                    "inheritedOwner": self._compact_inherited_owner(candidate.get("inheritedOwner")),
                }
            )
        return compact

    def _compact_inherited_owner(self, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            return {"found": False}
        if not value.get("found"):
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

    def _compact_feedback_override(self, value: Any) -> dict[str, Any] | None:
        if not isinstance(value, dict):
            return None
        return {
            "action": value.get("action"),
            "reviewer": value.get("reviewer"),
            "note": value.get("note"),
            "correctedOwner": value.get("correctedOwner"),
        }

    def _metadata(self) -> dict[str, Any]:
        return {
            "job": self.context.job,
            "buildNumber": self.context.build_number,
            "repo": self.context.repo,
            "branch": self.context.branch,
            "baseCommit": self.context.base_commit,
            "headCommit": self.context.head_commit,
            "lastSuccessfulBuildNumber": self.context.last_successful_build_number,
            "result": self.context.result,
        }

    def _parse_notice(self, raw: str) -> CiResponsibilityNotice | None:
        for text in self._notice_candidates(raw):
            notice = self._parse_notice_candidate(text)
            if notice is not None:
                return notice
        return None

    def _parse_notice_candidate(self, text: str) -> CiResponsibilityNotice | None:
        try:
            return CiResponsibilityNotice.model_validate_json(text)
        except Exception:
            try:
                return CiResponsibilityNotice.model_validate(json.loads(text))
            except Exception:
                return None

    def _notice_candidates(self, raw: str) -> list[str]:
        candidates = [raw.strip()]
        stripped = self._strip_code_fence(raw)
        if stripped not in candidates:
            candidates.append(stripped)
        for match in re.finditer(r"```(?:json)?\s*(.*?)\s*```", raw, flags=re.S | re.I):
            fenced = match.group(1).strip()
            if fenced and fenced not in candidates:
                candidates.append(fenced)
        json_object = self._extract_first_json_object(raw)
        if json_object and json_object not in candidates:
            candidates.append(json_object)
        return candidates

    def _extract_first_json_object(self, raw: str) -> str | None:
        decoder = json.JSONDecoder()
        for idx, char in enumerate(raw):
            if char != "{":
                continue
            try:
                _obj, end = decoder.raw_decode(raw[idx:])
            except json.JSONDecodeError:
                continue
            return raw[idx : idx + end]
        return None

    def _strip_code_fence(self, raw: str) -> str:
        text = raw.strip()
        match = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, flags=re.S | re.I)
        return match.group(1).strip() if match else text

    def _failure_notice(self, reason: str) -> CiResponsibilityNotice:
        base = CiResponsibilityNotice(
            job=self.context.job,
            buildNumber=self.context.build_number,
            buildUrl=self.context.build_url,
            result=self.context.result,
            branch=self.context.branch,
            headCommit=self.context.head_commit,
            baseCommit=self.context.base_commit,
            owner={
                "type": "no_high_confidence_owner",
                "name": "无高可信责任人",
                "email": None,
                "commit": None,
                "confidence": 0,
            },
            failureReason=reason,
            evidence=[],
            suggestions=[
                "人工查看 Jenkins 日志和本次 diff。",
                "检查 Agent prompt、模型输出格式和 LangSmith trace。",
                "检查 CI_AGENT_MODEL_TIMEOUT_SECONDS、模型服务响应、LangSmith trace 中最后一个 model run。",
            ],
            hasHighConfidenceOwner=False,
        )
        return downgrade_to_no_high_confidence(base, reason)
