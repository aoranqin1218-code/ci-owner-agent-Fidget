from __future__ import annotations

from contextlib import nullcontext
from dataclasses import replace
import json
import re
from typing import ContextManager

from ci_owner_agent.agents.context import AgentRuntimeContext
from ci_owner_agent.agents.factory import AgentConfigurationError, create_responsibility_agent
from ci_owner_agent.agents.langchain_agent import LangChainResponsibilityAgent
from ci_owner_agent.agents.responsibility_agent import AgentContext
from ci_owner_agent.config import Settings, load_settings
from ci_owner_agent.schemas import BuildInfo, ChangedFile, CiResponsibilityNotice, CommitInfo, EvidenceItem, FailureFact
from ci_owner_agent.services.failure_fact_ai import extract_failure_facts_with_ai
from ci_owner_agent.services.git_client import GitClient
from ci_owner_agent.services.history_inheritance import build_no_owner_item_from_decision
from ci_owner_agent.services.history_store import MongoHistoryStore, get_history_store
from ci_owner_agent.services.investigation_scope import InvestigationScope
from ci_owner_agent.services.jenkins_client import JenkinsClient
from ci_owner_agent.services.branch_normalization import normalize_branch_name
from ci_owner_agent.services.log_provider import JenkinsLogProvider, LocalFileLogProvider, LogProvider, resolve_checkout_revision_from_console_log
from ci_owner_agent.services.metrics import current_metrics_recorder
from ci_owner_agent.services.responsibility_path_enricher import enrich_responsibility_item_paths, normalize_repository_path
from ci_owner_agent.services.responsibility_signature_enricher import enrich_responsibility_item_signatures
from ci_owner_agent.services.scorer import no_owner, validate_notice
from ci_owner_agent.tools.ai_history_tools import history_search_similar_failure_facts
from ci_owner_agent.tools.history_tools import history_search_similar_failures
from ci_owner_agent.tools.jenkins_tools import jenkins_get_build_info, jenkins_get_last_successful_build_info


def _metrics_stage(name: str) -> ContextManager[None]:
    recorder = current_metrics_recorder()
    return recorder.stage(name) if recorder is not None else nullcontext()


def success_notice(build_info: BuildInfo, base_commit: str | None = None, repo: str | None = None) -> CiResponsibilityNotice:
    return CiResponsibilityNotice(
        repo=repo,
        job=build_info.job,
        buildNumber=build_info.buildNumber,
        buildUrl=build_info.buildUrl,
        result="SUCCESS",
        branch=build_info.branch,
        headCommit=build_info.commit,
        baseCommit=base_commit,
        owner=no_owner(),
        failureReason="构建成功，无需定责。",
        evidence=[
            EvidenceItem(
                id="E1",
                type="build_info",
                summary="构建结果为 SUCCESS",
                detail=f"build {build_info.buildNumber} succeeded",
                source="local" if build_info.buildUrl.startswith("local://") else "jenkins",
            )
        ],
        suggestions=[],
        hasHighConfidenceOwner=False,
    )


def aborted_notice(build_info: BuildInfo, base_commit: str | None = None, repo: str | None = None) -> CiResponsibilityNotice:
    return CiResponsibilityNotice(
        repo=repo,
        job=build_info.job,
        buildNumber=build_info.buildNumber,
        buildUrl=build_info.buildUrl,
        result="ABORTED",
        branch=build_info.branch,
        headCommit=build_info.commit,
        baseCommit=base_commit,
        owner=no_owner(),
        failureReason="构建被中止，更可能是 Jenkins Pipeline 执行上下文、环境问题或人工中断，不进入普通业务代码定责流程。",
        evidence=[
            EvidenceItem(
                id="E1",
                type="build_info",
                summary="构建结果为 ABORTED",
                detail="普通代码定责流程跳过",
                source="local" if build_info.buildUrl.startswith("local://") else "jenkins",
            )
        ],
        suggestions=[
            "优先检查 Jenkins Pipeline、post 阶段、节点上下文和执行环境。",
            "确认是否存在人工中止或 Jenkins agent 异常。",
            "如需定责 Pipeline 配置，请后续单独实现 Jenkinsfile / Pipeline 定责逻辑。",
        ],
        hasHighConfidenceOwner=False,
    )


def failure_without_context(
    build_info: BuildInfo,
    base_commit: str | None,
    reason: str,
    evidence: list[EvidenceItem] | None = None,
    repo: str | None = None,
) -> CiResponsibilityNotice:
    return CiResponsibilityNotice(
        repo=repo,
        job=build_info.job,
        buildNumber=build_info.buildNumber,
        buildUrl=build_info.buildUrl,
        result=build_info.result,
        branch=build_info.branch,
        headCommit=build_info.commit,
        baseCommit=base_commit,
        owner=no_owner(),
        failureReason=reason,
        evidence=evidence or [],
        suggestions=[
            "确认本地 repo 缓存目录、repo 名称、baseCommit 和 headCommit 是否正确。",
            "人工查看完整日志中首次失败位置。",
        ],
        hasHighConfidenceOwner=False,
    )


