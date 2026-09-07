"""Fidget 二期：为 c8 覆盖率门槛失败解析确定性 owner 候选。

与一期一致性：
- 确定性逻辑支持"定位候选人 + 给 medium_confidence"，**medium 是当前 coverage 上限**；
- 从多人里选"最后/最近/最多"提交者是被禁止的，多作者一律降级 no_high_confidence_owner；
- coverage 采用方案 X，由本模块单一写入，Agent 不参与 coverage 建项或置信度提升。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ci_owner_agent.constants import NO_OWNER_NAME
from ci_owner_agent.schemas import CiResponsibilityNotice, EvidenceItem, Owner, ResponsibilityItem
from ci_owner_agent.services.git_client import GitClient
from ci_owner_agent.services.investigation_scope import InvestigationScope

CoverageOwnerType = str  # "medium_confidence" | "no_high_confidence_owner"

_COVERAGE_POLICY_MARKERS = ("coverage", "覆盖率", "c8")
_NON_ASSIGNMENT_MARKERS = (
    "不为其分配责任人",
    "不分配责任人",
    "不生成责任项",
    "不创建责任项",
    "does not assign an owner",
    "does not create a responsibility item",
)

# Fidget 参与 c8 check-coverage 门槛的候选包（AGENTS.md：100% 覆盖率集合不含 fidget-sdk）。
# 用于 C 消歧：无可信包名时，在这些包的完整路径下校验覆盖率文件唯一命中。
FIDGET_COVERAGE_PACKAGES: tuple[str, ...] = (
    "fidget-core",
    "fidget-sql",
    "fidget-lake",
    "fidget-mongo",
    "fidget-postgres",
)


def is_stale_coverage_non_assignment_claim(value: str | None) -> bool:
    """识别 Agent 阶段遗留、与最终 coverage 责任项冲突的流程说明。"""
    text = str(value or "").strip().lower()
    return (
        any(marker in text for marker in _COVERAGE_POLICY_MARKERS)
        and any(marker in text for marker in _NON_ASSIGNMENT_MARKERS)
    )


def strip_stale_coverage_non_assignment_claims(value: str | None) -> str:
    """删除冲突句，保留同一 failureReason 中其它真实失败说明。"""
    parts = re.split(r"(?<=[。！？!?])|\r?\n", str(value or ""))
    return "".join(part for part in parts if not is_stale_coverage_non_assignment_claim(part)).strip()


def resolve_coverage_package(
    *,
    repo: str,
    raw_coverage_path: str,
    git_client: GitClient,
    trusted_head_commit: str,
) -> str | None:
    """候选包消歧（C）：在可信 headCommit 批量校验完整路径，唯一命中才返回包名。

    规则：
    - 一次性 `list_paths` 查询所有候选包的完整路径（list_paths 支持批量 paths）；
    - 恰好 1 个完整路径存在 → 确定的 package；
    - 0 个 / 多于 1 个 / git 校验失败 → None（fail closed，交调用方降级 no-owner）。
    """
    relative = raw_coverage_path.lstrip("/")
    candidates = [f"packages/{pkg}/{relative}" for pkg in FIDGET_COVERAGE_PACKAGES]
    listing = git_client.list_paths(repo, trusted_head_commit, candidates)
    if not listing.get("ok"):
        return None
    existing = listing.get("paths") or []
    # 只关心命中候选包完整路径的行
    matches = [path for path in existing if path in candidates]
    if len(matches) != 1:
        return None
    hit = matches[0]
    try:
        return hit.split("/")[1]
    except IndexError:
        return None


def resolve_coverage_responsibility(
    *,
    repo: str,
    raw_coverage_path: str,
    trusted_package_name: str | None,
    git_client: GitClient,
    investigation_scope: InvestigationScope,
    trusted_head_commit: str,
) -> CoverageOwnerResolution:
    """coverage 失败 → 确定性 owner 责任项的完整解析。

    组合 B（可信包名，若无）+ C（候选包唯一命中消歧）+ owner resolve：
    1. package_name 优先用调用方已确认的可信包名（来自 chunk 的 B 归属）；
    2. 为空时用 C 消歧（候选包唯一命中）；
    3. 仍无法确定包 / 文件不在 head / diff 无作者 / 多作者 → no_high_confidence_owner。
    """
    if trusted_package_name:
        package_name = trusted_package_name
    else:
        package_name = resolve_coverage_package(
            repo=repo,
            raw_coverage_path=raw_coverage_path,
            git_client=git_client,
            trusted_head_commit=trusted_head_commit,
        )
        if package_name is None:
            return CoverageOwnerResolution(
                resolved_path=None,
                owner_candidate=None,
                owner_type="no_high_confidence_owner",
                source_commit=None,
                scope_used="full",
                diff_evidence=False,
                reason=f"cannot disambiguate package for coverage path {raw_coverage_path!r} (C unique-match failed)",
            )
    return resolve_coverage_owner_candidate(
        repo=repo,
        package_name=package_name,
        raw_coverage_path=raw_coverage_path,
        git_client=git_client,
        investigation_scope=investigation_scope,
        trusted_head_commit=trusted_head_commit,
    )


@dataclass(frozen=True)
class CoverageOwnerResolution:
    """确定性解析结果：候选 owner 与判定理由。

    ownerType 仅为 medium_confidence（唯一作者且 diff 非空）或
    no_high_confidence_owner（无作者 / 多作者 / git 校验失败 / 文件不在 diff）。
    """

    resolved_path: str | None
    owner_candidate: dict[str, Any] | None  # {name, email, commit}，命中时填充
    owner_type: CoverageOwnerType
    source_commit: str | None
    scope_used: str  # "focus" | "full"
    diff_evidence: bool
    reason: str


def resolve_coverage_owner_candidate(
    *,
    repo: str,
    package_name: str,
    raw_coverage_path: str,
    git_client: GitClient,
    investigation_scope: InvestigationScope,
    trusted_head_commit: str,
) -> CoverageOwnerResolution:
    """在可信 headCommit 上，为覆盖率失败文件解析确定性的 owner 候选。

    规则（对齐一期"宁缺毋滥"）：
    1. 校验完整路径 `packages/<pkg>/<raw_path>` 在 headCommit 中存在；
    2. 优先 focusRange，不能解释再用 fullRange，并记录扩展原因；
    3. 文件不在 diff / diff 为空 / git 校验失败 → no_high_confidence_owner；
    4. 唯一作者且 diff 非空 → current_build_owner + medium_confidence；
    5. 多作者 → no_high_confidence_owner（不选最后/最近/最多）。
    """
    # c8 输出的是包内相对路径（如 `src/generator/SqlUtils.ts`），git 需要仓库相对完整
    # 路径（`packages/<pkg>/src/...`）。统一以 `packages/<pkg>/` + 原始路径为完整路径。
    full_path = f"packages/{package_name}/{raw_coverage_path.lstrip('/')}" if package_name else raw_coverage_path

    # 1. 校验完整路径在可信 headCommit 存在
    listing = git_client.list_paths(repo, trusted_head_commit, [full_path])
    if not listing.get("ok") or full_path not in (listing.get("paths") or []):
        return CoverageOwnerResolution(
            resolved_path=None,
            owner_candidate=None,
            owner_type="no_high_confidence_owner",
            source_commit=None,
            scope_used="full",
            diff_evidence=False,
            reason=f"coverage path {full_path!r} not present at trusted head commit {trusted_head_commit[:8]}",
        )

    # 2&3&4&5. 先 focus 再 full；首个能给出结论的范围直接定案
    for scope in ("focus", "full"):
        base, head = investigation_scope.range_for_scope(scope)
        if not base or not head:
            continue
        diff = git_client.get_file_diff(repo, base, head, full_path)
        if not diff.get("ok"):
            return CoverageOwnerResolution(
                resolved_path=full_path,
                owner_candidate=None,
                owner_type="no_high_confidence_owner",
                source_commit=None,
                scope_used=scope,
                diff_evidence=False,
                reason=f"git diff failed for {full_path}: {diff.get('error')}",
            )
        diff_text = diff.get("diff") or ""
        if not diff_text.strip():
            # 文件存在但本窗口无改动 → 非本次引入；若还有 full 窗口则继续尝试，否则定 no-owner
            if scope == "focus":
                continue
            return CoverageOwnerResolution(
                resolved_path=full_path,
                owner_candidate=None,
                owner_type="no_high_confidence_owner",
                source_commit=None,
                scope_used=scope,
                diff_evidence=False,
                reason=f"no diff for {full_path} in {scope} range (file unchanged since base)",
            )
        authors = diff.get("authors") or []
        if len(authors) == 0:
            return CoverageOwnerResolution(
                resolved_path=full_path,
                owner_candidate=None,
                owner_type="no_high_confidence_owner",
                source_commit=None,
                scope_used=scope,
                diff_evidence=True,
                reason=f"no author for {full_path} in {scope} range",
            )
        if len(authors) > 1:
            names = ", ".join(a.get("name", "") for a in authors)
            return CoverageOwnerResolution(
                resolved_path=full_path,
                owner_candidate=None,
                owner_type="no_high_confidence_owner",
                source_commit=None,
                scope_used=scope,
                diff_evidence=True,
                reason=f"multiple authors modified {full_path} ({names}); not selecting last/recent/most",
            )
        author = authors[0]
        commit = (author.get("commits") or [None])[0]
        return CoverageOwnerResolution(
            resolved_path=full_path,
            owner_candidate={"name": author.get("name"), "email": author.get("email"), "commit": commit},
            owner_type="medium_confidence",
            source_commit=commit,
            scope_used=scope,
            diff_evidence=True,
            reason=f"single author {author.get('name')} modified {full_path} in {scope} range",
        )

    return CoverageOwnerResolution(
        resolved_path=full_path,
        owner_candidate=None,
        owner_type="no_high_confidence_owner",
        source_commit=None,
        scope_used="full",
        diff_evidence=False,
        reason=f"no applicable diff range; cannot attribute {full_path} coverage gap",
    )

def _coverage_records(failure_summaries: dict[str, Any] | None) -> list[dict[str, Any]]:
    """读取不受展示预算影响的 coverage facts，并兼容旧版 chunk-only 输入。"""
    if not isinstance(failure_summaries, dict):
        return []
    coverage_files = failure_summaries.get("coverageFiles")
    if isinstance(coverage_files, list) and coverage_files:
        return [dict(item) for item in coverage_files if isinstance(item, dict)]
    records: list[dict[str, Any]] = []
    for chunk in failure_summaries.get("chunks") or []:
        if not isinstance(chunk, dict) or chunk.get("anchorType") != "coverage_failure_block":
            continue
        signature = chunk.get("signature") or {}
        raw_path = signature.get("rawCoveragePath") or signature.get("topStackFile")
        if not raw_path and signature.get("businessStackFiles"):
            raw_path = signature["businessStackFiles"][0]
        metrics = signature.get("metrics") or {}
        if not metrics:
            metric = str(signature.get("errorMessage") or "coverage").split(" ", 1)[0]
            metrics = {metric: {}}
        records.append(
            {
                "packageName": signature.get("packageName"),
                "rawCoveragePath": raw_path,
                "metrics": metrics,
                "signatureKey": signature.get("signatureKey"),
                "content": chunk.get("content") or "",
            }
        )
    return records


def _coverage_stable_signature(
    *,
    resolved_path: str | None,
    raw_path: str | None,
    package: str | None,
    metric: str | None = None,
) -> str:
    """每文件一个稳定签名；只有 global 项需要 metric 区分。"""
    if raw_path is None:
        return f"coverage_threshold_failure|global|{package or 'unresolved'}|{metric or 'coverage'}"
    return f"coverage_threshold_failure|{resolved_path or f'unresolved|{raw_path}'}"


def _format_coverage_number(value: Any) -> str | None:
    """把解析后的 coverage 数值格式化为适合 notice 标题的紧凑文本。"""
    try:
        return f"{float(value):g}"
    except (TypeError, ValueError):
        return None


def _format_coverage_metrics(metrics: dict[str, Any]) -> str:
    """保留指标名、实际覆盖率和阈值，同时兼容缺少详情的旧摘要。"""
    parts: list[str] = []
    for metric in sorted(str(name) for name in metrics):
        detail = metrics.get(metric)
        if not isinstance(detail, dict):
            parts.append(metric)
            continue
        pct = _format_coverage_number(detail.get("pct"))
        threshold = _format_coverage_number(detail.get("threshold"))
        if pct is None or threshold is None:
            parts.append(metric)
            continue
        parts.append(f"{metric} ({pct}%/{threshold}%)")
    return ",".join(parts)


def _is_coverage_item(item: ResponsibilityItem) -> bool:
    signature = item.failureSignature or ""
    return (
        signature.startswith("coverage_threshold_failure|")
        or signature.startswith("coverage|")
        or str(item.failureId or "").startswith("coverage-")
    )


def reconcile_coverage_responsibilities(
    notice: CiResponsibilityNotice,
    *,
    failure_summaries: dict[str, Any] | None,
    repo: str,
    head_commit: str | None,
    git_client: GitClient,
    investigation_scope: InvestigationScope | None,
) -> CiResponsibilityNotice:
    """coverage 责任项的唯一生产者（方案 X）：确定性地把 coverage 失败写进 notice。

    规则：
    1. 从 failure_summaries 的完整 coverageFiles 提取事实，逐个 resolve 出 canonical owner；
    2. 移除（reconciler 是唯一生产者）Agent 意外生成的所有 coverage 项，按稳定签名识别；
    3. 追加规范化后的 coverage 项，并显式聚合顶层 owner；后续 validate_notice 负责复检。
    """
    records = _coverage_records(failure_summaries)
    if not records:
        return notice

    # 1. 移除 AI / 其它来源意外生成 coverage 项（reconciler 唯一写入口）
    notice.responsibilityItems = [
        item for item in notice.responsibilityItems if not _is_coverage_item(item)
    ]

    for record in records:
        raw_path = record.get("rawCoveragePath")
        package_name = record.get("packageName")
        metrics = record.get("metrics") or {"coverage": {}}
        metric_names = sorted(str(metric) for metric in metrics)
        metric_title = _format_coverage_metrics(metrics)
        if not raw_path:
            # global 覆盖率失败：无具体文件，直接 no-owner（无法定位责任文件）
            for metric in metric_names:
                tag = _coverage_stable_signature(
                    resolved_path=None,
                    raw_path=None,
                    package=package_name,
                    metric=metric,
                )
                evidence_ids = _append_coverage_evidence(
                    notice,
                    tag=tag,
                    content=str(record.get("content") or ""),
                    metrics={metric: metrics.get(metric) or {}},
                    resolution=None,
                )
                notice.responsibilityItems.append(
                    _build_coverage_item(
                        failure_signature=tag,
                        failure_title=(
                            f"coverage {_format_coverage_metrics({metric: metrics.get(metric) or {}})} "
                            "below threshold"
                        ),
                        file_path=None,
                        owner_type="no_high_confidence_owner",
                        owner=None,
                        confidence=0.0,
                        reason=f"coverage {metric} below threshold without a concrete file; cannot attribute",
                        evidence_ids=evidence_ids,
                    )
                )
            continue

        resolution = resolve_coverage_responsibility(
            repo=repo,
            raw_coverage_path=raw_path,
            trusted_package_name=package_name,
            git_client=git_client,
            investigation_scope=investigation_scope or InvestigationScope(
                mode="default", full_base_commit=None, full_head_commit=head_commit
            ),
            trusted_head_commit=head_commit or "",
        )
        owner = resolution.owner_candidate
        tag = _coverage_stable_signature(
            resolved_path=resolution.resolved_path,
            raw_path=raw_path,
            package=package_name,
        )
        evidence_ids = _append_coverage_evidence(
            notice,
            tag=tag,
            content=str(record.get("content") or ""),
            metrics=metrics,
            resolution=resolution,
        )
        notice.responsibilityItems.append(
            _build_coverage_item(
                failure_signature=tag,
                failure_title=f"coverage {metric_title} below threshold for {raw_path}",
                file_path=resolution.resolved_path,
                owner_type=resolution.owner_type,
                owner=owner,
                confidence=0.6 if resolution.owner_type == "medium_confidence" else 0.0,
                reason=resolution.reason,
                evidence_ids=evidence_ids,
            )
        )
    notice.suggestions = [
        suggestion
        for suggestion in notice.suggestions
        if not is_stale_coverage_non_assignment_claim(suggestion)
    ]
    cleaned_failure_reason = strip_stale_coverage_non_assignment_claims(notice.failureReason)
    notice.failureReason = cleaned_failure_reason or "构建失败：覆盖率门槛未达标，责任项见 responsibilityItems。"
    _aggregate_top_level_owner(notice)
    return notice


def _append_coverage_evidence(
    notice: CiResponsibilityNotice,
    *,
    tag: str,
    content: str,
    metrics: dict[str, Any],
    resolution: CoverageOwnerResolution | None,
) -> list[str]:
    """为每个 coverage item 建立自己的 c8 日志证据和可用的 diff 证据。"""
    import hashlib

    suffix = hashlib.sha256(tag.encode("utf-8")).hexdigest()[:12]
    evidence_ids = {item.id for item in notice.evidence}
    result: list[str] = []
    log_id = f"coverage-log-{suffix}"
    if log_id not in evidence_ids:
        notice.evidence.append(
            EvidenceItem(
                id=log_id,
                type="log",
                summary="c8 coverage threshold failure",
                detail=(content or str(metrics))[:2000],
                source="c8",
            )
        )
        evidence_ids.add(log_id)
    result.append(log_id)
    if resolution is not None and resolution.diff_evidence and resolution.resolved_path:
        diff_id = f"coverage-diff-{suffix}"
        if diff_id not in evidence_ids:
            notice.evidence.append(
                EvidenceItem(
                    id=diff_id,
                    type="diff",
                    summary=f"coverage file changed in {resolution.scope_used} responsibility range",
                    detail=resolution.reason[:2000],
                    source=resolution.resolved_path,
                )
            )
        result.append(diff_id)
    return result


def _aggregate_top_level_owner(notice: CiResponsibilityNotice) -> None:
    """按责任项显式聚合构建级 owner；schema 本身只会降级，不会提升。"""
    current_items = [
        item
        for item in notice.responsibilityItems
        if item.responsibilityType == "current_build_owner"
        and item.owner.type in {"high_confidence", "medium_confidence"}
        and item.owner.name
        and item.owner.name != NO_OWNER_NAME
    ]
    identity_keys = {
        (item.owner.name, item.owner.email, item.owner.commit, item.responsibilityType)
        for item in current_items
    }
    if len(identity_keys) != 1:
        notice.owner = Owner(
            type="no_high_confidence_owner",
            name=NO_OWNER_NAME,
            email=None,
            commit=None,
            confidence=0.0,
        )
        notice.hasHighConfidenceOwner = False
        return
    selected = next(
        (item for item in current_items if item.owner.type == "high_confidence"),
        current_items[0],
    )
    notice.owner = Owner.model_validate(selected.owner.model_dump())
    notice.hasHighConfidenceOwner = selected.owner.type == "high_confidence"


def _build_coverage_item(
    *,
    failure_signature: str,
    failure_title: str,
    file_path: str | None,
    owner_type: str,
    owner: dict[str, Any] | None,
    confidence: float,
    reason: str,
    evidence_ids: list[str],
) -> ResponsibilityItem:
    """构造一个 canonical coverage ResponsibilityItem。"""
    if owner_type == "medium_confidence" and owner:
        owner_obj = Owner(
            type="medium_confidence",
            name=owner.get("name") or NO_OWNER_NAME,
            email=owner.get("email"),
            commit=owner.get("commit"),
            confidence=confidence,
        )
    else:
        owner_obj = Owner(
            type="no_high_confidence_owner",
            name=NO_OWNER_NAME,
            email=None,
            commit=None,
            confidence=0.0,
        )
    return ResponsibilityItem(
        failureId=_coverage_failure_id(failure_signature),
        failureTitle=failure_title,
        failureSignature=failure_signature,
        failureSummary=failure_title,
        testFilePath=None,
        failureFilePath=file_path,
        owner=owner_obj,
        responsibilityType=(
            "current_build_owner" if owner_type == "medium_confidence" else "no_high_confidence_owner"
        ),
        sourceCommit=owner.get("commit") if owner else None,
        matchType=None,
        relationship=None,
        confidence=confidence,
        reason=reason,
        evidenceIds=evidence_ids,
    )


def _coverage_failure_id(signature: str) -> str:
    import hashlib

    return "coverage-" + hashlib.sha256(signature.encode("utf-8")).hexdigest()[:12]
