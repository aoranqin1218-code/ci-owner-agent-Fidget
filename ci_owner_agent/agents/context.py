from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ci_owner_agent.config import Settings
from ci_owner_agent.schemas import BuildInfo, ChangedFile, CommitInfo
from ci_owner_agent.services.git_client import GitClient
from ci_owner_agent.services.investigation_scope import InvestigationScope
from ci_owner_agent.services.log_provider import LogProvider


@dataclass(frozen=True)
class AgentRuntimeContext:
    repo: str
    job: str
    build_number: int
    build_url: str
    result: str
    branch: str | None
    base_commit: str | None
    head_commit: str | None
    build_info: BuildInfo
    commits: list[CommitInfo]
    changed_files: list[ChangedFile]
    log_provider: LogProvider
    git_client: GitClient
    settings: Settings
    last_successful_build_number: int | None = None
    investigation_scope: InvestigationScope | None = None
    failure_summaries: dict[str, Any] | None = None
    failure_facts: dict[str, Any] | None = None
    history_precheck: dict[str, Any] | None = None
    ai_history_precheck: dict[str, Any] | None = None