def analyze_failed_build(
    repo: str,
    build_info: BuildInfo,
    base_commit: str,
    head_commit: str,
    log_provider: LogProvider,
    git_client: GitClient,
    allow_sync_failure: bool = False,
    settings: Settings | None = None,
    last_successful_build_number: int | None = None,
    history_store: MongoHistoryStore | None = None,
    previous_build_number: int | None = None,
    previous_commit: str | None = None,
) -> CiResponsibilityNotice:
    settings = settings or load_settings()
    if history_store is None and settings.history_enabled:
        history_store = get_history_store(settings)
    with _metrics_stage("gitSync"):
        sync_result = git_client.sync(repo)
    sync_warning: EvidenceItem | None = None
    if not sync_result.get("ok"):
        message = f"repo_sync 失败：{sync_result.get('error') or sync_result.get('command', {}).get('error') or 'unknown error'}"
        if not allow_sync_failure:
            return failure_without_context(build_info, base_commit, f"{message}；正式模式中仓库同步失败会阻止高可信定责。", repo=repo)
        build_info.warnings.append(message)
        sync_warning = EvidenceItem(
            id="E_SYNC",
            type="build_info",
            summary="本地模式 repo_sync 失败，已降级继续分析",
            detail=message,
            source="repo_sync",
        )
    ancestry = git_client.check_ancestor(repo, base_commit, head_commit)
    if not ancestry.get("ok"):
        return failure_without_context(build_info, base_commit, f"Git ancestry 校验失败，不能输出高可信责任人：{ancestry.get('error')}", repo=repo)
    if not ancestry.get("isAncestor"):
        return failure_without_context(build_info, base_commit, "当前选择的上次成功提交不是本次构建提交的祖先，不能将该提交区间作为可靠的定责范围。", repo=repo)
    investigation_scope = _resolve_investigation_scope(
        repo=repo,
        build_info=build_info,
        base_commit=base_commit,
        head_commit=head_commit,
        history_store=history_store,
        previous_build_number=previous_build_number,
        previous_commit=previous_commit,
    )
    initial_scope_name = "focus" if investigation_scope.has_focus_range else "full"
    initial_base, initial_head = investigation_scope.range_for_scope(initial_scope_name)
    commits_result, diff_result = _read_git_range(
        git_client=git_client,
        repo=repo,
        base_commit=initial_base,
        head_commit=initial_head,
        stage_name="gitDiffFocus" if initial_scope_name == "focus" else "gitDiffFull",
    )
    if initial_scope_name == "focus" and (not commits_result.get("ok") or not diff_result.get("ok")):
        message = (
            "focusRange Git diff 读取失败，已降级 fullRange："
            f"commits={commits_result.get('error')}; diff={diff_result.get('error')}"
        )
        build_info.warnings.append(message)
        commits_result, diff_result = _read_git_range(
            git_client=git_client,
            repo=repo,
            base_commit=base_commit,
            head_commit=head_commit,
            stage_name="gitDiffFull",
        )
    if not commits_result.get("ok"):
        return failure_without_context(build_info, base_commit, f"Git commit 区间读取失败：{commits_result.get('error')}", repo=repo)
    if not diff_result.get("ok"):
        return failure_without_context(build_info, base_commit, f"Git diff 文件列表读取失败：{diff_result.get('error')}", repo=repo)
    commits = [CommitInfo.model_validate(item) for item in commits_result.get("commits", [])]
    changed_files = [ChangedFile.model_validate(item) for item in diff_result.get("files", [])]
    context = AgentContext(
        repo=repo,
        build_info=build_info,
        base_commit=base_commit,
        head_commit=head_commit,
        commits=commits,
        changed_files=changed_files,
        log_provider=log_provider,
    )
    runtime_context = AgentRuntimeContext(
        repo=repo,
        job=build_info.job,
        build_number=build_info.buildNumber,
        build_url=build_info.buildUrl,
        result=build_info.result,
        branch=build_info.branch,
        base_commit=base_commit,
        head_commit=head_commit,
        build_info=build_info,
        commits=commits,
        changed_files=changed_files,
        log_provider=log_provider,
        git_client=git_client,
        settings=settings,
        last_successful_build_number=last_successful_build_number,
        investigation_scope=investigation_scope,
    )
    runtime_context = _with_precomputed_failure_context(runtime_context, history_store=history_store)
    with _metrics_stage("historyNoOwnerDecision"):
        no_owner_decision = _select_build_level_no_owner_decision(
            runtime_context.history_precheck,
            runtime_context.ai_history_precheck,
        )
        trusted_no_owner_decision = _validate_trusted_history_no_owner_decisions(
            no_owner_decision,
            current_build_number=build_info.buildNumber,
        )
        should_short_circuit_no_owner = bool(
            settings.history_inherit_no_owner_enabled
            and trusted_no_owner_decision
            and not _has_new_strong_evidence(
                decision=trusted_no_owner_decision,
                failure_summaries=runtime_context.failure_summaries,
                failure_facts=runtime_context.failure_facts,
                changed_files=changed_files,
            )
        )
    if should_short_circuit_no_owner and trusted_no_owner_decision:
        recorder = current_metrics_recorder()
        if recorder is not None:
            recorder.warnings.append(
                f"short-circuited by historical no-owner decision from build #{trusted_no_owner_decision.get('sourceBuildNumber')}"
            )
        notice = _build_no_owner_notice_from_history_decision(
            build_info=build_info,
            base_commit=base_commit,
            head_commit=head_commit,
            decision=trusted_no_owner_decision,
            failure_summaries=runtime_context.failure_summaries,
            failure_facts=runtime_context.failure_facts,
        )
        notice = enrich_responsibility_item_signatures(
            notice,
            runtime_context.failure_summaries,
            runtime_context.failure_facts,
        )
        notice = enrich_responsibility_item_paths(
            notice,
            runtime_context.failure_summaries,
            runtime_context.failure_facts,
            repo=repo,
        )
        _save_history(
            settings,
            build_info,
            notice,
            log_provider,
            base_commit,
            head_commit,
            last_successful_build_number,
            runtime_context.failure_summaries,
            runtime_context.failure_facts,
            history_store=history_store,
        )
        return notice
    try:
        with _metrics_stage("agentAnalyze"):
            agent = create_responsibility_agent(settings, runtime_context)
            if isinstance(agent, LangChainResponsibilityAgent):
                notice = agent.analyze()
            else:
                notice = agent.analyze(context)
    except AgentConfigurationError as exc:
        return failure_without_context(build_info, base_commit, f"LLM 配置错误：{exc}", repo=repo)
    if sync_warning is not None:
        notice.evidence.append(sync_warning)
    notice = validate_notice(notice)
    notice = enrich_responsibility_item_signatures(
        notice,
        runtime_context.failure_summaries,
        runtime_context.failure_facts,
    )
    notice = enrich_responsibility_item_paths(
        notice,
        runtime_context.failure_summaries,
        runtime_context.failure_facts,
        repo=repo,
    )
    notice = validate_notice(notice)
    _save_history(
        settings,
        build_info,
        notice,
        log_provider,
        base_commit,
        head_commit,
        last_successful_build_number,
        runtime_context.failure_summaries,
        runtime_context.failure_facts,
        history_store=history_store,
    )
    return notice


