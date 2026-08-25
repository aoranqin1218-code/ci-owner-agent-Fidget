from __future__ import annotations

from contextlib import nullcontext
from dataclasses import replace
from typing import ContextManager

from ci_owner_agent.agents.context import AgentRuntimeContext
from ci_owner_agent.agents.factory import (
    AgentConfigurationError,
    create_responsibility_agent,
)
from ci_owner_agent.agents.langchain_agent import LangChainResponsibilityAgent
from ci_owner_agent.agents.responsibility_agent import AgentContext
from ci_owner_agent.config import Settings, load_settings
from ci_owner_agent.schemas import (
    BuildInfo,
    ChangedFile,
    CiResponsibilityNotice,
    CommitInfo,
    EvidenceItem,
    FailureFact,
)
from ci_owner_agent.services.ai_history_search import (
    history_search_similar_failure_facts,
)
from ci_owner_agent.services.branch_normalization import normalize_branch_name
from ci_owner_agent.services.coverage_responsibility import reconcile_coverage_responsibilities
from ci_owner_agent.services.failure_fact_ai import extract_failure_facts_with_ai
from ci_owner_agent.services.git_client import GitClient
from ci_owner_agent.services.history_no_owner import (
    build_no_owner_notice_from_history_decision,
    has_new_strong_evidence,
    select_build_level_no_owner_decision,
    validate_trusted_history_no_owner_decisions,
)
from ci_owner_agent.services.history_search import history_search_similar_failures
from ci_owner_agent.services.history_store import MongoHistoryStore, get_history_store
from ci_owner_agent.services.investigation_scope import InvestigationScope
from ci_owner_agent.services.jenkins_client import JenkinsClient
from ci_owner_agent.services.log_provider import (
    JenkinsLogProvider,
    LocalFileLogProvider,
    LogProvider,
)
from ci_owner_agent.services.log_parsing import resolve_checkout_revision_from_console_log
from ci_owner_agent.services.metrics import current_metrics_recorder
from ci_owner_agent.services.responsibility_path_enricher import (
    enrich_responsibility_item_paths,
)
from ci_owner_agent.services.responsibility_signature_enricher import (
    enrich_responsibility_item_signatures,
)
from ci_owner_agent.services.scorer import no_owner, validate_notice


def _metrics_stage(name: str) -> ContextManager[None]:
    recorder = current_metrics_recorder()
    return recorder.stage(name) if recorder is not None else nullcontext()


def _is_coverage_only_failures(failure_summaries: dict | None) -> bool:
    """failure_summaries 是否仅含 coverage 失败块（且至少有一个 coverage 块）。

    coverage-only 分支判断：有 coverage chunk、且没有任何 Japa 等非 coverage chunk 时为 True。
    空摘要（无任何 chunk）不算 coverage-only，走原 agent 流程。
    """
    if not isinstance(failure_summaries, dict):
        return False
    chunks = failure_summaries.get("chunks") or []
    if not chunks:
        return False
    has_coverage = any(
        isinstance(c, dict) and c.get("anchorType") == "coverage_failure_block" for c in chunks
    )
    has_other = any(
        isinstance(c, dict) and c.get("anchorType") != "coverage_failure_block" for c in chunks
    )
    return has_coverage and not has_other


