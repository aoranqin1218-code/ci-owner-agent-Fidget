from __future__ import annotations

from ci_owner_agent.agents.responsibility_agent import AgentContext, RuleBasedResponsibilityAgent
from ci_owner_agent.schemas import BuildInfo, ChangedFile, CiResponsibilityNotice, CommitInfo, EvidenceItem
from ci_owner_agent.services.git_client import GitClient
from ci_owner_agent.services.log_provider import LocalFileLogProvider, LogProvider
from ci_owner_agent.services.scorer import no_owner


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


def failure_without_context(build_info: BuildInfo, base_commit: str | None, reason: str) -> CiResponsibilityNotice:
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
        evidence=[],
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
) -> CiResponsibilityNotice:
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
    commits_result = git_client.get_commits_between(repo, base_commit, head_commit)
    if not commits_result.get("ok"):
        return failure_without_context(build_info, base_commit, f"Git commit 区间读取失败：{commits_result.get('error')}")
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
    notice = RuleBasedResponsibilityAgent(git_client).analyze(context)
    if sync_warning is not None:
        notice.evidence.append(sync_warning)
    return notice


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
) -> CiResponsibilityNotice:
    log_provider = LocalFileLogProvider(console_file, max_output_chars=max_output_chars)
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
        return success_notice(build_info, base_commit=None)
    if final_result == "ABORTED":
        return aborted_notice(build_info, base_commit=None)
    return analyze_failed_build(
        repo,
        build_info,
        base_commit,
        head_commit,
        log_provider,
        git_client,
        allow_sync_failure=True,
    )


def analyze_jenkins_placeholder(job: str, build: int, repo: str) -> CiResponsibilityNotice:
    build_info = BuildInfo(
        job=job,
        buildNumber=build,
        result="UNKNOWN",
        buildUrl=f"jenkins://{job}/{build}",
        branch=None,
        commit=None,
        logTail=None,
        warnings=["Jenkins mode is planned for phase 2", f"repo={repo}"],
    )
    return failure_without_context(build_info, None, "正式 Jenkins analyze 模式将在第二阶段接入；第一阶段仅保证 analyze-local 闭环。")