def _read_git_range(
    *,
    git_client: GitClient,
    repo: str,
    base_commit: str | None,
    head_commit: str | None,
    stage_name: str,
) -> tuple[dict, dict]:
    with _metrics_stage("gitDiff"):
        with _metrics_stage(stage_name):
            commits_result = git_client.get_commits_between(repo, base_commit or "", head_commit or "")
            diff_result = git_client.get_diff_files(repo, base_commit or "", head_commit or "")
    return commits_result, diff_result


def _resolve_investigation_scope(
    *,
    repo: str,
    build_info: BuildInfo,
    base_commit: str | None,
    head_commit: str | None,
    history_store: MongoHistoryStore | None,
    previous_build_number: int | None = None,
    previous_commit: str | None = None,
) -> InvestigationScope:
    if previous_commit and previous_commit != head_commit:
        return InvestigationScope(
            mode="focus_then_full",
            full_base_commit=base_commit,
            full_head_commit=head_commit,
            focus_base_commit=previous_commit,
            focus_head_commit=head_commit,
            previous_build_number=previous_build_number,
            reason="previous commit provided explicitly",
        )
    if previous_commit and previous_commit == head_commit:
        return InvestigationScope(
            mode="full",
            full_base_commit=base_commit,
            full_head_commit=head_commit,
            previous_build_number=previous_build_number,
            reason="previous head equals current head; using full range",
        )
    if history_store is not None:
        try:
            previous_doc = history_store.find_previous_build(
                repo=repo,
                job=build_info.job,
                branch=build_info.branch,
                current_build_number=build_info.buildNumber,
            )
        except Exception as exc:
            return InvestigationScope(
                mode="full",
                full_base_commit=base_commit,
                full_head_commit=head_commit,
                reason=f"previous build lookup failed: {exc}",
            )
        if isinstance(previous_doc, dict):
            previous_head = previous_doc.get("headCommit")
            if previous_head and previous_head != head_commit:
                return InvestigationScope(
                    mode="focus_then_full",
                    full_base_commit=base_commit,
                    full_head_commit=head_commit,
                    focus_base_commit=previous_head,
                    focus_head_commit=head_commit,
                    previous_build_number=previous_doc.get("buildNumber"),
                    previous_build_url=previous_doc.get("buildUrl"),
                    previous_build_result=previous_doc.get("result"),
                    reason="previous build headCommit found in history store",
                )
            if previous_head and previous_head == head_commit:
                return InvestigationScope(
                    mode="full",
                    full_base_commit=base_commit,
                    full_head_commit=head_commit,
                    previous_build_number=previous_doc.get("buildNumber"),
                    previous_build_url=previous_doc.get("buildUrl"),
                    previous_build_result=previous_doc.get("result"),
                    reason="previous head equals current head; using full range",
                )
    return InvestigationScope(
        mode="full",
        full_base_commit=base_commit,
        full_head_commit=head_commit,
        reason="previous build headCommit unavailable; using full range",
    )


