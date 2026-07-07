from __future__ import annotations

import re
from dataclasses import dataclass

from ci_owner_agent.schemas import (
    BuildInfo,
    ChangedFile,
    CiResponsibilityNotice,
    CommitInfo,
    EvidenceItem,
    Owner,
)
from ci_owner_agent.services.git_client import GitClient
from ci_owner_agent.services.log_provider import LogProvider
from ci_owner_agent.services.scorer import no_owner, validate_notice
from ci_owner_agent.tools.keyword_tools import repo_keyword_search
from ci_owner_agent.tools.typescript_tools import ts_analyze_changed_functions


FILE_RE = re.compile(r"[\w./@-]+\.(?:ts|tsx|js|jsx|mjs|cjs|py|json|md)")
IDENT_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]{3,}\b")
STOP_WORDS = {
    "error",
    "failed",
    "failure",
    "expected",
    "received",
    "actual",
    "build",
    "finished",
    "exception",
    "timeout",
    "false",
    "true",
    "undefined",
    "null",
    "test",
    "tests",
    "line",
    "stack",
    "trace",
}


@dataclass(frozen=True)
class AgentContext:
    repo: str
    build_info: BuildInfo
    base_commit: str
    head_commit: str
    commits: list[CommitInfo]
    changed_files: list[ChangedFile]
    log_provider: LogProvider