def _restore_authoritative_build_metadata(
    notice: CiResponsibilityNotice,
    *,
    repo: str,
    build_info: BuildInfo,
    base_commit: str | None,
    head_commit: str | None,
) -> CiResponsibilityNotice:
    original_notice_head_commit = notice.headCommit
    authoritative_head_commit = head_commit or build_info.commit
    authoritative_metadata = {
        "repo": repo,
        "job": build_info.job,
        "buildNumber": build_info.buildNumber,
        "buildUrl": build_info.buildUrl,
        "result": build_info.result,
        "branch": build_info.branch,
        "baseCommit": base_commit,
        "headCommit": authoritative_head_commit,
    }
    restored_fields = [
        field_name
        for field_name, authoritative_value in authoritative_metadata.items()
        if getattr(notice, field_name) != authoritative_value
    ]
    if restored_fields:
        recorder = current_metrics_recorder()
        if recorder is not None:
            recorder.warnings.append(f"restored authoritative notice metadata: {','.join(restored_fields)}")

    payload = notice.model_dump(mode="python")
    payload.update(authoritative_metadata)
    for item in payload.get("responsibilityItems", []):
        if item.get("responsibilityType") != "current_build_owner":
            continue
        item["sourceBuildNumber"] = build_info.buildNumber
        item["sourceBuildUrl"] = build_info.buildUrl
        source_commit = item.get("sourceCommit")
        owner_commit = (item.get("owner") or {}).get("commit")
        source_commit_was_derived_from_wrong_head = (
            original_notice_head_commit != authoritative_head_commit
            and source_commit == original_notice_head_commit
        )
        if not source_commit or source_commit_was_derived_from_wrong_head:
            item["sourceCommit"] = owner_commit or authoritative_head_commit
    return CiResponsibilityNotice.model_validate(payload)


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
    commits_result, diff_result = _read_investigation_range(
        git_client=git_client,
        repo=repo,
        build_info=build_info,
        scope=investigation_scope,
        full_base_commit=base_commit,
        full_head_commit=head_commit,
    )
    if not commits_result.get("ok"):
        return failure_without_context(build_info, base_commit, f"Git commit 区间读取失败：{commits_result.get('error')}", repo=repo)
    if not diff_result.get("ok"):
        return failure_without_context(build_info, base_commit, f"Git diff 文件列表读取失败：{diff_result.get('error')}", repo=repo)
    context, runtime_context, changed_files = _build_analysis_contexts(
        repo=repo,
        build_info=build_info,
        base_commit=base_commit,
        head_commit=head_commit,
        log_provider=log_provider,
        git_client=git_client,
        settings=settings,
        last_successful_build_number=last_successful_build_number,
        investigation_scope=investigation_scope,
        commits_result=commits_result,
        diff_result=diff_result,
    )
    runtime_context = _with_precomputed_failure_context(runtime_context, history_store=history_store)
    with _metrics_stage("historyNoOwnerDecision"):
        no_owner_decision = select_build_level_no_owner_decision(
            runtime_context.history_precheck,
            runtime_context.ai_history_precheck,
        )
        trusted_no_owner_decision = validate_trusted_history_no_owner_decisions(
            no_owner_decision,
            current_build_number=build_info.buildNumber,
        )
        should_short_circuit_no_owner = bool(
            settings.history_inherit_no_owner_enabled
            and trusted_no_owner_decision
            and not has_new_strong_evidence(
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
        notice = build_no_owner_notice_from_history_decision(
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
        # 防御性接线：即使历史 no-owner 规则允许短路，当前构建里的 coverage
        # 事实仍必须经过唯一 reconciler，不能直接沿用旧 notice 后返回。
        notice = reconcile_coverage_responsibilities(
            notice,
            failure_summaries=runtime_context.failure_summaries,
            repo=repo,
            head_commit=head_commit,
            git_client=runtime_context.git_client,
            investigation_scope=runtime_context.investigation_scope,
        )
        # 历史 no-owner item 会保留来源构建用于审计；再次 model_validate 会按公共
        # schema 的普通 no-owner 归一规则清空这些历史字段，因此此分支不重复校验。
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
    # Fidget 二期（方案 X）：仅存在覆盖率门槛失败（无 Japa 测试失败）时，跳过责任分析
    # Agent，直接走确定性 coverage reconciler 生成 notice。
    if _is_coverage_only_failures(runtime_context.failure_summaries):
        # coverage-only：coverage 责任项完全由确定性 reconciler 生成，不调 LLM。
        notice = CiResponsibilityNotice(
            repo=repo,
            job=build_info.job,
            buildNumber=build_info.buildNumber,
            buildUrl=build_info.buildUrl,
            result=build_info.result,
            branch=build_info.branch,
            headCommit=head_commit,
            baseCommit=base_commit,
            owner=no_owner(),
            failureReason="构建失败：c8 覆盖率门槛失败（coverage-only，未发现 Japa 测试失败）。",
            evidence=[],
            suggestions=["查看 Coverage summary 中未达 100% 的指标与文件。"],
            hasHighConfidenceOwner=False,
        )
    else:
        try:
            with _metrics_stage("agentAnalyze"):
                agent = create_responsibility_agent(settings, runtime_context)
                if isinstance(agent, LangChainResponsibilityAgent):
                    notice = agent.analyze()
                else:
                    notice = agent.analyze(context)
        except AgentConfigurationError as exc:
            return failure_without_context(build_info, base_commit, f"LLM 配置错误：{exc}", repo=repo)
        notice = _restore_authoritative_build_metadata(
            notice,
            repo=repo,
            build_info=build_info,
            base_commit=base_commit,
            head_commit=head_commit,
        )
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
    # Fidget 二期：coverage 责任项由确定性 reconciler 单一写入（方案 X），
    # Agent 不生成 coverage 项。追加后由 validate 重算顶层 owner。
    notice = reconcile_coverage_responsibilities(
        notice,
        failure_summaries=runtime_context.failure_summaries,
        repo=repo,
        head_commit=head_commit,
        git_client=runtime_context.git_client,
        investigation_scope=runtime_context.investigation_scope,
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


def _read_investigation_range(
    *,
    git_client: GitClient,
    repo: str,
    build_info: BuildInfo,
    scope: InvestigationScope,
    full_base_commit: str,
    full_head_commit: str,
) -> tuple[dict, dict]:
    """Read the preferred focus range, then fall back to the trusted full range on failure."""
    scope_name = "focus" if scope.has_focus_range else "full"
    base_commit, head_commit = scope.range_for_scope(scope_name)
    commits_result, diff_result = _read_git_range(
        git_client=git_client,
        repo=repo,
        base_commit=base_commit,
        head_commit=head_commit,
        stage_name="gitDiffFocus" if scope_name == "focus" else "gitDiffFull",
    )
    if scope_name != "focus" or (commits_result.get("ok") and diff_result.get("ok")):
        return commits_result, diff_result
    message = (
        "focusRange Git diff 读取失败，已降级 fullRange："
        f"commits={commits_result.get('error')}; diff={diff_result.get('error')}"
    )
    build_info.warnings.append(message)
    return _read_git_range(
        git_client=git_client,
        repo=repo,
        base_commit=full_base_commit,
        head_commit=full_head_commit,
        stage_name="gitDiffFull",
    )


def _build_analysis_contexts(
    *,
    repo: str,
    build_info: BuildInfo,
    base_commit: str,
    head_commit: str,
    log_provider: LogProvider,
    git_client: GitClient,
    settings: Settings,
    last_successful_build_number: int | None,
    investigation_scope: InvestigationScope,
    commits_result: dict,
    diff_result: dict,
) -> tuple[AgentContext, AgentRuntimeContext, list[ChangedFile]]:
    """Build the legacy and runtime Agent contexts from an already validated Git range."""
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
    return context, runtime_context, changed_files


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
        build_result = jenkins_client.get_build_info(job, build, log_tail_lines)
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
        last_success_result = jenkins_client.get_last_successful_build_info(
            job,
            branch=build_info.branch,
            before_build_number=build_info.buildNumber,
            scan_limit=settings.jenkins_successful_build_scan_limit,
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