def _with_precomputed_failure_context(
    context: AgentRuntimeContext,
    history_store: MongoHistoryStore | None = None,
) -> AgentRuntimeContext:
    try:
        with _metrics_stage("failureSummary"):
            failure_summaries = context.log_provider.find_test_failure_summaries(
                tail_lines=context.settings.failure_chunk_tail_lines,
                max_chunks=5,
            )
    except Exception as exc:
        failure_summaries = {"chunks": [], "warning": f"failure summary extraction failed: {exc}"}
    enriched = replace(context, failure_summaries=failure_summaries)
    if not (failure_summaries.get("chunks") if isinstance(failure_summaries, dict) else None) and context.settings.ai_failure_facts_enabled:
        try:
            with _metrics_stage("failureFacts"):
                focused = context.log_provider.find_focused_failure_chunks(
                    tail_lines=context.settings.failure_chunk_tail_lines,
                    max_chunks=1,
                )
                chunks = focused.get("chunks", []) if isinstance(focused, dict) else []
                log_excerpt = str(chunks[0].get("content") or "") if chunks else ""
                if len(log_excerpt) > context.settings.ai_failure_fact_max_log_chars:
                    log_excerpt = log_excerpt[-context.settings.ai_failure_fact_max_log_chars :]
                facts_result = extract_failure_facts_with_ai(
                    settings=context.settings,
                    job=context.job,
                    build_number=context.build_number,
                    build_url=context.build_url,
                    branch=context.branch,
                    log_excerpt=log_excerpt,
                    changed_files=context.changed_files,
                    commits=context.commits,
                )
            enriched = replace(enriched, failure_facts=facts_result.model_dump(mode="json"))
        except Exception as exc:
            enriched = replace(enriched, failure_facts={"ok": False, "facts": [], "warning": f"AI failure facts extraction failed: {exc}"})
    try:
        with _metrics_stage("historyPrecheck"):
            history_precheck = history_search_similar_failures(
                enriched,
                maxCandidates=enriched.settings.history_max_candidates,
                store=history_store,
            )
    except Exception as exc:
        history_precheck = {
            "ok": False,
            "historyEnabled": enriched.settings.history_enabled,
            "error": str(exc),
            "stage": "orchestrator_history_precheck",
            "candidates": [],
        }
    enriched = replace(enriched, history_precheck=history_precheck)

    summaries_exist = bool(failure_summaries.get("chunks")) if isinstance(failure_summaries, dict) else False
    facts_exist = bool((enriched.failure_facts or {}).get("facts")) if isinstance(enriched.failure_facts, dict) else False
    if (
        not summaries_exist
        and facts_exist
        and enriched.settings.history_enabled
        and enriched.settings.ai_history_compare_enabled
    ):
        try:
            with _metrics_stage("aiHistoryPrecheck"):
                ai_history_precheck = history_search_similar_failure_facts(enriched, store=history_store)
        except Exception as exc:
            ai_history_precheck = {
                "ok": False,
                "historyEnabled": enriched.settings.history_enabled,
                "mode": "ai_failure_facts",
                "error": str(exc),
                "candidates": [],
                "currentFacts": [],
            }
        enriched = replace(enriched, ai_history_precheck=ai_history_precheck)
    return enriched


def _select_build_level_no_owner_decision(history_precheck: dict | None, ai_history_precheck: dict | None) -> dict | None:
    history_items = (history_precheck or {}).get("currentChunks") or []
    if history_items:
        return _select_all_failures_no_owner_decision(history_items, source="historyPrecheck")

    ai_items = (ai_history_precheck or {}).get("currentFacts") or []
    if ai_items:
        return _select_all_failures_no_owner_decision(ai_items, source="aiHistoryPrecheck")
    return None


def _select_all_failures_no_owner_decision(items: list, *, source: str) -> dict | None:
    decisions: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            return None
        inherited = item.get("inheritedOwner") if isinstance(item.get("inheritedOwner"), dict) else {}
        if inherited.get("found"):
            return None
        decision = item.get("noOwnerDecision") if isinstance(item.get("noOwnerDecision"), dict) else None
        if not decision or not decision.get("found"):
            return None
        decisions.append(
            {
                **decision,
                "source": source,
                "failureSignature": _current_item_signature(item, source),
                "failureTitle": _current_item_title(item, source),
                "failureSummary": _current_item_summary(item, source),
            }
        )
    if not decisions:
        return None
    ranked = sorted(
        decisions,
        key=lambda item: (
            1 if item.get("feedbackAction") in {"mark_flaky", "mark_no_owner"} else 0,
            -(item.get("sourceBuildNumber") or 0),
        ),
        reverse=True,
    )
    selected = dict(ranked[0])
    selected["allFailuresNoOwnerDecision"] = True
    selected["coveredFailureCount"] = len(decisions)
    selected["decisions"] = decisions
    selected["coveredSourceBuildNumbers"] = sorted(
        {number for number in (item.get("sourceBuildNumber") for item in decisions) if number is not None}
    )
    selected["coveredSignatures"] = [
        signature
        for signature in (_decision_signature(item) for item in decisions)
        if signature
    ]
    return selected


def _current_item_signature(item: dict, source: str) -> str | None:
    if source == "historyPrecheck":
        signature = item.get("signature") if isinstance(item.get("signature"), dict) else {}
        return signature.get("signatureKey") or signature.get("signatureHash") or item.get("normalizedHash")
    return item.get("signatureKey") or item.get("factId")


def _current_item_title(item: dict, source: str) -> str:
    if source == "historyPrecheck":
        signature = item.get("signature") if isinstance(item.get("signature"), dict) else {}
        return str(
            signature.get("testName")
            or signature.get("testCase")
            or signature.get("errorType")
            or _first_nonempty_line(item.get("preview"))
            or "historical same failure"
        )
    return " | ".join(
        str(value)
        for value in (item.get("failureKind"), item.get("errorCode"), item.get("symbol") or item.get("packageName"))
        if value
    ) or "historical same failure fact"


def _current_item_summary(item: dict, source: str) -> str | None:
    if source == "historyPrecheck":
        return str(item.get("preview") or "").strip() or None
    parts = [item.get("failureKind"), item.get("errorCode"), item.get("filePath"), item.get("symbol"), item.get("packageName")]
    text = " | ".join(str(value) for value in parts if value)
    return text or None


def _first_nonempty_line(value: object) -> str | None:
    for line in str(value or "").splitlines():
        if line.strip():
            return line.strip()
    return None


HISTORY_NO_OWNER_MATCH_TYPES = {"signature_exact", "signature_structural", "ai_fact_semantic"}


