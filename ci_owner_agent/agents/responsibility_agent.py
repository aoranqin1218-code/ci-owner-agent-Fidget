from __future__ import annotations

from dataclasses import dataclass

from ci_owner_agent.schemas import BuildInfo, ChangedFile, CiResponsibilityNotice, CommitInfo
from ci_owner_agent.services.log_provider import LogProvider
from ci_owner_agent.services.scorer import no_owner, validate_notice


@dataclass(frozen=True)
class AgentContext:
    repo: str
    build_info: BuildInfo
    base_commit: str
    head_commit: str
    commits: list[CommitInfo]
    changed_files: list[ChangedFile]
    log_provider: LogProvider


class FakeResponsibilityAgent:
    def analyze(self, context: AgentContext) -> CiResponsibilityNotice:
        return validate_notice(
            CiResponsibilityNotice(
                job=context.build_info.job,
                buildNumber=context.build_info.buildNumber,
                buildUrl=context.build_info.buildUrl,
                result=context.build_info.result,
                branch=context.build_info.branch,
                headCommit=context.head_commit,
                baseCommit=context.base_commit,
                owner=no_owner(),
                failureReason="fake provider 仅用于测试工具链，不执行正式定责。",
                evidence=[],
                suggestions=["使用真实 LLM provider 运行正式分析。"],
                hasHighConfidenceOwner=False,
            )
        )
