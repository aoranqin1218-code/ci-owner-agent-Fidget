from __future__ import annotations

from contextlib import nullcontext
from dataclasses import replace
from typing import ContextManager

from ci_owner_agent.agents.context import AgentRuntimeContext
from ci_owner_agent.agents.factory import AgentConfigurationError, create_responsibility_agent
from ci_owner_agent.agents.langchain_agent import LangChainResponsibilityAgent
from ci_owner_agent.agents.responsibility_agent import AgentContext
from ci_owner_agent.config import Settings, load_settings
from ci_owner_agent.schemas import BuildInfo, ChangedFile, CiResponsibilityNotice, CommitInfo, EvidenceItem, FailureFact
from ci_owner_agent.services.failure_fact_ai import extract_failure_facts_with_ai
from ci_owner_agent.services.git_client import GitClient
from ci_owner_agent.services.history_store import MongoHistoryStore, get_history_store
from ci_owner_agent.services.jenkins_client import JenkinsClient
from ci_owner_agent.services.log_provider import JenkinsLogProvider, LocalFileLogProvider, LogProvider, detect_checkout_revision_from_console_log
from ci_owner_agent.services.metrics import current_metrics_recorder
from ci_owner_agent.services.scorer import no_owner, validate_notice
from ci_owner_agent.tools.ai_history_tools import history_search_similar_failure_facts
from ci_owner_agent.tools.history_tools import history_search_similar_failures
from ci_owner_agent.tools.jenkins_tools import jenkins_get_build_info, jenkins_get_last_successful_build_info


def _metrics_stage(name: str) -> ContextManager[None]:
    recorder = current_metrics_recorder()
    return recorder.stage(name) if recorder is not None else nullcontext()


def success_notice(build_info: BuildInfo, base_commit: str | None = None) -> CiResponsibilityNotice:
    return CiResponsibilityNotice(
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


def aborted_notice(build_info: BuildInfo, base_commit: str | None = None) -> CiResponsibilityNotice:
    return CiResponsibilityNotice(
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
) -> CiResponsibilityNotice:
    return CiResponsibilityNotice(
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
            return failure_without_context(build_info, base_commit, f"{message}；正式模式中仓库同步失败会阻止高可信定责。")
        build_info.warnings.append(message)
        sync_warning = EvidenceItem(
            id="E_SYNC",
            type="build_info",
            summary="本地模式 repo_sync 失败，已降级继续分析",
            detail=message,
            source="repo_sync",
        )
    with _metrics_stage("gitDiff"):
        commits_result = git_client.get_commits_between(repo, base_commit, head_commit)
    if not commits_result.get("ok"):
        return failure_without_context(build_info, base_commit, f"Git commit 区间读取失败：{commits_result.get('error')}")
    with _metrics_stage("gitDiff"):
        diff_result = git_client.get_diff_files(repo, base_commit, head_commit)
    if not diff_result.get("ok"):
        return failure_without_context(build_info, base_commit, f"Git diff 文件列表读取失败：{diff_result.get('error')}")
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
    )
    runtime_context = _with_precomputed_failure_context(runtime_context, history_store=history_store)
    try:
        with _metrics_stage("agentAnalyze"):
            agent = create_responsibility_agent(settings, runtime_context)
            if isinstance(agent, LangChainResponsibilityAgent):
                notice = agent.analyze()
            else:
                notice = agent.analyze(context)
    except AgentConfigurationError as exc:
        return failure_without_context(build_info, base_commit, f"LLM 配置错误：{exc}")
    if sync_warning is not None:
        notice.evidence.append(sync_warning)
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
) -> CiResponsibilityNotice:
    settings = settings or load_settings()
    log_provider = LocalFileLogProvider(console_file, max_output_chars=max_output_chars)
    actual_checkout_commit = detect_checkout_revision_from_console_log(log_provider._content())
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
        timestamp=None,
        durationMs=None,
        logTail=log_tail,
        warnings=[] if result else [f"result detected from log: {detected}"],
    )
    if final_result == "SUCCESS":
        notice = success_notice(build_info, base_commit=None)
        _save_history(settings, build_info, notice, log_provider, None, head_commit, last_successful_build_number)
        return notice
    if final_result == "ABORTED":
        notice = aborted_notice(build_info, base_commit=None)
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
        return failure_without_context(build_info, None, f"Jenkins 构建信息获取失败：{build_result.get('error')}")

    build_info = BuildInfo.model_validate(build_result["buildInfo"])
    if build_info.result == "SUCCESS":
        notice = success_notice(build_info, base_commit=None)
        log_provider = JenkinsLogProvider(jenkins_client, job, build_info.buildNumber)
        _save_history(settings, build_info, notice, log_provider, None, build_info.commit, None)
        return notice
    if build_info.result == "ABORTED":
        notice = aborted_notice(build_info, base_commit=None)
        log_provider = JenkinsLogProvider(jenkins_client, job, build_info.buildNumber)
        _save_history(settings, build_info, notice, log_provider, None, build_info.commit, None)
        return notice

    if build_info.result not in {"FAILURE", "UNSTABLE", "UNKNOWN"}:
        return failure_without_context(build_info, None, f"不支持的 Jenkins 构建结果：{build_info.result}")

    with _metrics_stage("jenkinsFetch"):
        last_success_result = jenkins_get_last_successful_build_info(
            jenkins_client,
            job,
            branch=build_info.branch,
            beforeBuildNumber=build_info.buildNumber,
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