def _validate_trusted_history_no_owner_decisions(
    decision: dict | None,
    *,
    current_build_number: int,
) -> dict | None:
    from ci_owner_agent.services.failure_identity import canonicalize_failure_signature

    if not isinstance(decision, dict):
        return None
    raw_items = decision.get("decisions") if isinstance(decision.get("decisions"), list) else [decision]
    trusted_items: list[dict] = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            return None
        source_build = raw.get("sourceBuildNumber")
        failure_signature = canonicalize_failure_signature(raw.get("failureSignature"))
        match_type = str(raw.get("matchType") or "").strip()
        relationship = str(raw.get("relationship") or "").strip()
        reason = str(raw.get("reason") or "").strip()
        if not (
            type(source_build) is int
            and 0 < source_build < current_build_number
            and failure_signature
            and match_type in HISTORY_NO_OWNER_MATCH_TYPES
            and relationship
            and reason
        ):
            return None
        trusted_items.append(
            {
                **raw,
                "failureSignature": failure_signature,
                "sourceBuildNumber": source_build,
                "sourceBuildUrl": str(raw.get("sourceBuildUrl") or "").strip() or None,
                "matchType": match_type,
                "relationship": relationship,
                "reason": reason,
            }
        )
    if not trusted_items:
        return None
    return {
        **decision,
        **trusted_items[0],
        "decisions": trusted_items,
        "allFailuresNoOwnerDecision": len(trusted_items) > 1 or bool(decision.get("allFailuresNoOwnerDecision")),
        "coveredFailureCount": len(trusted_items),
    }


def _has_new_strong_evidence(
    *,
    decision: dict,
    failure_summaries: dict | None,
    failure_facts: dict | None,
    changed_files: list[ChangedFile],
) -> bool:
    current_items = _current_failure_evidence_items(failure_summaries, failure_facts)
    item_decisions = decision.get("decisions") if isinstance(decision.get("decisions"), list) else [decision]
    aligned = _align_current_failures_to_decisions(current_items, item_decisions)
    if aligned is None:
        return True

    changed_paths = {
        normalized
        for item in changed_files
        if (normalized := normalize_repository_path(item.path))
    }
    for current, historical in aligned:
        if current["paths"] & changed_paths:
            return True
        current_markers = _strong_failure_markers(current["payload"])
        historical_markers = _strong_failure_markers(historical, historical=True)
        if not current_markers.issubset(historical_markers):
            return True
    return False


def _current_failure_evidence_items(failure_summaries: dict | None, failure_facts: dict | None) -> list[dict]:
    summaries = _failure_summary_signatures(failure_summaries)
    if summaries:
        return [
            {
                "identifiers": _failure_identifiers(summary),
                "payload": summary,
                "paths": _paths_from_payload(summary),
            }
            for summary in summaries
        ]
    facts = (failure_facts or {}).get("facts") if isinstance(failure_facts, dict) else []
    return [
        {
            "identifiers": _failure_identifiers(fact),
            "payload": fact,
            "paths": _paths_from_payload(fact),
        }
        for fact in facts or []
        if isinstance(fact, dict)
    ]


def _align_current_failures_to_decisions(current_items: list[dict], decisions: list[dict]) -> list[tuple[dict, dict]] | None:
    if not current_items or len(current_items) != len(decisions):
        return None
    remaining = list(decisions)
    aligned: list[tuple[dict, dict]] = []
    for current in current_items:
        identifiers = current.get("identifiers") or set()
        matches = [item for item in remaining if identifiers & _failure_identifiers(item)]
        if len(matches) != 1:
            return None
        matched = matches[0]
        remaining.remove(matched)
        aligned.append((current, matched))
    return aligned if not remaining else None


def _failure_identifiers(value: dict) -> set[str]:
    signature = value.get("signature") if isinstance(value.get("signature"), dict) else {}
    candidates = (
        value.get("failureSignature"),
        value.get("signatureKey"),
        signature.get("signatureKey"),
        signature.get("signatureHash"),
        value.get("signatureHash"),
        value.get("normalizedHash"),
        value.get("factId"),
    )
    return {str(item).strip() for item in candidates if str(item or "").strip()}


def _paths_from_payload(value: dict) -> set[str]:
    signature = value.get("signature") if isinstance(value.get("signature"), dict) else {}
    result: set[str] = set()
    for raw_path in (
        value.get("testFile"),
        value.get("topStackFile"),
        value.get("filePath"),
        signature.get("testFile"),
        signature.get("topStackFile"),
        signature.get("filePath"),
    ):
        normalized = normalize_repository_path(raw_path)
        if normalized:
            result.add(normalized)
    return result


def _strong_failure_markers(value: dict, *, historical: bool = False) -> set[str]:
    signature = value.get("signature") if isinstance(value.get("signature"), dict) else {}
    explicit_values = (
        signature.get("errorCode"),
        signature.get("errorType"),
        value.get("errorCode"),
        value.get("errorType"),
        signature.get("failureKind"),
        value.get("failureKind"),
    )
    text = " ".join(
        str(part or "")
        for part in (
            None if historical else value.get("signatureKey"),
            None if historical else value.get("signatureHash"),
            None if historical else value.get("errorMessage"),
            signature.get("signatureKey"),
            signature.get("signatureHash"),
            signature.get("errorMessage"),
        )
    )
    markers = re.findall(
        r"\b(?:TS\d{4}|AssertionError|ReferenceError|TypeScriptCompileError|Timeout|MODULE_NOT_FOUND|ERR_[A-Z0-9_]+)\b",
        text,
        flags=re.IGNORECASE,
    )
    return {
        *(marker.lower() for marker in markers),
        *(str(marker).strip().lower() for marker in explicit_values if str(marker or "").strip()),
    }