class RuleBasedResponsibilityAgent:
    def __init__(self, git_client: GitClient) -> None:
        self.git_client = git_client

    def analyze(self, context: AgentContext) -> CiResponsibilityNotice:
        log_text = context.build_info.logTail.content if context.build_info.logTail else ""
        clues = self._extract_clues(log_text)
        candidate = self._find_candidate_from_changed_files(clues, context.changed_files, log_text)
        keyword_matches = []
        if candidate is None and clues["keywords"]:
            keyword_result = repo_keyword_search(
                self.git_client,
                repo=context.repo,
                commit=context.head_commit,
                keywords=clues["keywords"][:12],
                scope="changed_files",
                baseCommit=context.base_commit,
                headCommit=context.head_commit,
                maxMatches=20,
            )
            keyword_matches = keyword_result.get("matches", []) if keyword_result.get("ok") else []
            if keyword_matches:
                first_file = keyword_matches[0]["file"]
                candidate = next((item for item in context.changed_files if item.path == first_file), None)

        if candidate is None:
            ts_analyze_changed_functions(
                repo=context.repo,
                baseCommit=context.base_commit,
                headCommit=context.head_commit,
                files=[item.path for item in context.changed_files if item.path.endswith((".ts", ".tsx"))],
            )
            return self._no_confidence_notice(context)

        diff_result = self.git_client.get_file_diff(
            context.repo,
            context.base_commit,
            context.head_commit,
            candidate.path,
            context_lines=8,
        )
        diff_text = diff_result.get("diff", "") if diff_result.get("ok") else ""
        if not self._diff_supports_clues(diff_text, clues, candidate.path, log_text, bool(keyword_matches)):
            return self._no_confidence_notice(context)

        author = candidate.authors[0] if candidate.authors else None
        if author is None:
            return self._no_confidence_notice(context)

        commit_hash = author.commits[0] if author.commits else self._first_commit_for_file(context.commits, candidate.path)
        evidence = [
            EvidenceItem(
                id="E1",
                type="log",
                summary="日志中出现可定位的失败线索",
                detail=self._summarize_log_clues(clues),
                source=f"log:{context.build_info.logTail.startLine}-{context.build_info.logTail.endLine}"
                if context.build_info.logTail
                else "log",
            ),
            EvidenceItem(
                id="E2",
                type="diff",
                summary=f"{candidate.path} 在 base..head 区间被修改",
                detail=f"+{candidate.additions if candidate.additions is not None else '?'} -{candidate.deletions if candidate.deletions is not None else '?'}; author {author.name}",
                source=f"git diff {context.base_commit}..{context.head_commit} -- {candidate.path}",
            ),
        ]
        if keyword_matches:
            evidence.append(
                EvidenceItem(
                    id="E3",
                    type="keyword_match",
                    summary="日志关键词命中本次变更文件",
                    detail=f"{keyword_matches[0]['keyword']} matched {keyword_matches[0]['file']}:{keyword_matches[0]['line']}",
                    source="repo_keyword_search(scope=changed_files)",
                )
            )

        notice = CiResponsibilityNotice(
            job=context.build_info.job,
            buildNumber=context.build_info.buildNumber,
            buildUrl=context.build_info.buildUrl,
            result=context.build_info.result,
            branch=context.build_info.branch,
            headCommit=context.head_commit,
            baseCommit=context.base_commit,
            owner=Owner(
                type="high_confidence",
                name=author.name,
                email=author.email,
                commit=commit_hash,
                confidence=0.86,
            ),
            failureReason=f"构建失败，日志线索与本次修改的 {candidate.path} 能建立关联，该 diff 可解释当前失败现象。",
            evidence=evidence,
            suggestions=[
                f"优先检查 {candidate.path} 中本次变更相关逻辑。",
                "本地单独运行日志中对应的失败测试或命令。",
                "确认本次失败构建与 base/head commit 选择属于同一分支和参数集。",
            ],
            hasHighConfidenceOwner=True,
        )
        return validate_notice(notice)

    def _extract_clues(self, log_text: str) -> dict[str, list[str]]:
        files = list(dict.fromkeys(FILE_RE.findall(log_text)))
        identifiers = []
        for token in IDENT_RE.findall(log_text):
            lower = token.lower()
            if lower in STOP_WORDS:
                continue
            if len(token) > 80:
                continue
            if token not in identifiers:
                identifiers.append(token)
        keywords = files + identifiers
        high_signal = [token for token in keywords if token.isupper() or any(ch.isupper() for ch in token[1:]) or "/" in token or "." in token]
        return {"files": files[:20], "identifiers": identifiers[:50], "keywords": (high_signal or keywords)[:50]}

    def _find_candidate_from_changed_files(self, clues: dict[str, list[str]], changed_files: list[ChangedFile], log_text: str) -> ChangedFile | None:
        lower_log = log_text.lower()
        clue_files = {item.replace("\\", "/").lower() for item in clues["files"]}
        for changed in changed_files:
            path = changed.path.replace("\\", "/")
            basename = path.rsplit("/", 1)[-1].lower()
            if path.lower() in lower_log or basename in lower_log or path.lower() in clue_files:
                return changed
        return None

    def _diff_supports_clues(
        self,
        diff_text: str,
        clues: dict[str, list[str]],
        path: str,
        log_text: str,
        has_keyword_match: bool,
    ) -> bool:
        if has_keyword_match:
            return True
        lower_diff = diff_text.lower()
        lower_log = log_text.lower()
        basename = path.rsplit("/", 1)[-1].lower()
        if path.lower() in lower_log or basename in lower_log:
            return bool(diff_text.strip())
        for keyword in clues["keywords"][:20]:
            normalized = keyword.lower()
            if len(normalized) >= 4 and normalized in lower_diff:
                return True
        return False

    def _summarize_log_clues(self, clues: dict[str, list[str]]) -> str:
        parts = []
        if clues["files"]:
            parts.append("files=" + ", ".join(clues["files"][:5]))
        if clues["identifiers"]:
            parts.append("identifiers=" + ", ".join(clues["identifiers"][:8]))
        return "; ".join(parts) if parts else "日志尾部存在失败信息，但缺少可稳定定位的符号"

    def _first_commit_for_file(self, commits: list[CommitInfo], _path: str) -> str | None:
        return commits[0].hash if commits else None

    def _no_confidence_notice(self, context: AgentContext) -> CiResponsibilityNotice:
        return CiResponsibilityNotice(
            job=context.build_info.job,
            buildNumber=context.build_info.buildNumber,
            buildUrl=context.build_info.buildUrl,
            result=context.build_info.result,
            branch=context.build_info.branch,
            headCommit=context.head_commit,
            baseCommit=context.base_commit,
            owner=no_owner(),
            failureReason="构建失败，但当前日志线索未能和本次 diff 建立明确关联；关键词搜索和 TypeScript 调用分析也未找到足够证据。",
            evidence=[],
            suggestions=[
                "人工查看完整 Jenkins 日志中首次失败位置。",
                "确认本次失败构建和上次成功构建是否属于同一分支和同一参数集。",
                "本地复现失败测试以获取更明确的堆栈。",
            ],
            hasHighConfidenceOwner=False,
        )


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