def _build_no_owner_notice_from_history_decision(
    *,
    build_info: BuildInfo,
    base_commit: str,
    head_commit: str,
    decision: dict,
    failure_summaries: dict | None,
    failure_facts: dict | None,
) -> CiResponsibilityNotice:
    trusted = _validate_trusted_history_no_owner_decisions(
        decision,
        current_build_number=build_info.buildNumber,
    )
    if trusted is None:
        raise ValueError("invalid historical no-owner decision")
    decision = trusted
    source_build = decision.get("sourceBuildNumber")
    if decision.get("allFailuresNoOwnerDecision"):
        covered = decision.get("coveredFailureCount") or 1
        reason = (
            f"所有当前失败项（共 {covered} 个）均与历史构建 #{source_build} 等已分析为无高可信责任人的同类失败一致，"
            "且当前构建没有新的强证据改变结论，因此直接继承历史 no-owner 判定。"
        )
    else:
        reason = (
            f"该失败与历史构建 #{source_build} 已分析为无高可信责任人的失败一致，"
            "当前构建没有新的强证据改变结论，因此直接继承历史 no-owner 判定。"
        )
    item_decisions = decision.get("decisions") if isinstance(decision.get("decisions"), list) else []
    if not item_decisions:
        item_decisions = [
            {
                **decision,
                "failureTitle": decision.get("failureTitle") or _failure_title(failure_summaries, failure_facts),
                "failureSignature": (
                    _current_failure_signature(failure_summaries, failure_facts)
                    or decision.get("failureSignature")
                    or _decision_signature(decision)
                ),
                "failureSummary": decision.get("failureSummary") or _failure_title(failure_summaries, failure_facts),
            }
        ]
    summary_evidence = EvidenceItem(
        id="E_HISTORY_NO_OWNER_SUMMARY",
        type="reasoning",
        summary=f"当前共 {len(item_decisions)} 个失败均命中历史 no-owner",
        detail=(
            "failures="
            + json.dumps(
                [
                    {
                        "failureSignature": item.get("failureSignature"),
                        "sourceBuildNumber": item.get("sourceBuildNumber"),
                    }
                    for item in item_decisions
                ],
                ensure_ascii=False,
            )
        ),
        source="history_no_owner_decision",
    )
    item_evidence = [
        EvidenceItem(
            id=f"E_HISTORY_NO_OWNER_{index}",
            type="reasoning",
            summary="当前失败与历史无责任人失败一致",
            detail="; ".join(
                part
                for part in (
                    f"failureSignature={item['failureSignature']}",
                    f"sourceBuildNumber={item['sourceBuildNumber']}",
                    f"sourceBuildUrl={item['sourceBuildUrl']}" if item.get("sourceBuildUrl") else None,
                    f"matchType={item['matchType']}",
                    f"relationship={item['relationship']}",
                    f"feedbackAction={item['feedbackAction']}" if item.get("feedbackAction") else None,
                    f"reason={item['reason']}",
                    f"source={item['source']}" if item.get("source") else None,
                )
                if part
            ),
            source="history_no_owner_decision",
        )
        for index, item in enumerate(item_decisions, start=1)
    ]
    items = [
        build_no_owner_item_from_decision(
            item_decision,
            failure_title=str(item_decision.get("failureTitle") or "historical same failure"),
            failure_signature=item_decision.get("failureSignature"),
            failure_summary=item_decision.get("failureSummary"),
            evidence_id=item_evidence[index].id,
        )
        for index, item_decision in enumerate(item_decisions)
    ]
    notice = CiResponsibilityNotice(
        job=build_info.job,
        buildNumber=build_info.buildNumber,
        buildUrl=build_info.buildUrl,
        result=build_info.result,
        branch=build_info.branch,
        headCommit=head_commit,
        baseCommit=base_commit,
        owner=no_owner(),
        failureReason=reason,
        evidence=[summary_evidence, *item_evidence],
        suggestions=["如需强制重新分析，可设置 CI_AGENT_HISTORY_INHERIT_NO_OWNER_ENABLED=false。"],
        responsibilityItems=items,
        hasHighConfidenceOwner=False,
    )
    notice = validate_notice(notice)
    return apply_history_no_owner_sources(notice, item_decisions)


def apply_history_no_owner_sources(notice: CiResponsibilityNotice, trusted_decisions: list[dict]) -> CiResponsibilityNotice:
    """Restore history source fields using only orchestrator-validated decisions."""
    decisions_by_signature = {
        str(item.get("failureSignature")): item
        for item in trusted_decisions
        if item.get("failureSignature")
    }
    for item in notice.responsibilityItems:
        trusted = decisions_by_signature.get(str(item.failureSignature))
        if not trusted:
            continue
        source_build = trusted.get("sourceBuildNumber")
        match_type = trusted.get("matchType")
        if not (
            isinstance(source_build, int)
            and 0 < source_build < notice.buildNumber
            and match_type in HISTORY_NO_OWNER_MATCH_TYPES
            and trusted.get("relationship")
        ):
            continue
        item.sourceBuildNumber = source_build
        item.sourceBuildUrl = trusted.get("sourceBuildUrl")
        item.matchType = match_type
        item.relationship = trusted.get("relationship")
        item.reason = trusted.get("reason") or item.reason
    return notice


def _current_failure_signature(failure_summaries: dict | None, failure_facts: dict | None) -> str | None:
    summary = _first_failure_summary_signature(failure_summaries)
    if summary:
        return summary.get("signatureKey") or summary.get("signatureHash")
    facts = (failure_facts or {}).get("facts") if isinstance(failure_facts, dict) else []
    for fact in facts or []:
        if isinstance(fact, dict) and fact.get("signatureKey"):
            return fact.get("signatureKey")
    return None


def _decision_signature(decision: dict) -> str | None:
    signature = decision.get("signature") if isinstance(decision.get("signature"), dict) else {}
    return signature.get("signatureKey") or decision.get("signatureHash")


def _failure_summary_signatures(failure_summaries: dict | None) -> list[dict]:
    chunks = (failure_summaries or {}).get("chunks") if isinstance(failure_summaries, dict) else []
    result: list[dict] = []
    for chunk in chunks or []:
        if not isinstance(chunk, dict):
            continue
        signature = chunk.get("signature") if isinstance(chunk.get("signature"), dict) else {}
        if signature:
            result.append({**signature, "signatureHash": chunk.get("signatureHash")})
    return result


def _first_failure_summary_signature(failure_summaries: dict | None) -> dict | None:
    summaries = _failure_summary_signatures(failure_summaries)
    return summaries[0] if summaries else None


def _failure_paths(summary: dict | None, facts: list | None) -> set[str]:
    paths = {
        str((summary or {}).get("testFile") or ""),
        str((summary or {}).get("topStackFile") or ""),
    }
    for item in facts or []:
        if isinstance(item, dict):
            paths.add(str(item.get("filePath") or ""))
    return {path for path in paths if path}


def _failure_title(failure_summaries: dict | None, failure_facts: dict | None) -> str:
    summary = _first_failure_summary_signature(failure_summaries)
    if summary:
        return str(summary.get("testName") or summary.get("testCase") or summary.get("errorType") or "historical same failure")
    facts = (failure_facts or {}).get("facts") if isinstance(failure_facts, dict) else []
    for fact in facts or []:
        if isinstance(fact, dict):
            return str(fact.get("rootCauseSummary") or fact.get("message") or fact.get("failureKind") or "historical same failure")
    return "historical same failure"


def _save_history(
    settings: Settings,
    build_info: BuildInfo,
    notice: CiResponsibilityNotice,
    log_provider: LogProvider,
    base_commit: str | None,
    head_commit: str | None,
    last_successful_build_number: int | None,
    failure_summaries: dict | None = None,
    failure_facts: dict | None = None,
    history_store: MongoHistoryStore | None = None,
) -> None:
    with _metrics_stage("saveHistory"):
        store = history_store or get_history_store(settings)
        if store is None:
            return
        try:
            summaries = failure_summaries
            if summaries is None:
                summaries = log_provider.find_test_failure_summaries(
                    tail_lines=settings.failure_chunk_tail_lines,
                    max_chunks=5,
                )
            chunks = summaries.get("chunks", [])
            store.save_analysis(
                build_info=build_info,
                notice=notice,
                base_commit=base_commit,
                head_commit=head_commit,
                last_successful_build_number=last_successful_build_number,
                last_successful_commit=base_commit,
                error_chunks=chunks,
            )
            failure_facts_ok = isinstance(failure_facts, dict) and failure_facts.get("ok") is True
            if failure_facts_ok:
                facts = [
                    fact if isinstance(fact, FailureFact) else FailureFact.model_validate(fact)
                    for fact in ((failure_facts or {}).get("facts") or [])
                ]
                store.save_failure_facts(build_info=build_info, notice=notice, facts=facts)
            elif isinstance(failure_facts, dict) and failure_facts.get("ok") is False:
                warning = failure_facts.get("warning") or "AI failure facts extraction failed"
                build_info.warnings.append(f"failure facts not saved: {warning}")
        except Exception as exc:
            build_info.warnings.append(f"history save failed: {exc}")


def analyze_local(
    repo: str,
    job: str,
    build: int,
    branch: str | None,
    base_commit: str,
    head_commit: str,
    console_file: str,
    build_url: str,
    git_client: GitClient,
    log_tail_lines: int = 500,
    result: str | None = None,
    max_output_chars: int = 20000,
    settings: Settings | None = None,
    ignore_checkout_commit_mismatch: bool = False,
    last_successful_build_number: int | None = None,
    previous_build_number: int | None = None,
    previous_commit: str | None = None,
    build_timestamp: str | None = None,
) -> CiResponsibilityNotice:
    settings = settings or load_settings()
    log_provider = LocalFileLogProvider(console_file, max_output_chars=max_output_chars)
    checkout_resolution = resolve_checkout_revision_from_console_log(log_provider._content())
    if checkout_resolution.ambiguous and not ignore_checkout_commit_mismatch:
        raise ValueError(
            "Console log contains multiple conflicting checkout commits, so --head-commit cannot be verified. "
            "Refusing to execute Git analysis. Review the log or rerun with --ignore-checkout-commit-mismatch "
            "only when the supplied head commit is known to be correct."
        )
    actual_checkout_commit = checkout_resolution.commit
    if (
        actual_checkout_commit
        and actual_checkout_commit.lower() != head_commit.lower()
        and not ignore_checkout_commit_mismatch
    ):
        raise ValueError(
            f"console log checkout commit {actual_checkout_commit} does not match --head-commit {head_commit}.\n"
            f"The build actually tested {actual_checkout_commit}.\n"
            "Please rerun analyze-local with:\n"
            f"  --head-commit {actual_checkout_commit}"
        )
    log_tail = log_provider.read_tail(log_tail_lines)
    detected = log_provider.detect_final_status()
    final_result = (result or detected).upper()
    build_info = BuildInfo(
        job=job,
        buildNumber=build,
        result=final_result,
        buildUrl=build_url,
        branch=branch,
        commit=head_commit,
        timestamp=build_timestamp,
        durationMs=None,
        logTail=log_tail,
        warnings=[] if result else [f"result detected from log: {detected}"],
    )
    if final_result == "SUCCESS":
        notice = success_notice(build_info, base_commit=None, repo=repo)
        _save_history(settings, build_info, notice, log_provider, None, head_commit, last_successful_build_number)
        return notice
    if final_result == "ABORTED":
        notice = aborted_notice(build_info, base_commit=None, repo=repo)
        _save_history(settings, build_info, notice, log_provider, None, head_commit, last_successful_build_number)
        return notice
    return analyze_failed_build(
        repo,
        build_info,
        base_commit,
        head_commit,
        log_provider,
        git_client,
        allow_sync_failure=True,
        settings=settings,
        last_successful_build_number=last_successful_build_number,
        previous_build_number=previous_build_number,
        previous_commit=previous_commit,
    )


def analyze_jenkins(
    repo: str,
    job: str,
    build: int,
    jenkins_client: JenkinsClient,
    git_client: GitClient,
    log_tail_lines: int = 500,
    settings: Settings | None = None,
) -> CiResponsibilityNotice:
    settings = settings or load_settings()
    with _metrics_stage("jenkinsFetch"):
        build_result = jenkins_get_build_info(jenkins_client, job, build, log_tail_lines)
    if not build_result.get("ok"):
        build_info = BuildInfo(
            job=job,
            buildNumber=build,
            result="UNKNOWN",
            buildUrl=f"jenkins://{job}/{build}",
            branch=None,
            commit=None,
            logTail=None,
            warnings=[str(build_result.get("error"))],
        )
        return failure_without_context(build_info, None, f"Jenkins 构建信息获取失败：{build_result.get('error')}", repo=repo)

    build_info = BuildInfo.model_validate(build_result["buildInfo"])
    if build_info.result == "SUCCESS":
        notice = success_notice(build_info, base_commit=None, repo=repo)
        log_provider = JenkinsLogProvider(jenkins_client, job, build_info.buildNumber)
        _save_history(settings, build_info, notice, log_provider, None, build_info.commit, None)
        return notice
    if build_info.result == "ABORTED":
        notice = aborted_notice(build_info, base_commit=None, repo=repo)
        log_provider = JenkinsLogProvider(jenkins_client, job, build_info.buildNumber)
        _save_history(settings, build_info, notice, log_provider, None, build_info.commit, None)
        return notice

    if build_info.result not in {"FAILURE", "UNSTABLE", "UNKNOWN"}:
        return failure_without_context(build_info, None, f"不支持的 Jenkins 构建结果：{build_info.result}", repo=repo)
    if build_info.commit is None:
        return failure_without_context(
            build_info,
            None,
            "无法确认当前 Jenkins checkout SHA，因此不能可靠确定 baseCommit 或执行 Git diff。",
            repo=repo,
        )
    if normalize_branch_name(build_info.branch) is None:
        return failure_without_context(
            build_info,
            None,
            "当前构建分支无法确认，因此不能可靠确定 baseCommit 或执行 Git diff。",
            repo=repo,
        )

    with _metrics_stage("jenkinsFetch"):
        last_success_result = jenkins_get_last_successful_build_info(
            jenkins_client,
            job,
            branch=build_info.branch,
            beforeBuildNumber=build_info.buildNumber,
            scanLimit=settings.jenkins_successful_build_scan_limit,
        )
    if not last_success_result.get("ok"):
        return failure_without_context(
            build_info,
            None,
            f"无法获取上次成功构建，无法确定 baseCommit 做 Git diff：{last_success_result.get('error')}",
            evidence=[
                EvidenceItem(
                    id="E1",
                    type="build_info",
                    summary="Jenkins 当前构建信息已获取",
                    detail="上次成功构建信息获取失败",
                    source="jenkins",
                )
            ],
            repo=repo,
        )

    successful = last_success_result["successfulBuildInfo"]
    base_commit = successful.get("commit")
    head_commit = build_info.commit
    evidence = [
        EvidenceItem(
            id="E1",
            type="build_info",
            summary="Jenkins 构建失败，需要定责分析",
            detail=f"current result={build_info.result}; lastSuccessfulBuild={successful.get('buildNumber')}",
            source="jenkins",
        )
    ]
    if build_info.warnings:
        evidence.append(
            EvidenceItem(
                id="E2",
                type="build_info",
                summary="Jenkins 当前构建存在元数据 warning",
                detail="; ".join(build_info.warnings),
                source="jenkins",
            )
        )
    if successful.get("warnings"):
        evidence.append(
            EvidenceItem(
                id="E3",
                type="build_info",
                summary="Jenkins 上次成功构建存在元数据 warning",
                detail="; ".join(successful.get("warnings") or []),
                source="jenkins",
            )
        )
    if not head_commit or not base_commit:
        return failure_without_context(
            build_info,
            base_commit,
            "缺少 headCommit 或 baseCommit，无法执行 Git diff，因此不能输出高可信责任人。",
            evidence=evidence,
            repo=repo,
        )

    log_provider = JenkinsLogProvider(jenkins_client, job, build_info.buildNumber)
    return analyze_failed_build(
        repo,
        build_info,
        base_commit,
        head_commit,
        log_provider,
        git_client,
        allow_sync_failure=False,
        settings=settings,
        last_successful_build_number=successful.get("buildNumber"),
    )
