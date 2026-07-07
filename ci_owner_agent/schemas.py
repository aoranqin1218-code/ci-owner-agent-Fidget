from __future__ import annotations

import hashlib
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ci_owner_agent.constants import NO_OWNER_NAME


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LogTail(StrictModel):
    startLine: int
    endLine: int
    content: str


class BuildInfo(StrictModel):
    job: str
    buildNumber: int
    result: str
    buildUrl: str
    branch: str | None = None
    commit: str | None = None
    timestamp: str | None = None
    durationMs: int | None = None
    logTail: LogTail | None = None
    warnings: list[str] = Field(default_factory=list)


class SuccessfulBuildInfo(StrictModel):
    buildNumber: int
    result: str
    commit: str | None = None
    buildUrl: str
    warnings: list[str] = Field(default_factory=list)


class CommitInfo(StrictModel):
    hash: str
    authorName: str
    authorEmail: str
    subject: str
    timestamp: str | None = None


class FileAuthor(StrictModel):
    name: str
    email: str | None = None
    commits: list[str] = Field(default_factory=list)


class ChangedFile(StrictModel):
    path: str
    status: str
    additions: int | None = None
    deletions: int | None = None
    authors: list[FileAuthor] = Field(default_factory=list)


class KeywordMatch(StrictModel):
    keyword: str
    file: str
    line: int
    text: str
    changedInRange: bool


EvidenceType = Literal[
    "log",
    "diff",
    "commit",
    "keyword_match",
    "ts_symbol",
    "file_content",
    "reasoning",
    "build_info",
]


class EvidenceItem(StrictModel):
    id: str
    type: EvidenceType
    summary: str
    detail: str
    source: str | None = None


OwnerType = Literal[
    "high_confidence",
    "medium_confidence",
    "no_high_confidence_owner",
    "inherited_failure_owner",
]


class Owner(StrictModel):
    type: OwnerType
    name: str
    email: str | None = None
    commit: str | None = None
    confidence: float


ResponsibilityType = Literal[
    "current_build_owner",
    "inherited_failure_owner",
    "no_high_confidence_owner",
    "unknown",
]


class ResponsibilityItem(StrictModel):
    failureId: str
    failureTitle: str
    failureSignature: str | None = None
    failureSummary: str | None = None
    owner: Owner
    responsibilityType: ResponsibilityType
    sourceBuildNumber: int | None = None
    sourceBuildUrl: str | None = None
    sourceCommit: str | None = None
    matchType: str | None = None
    relationship: str | None = None
    confidence: float
    reason: str
    evidenceIds: list[str] = Field(default_factory=list)


class FailureFact(StrictModel):
    schemaVersion: int = 1
    factId: str | None = None
    signatureKey: str
    historyEligible: bool
    isGenericWrapper: bool = False
    failureKind: str
    phase: str | None = None
    command: str | None = None
    errorCode: str | None = None
    errorType: str | None = None
    packageName: str | None = None
    filePath: str | None = None
    symbol: str | None = None
    message: str
    rootCauseSummary: str
    evidenceLines: list[str] = Field(default_factory=list)
    startLine: int | None = None
    endLine: int | None = None
    confidence: float

    @model_validator(mode="after")
    def normalize_fact(self) -> "FailureFact":
        self.confidence = max(0, min(float(self.confidence or 0), 1))
        self.signatureKey = str(self.signatureKey or "").strip()
        if self.historyEligible and not self.signatureKey:
            raise ValueError("signatureKey is required when historyEligible=true")
        if not self.factId:
            self.factId = stable_failure_fact_id(self)
        return self


class FailureFactExtractionResult(StrictModel):
    ok: bool
    facts: list[FailureFact] = Field(default_factory=list)
    warning: str | None = None


class FailureFactComparison(StrictModel):
    sameFailure: bool
    confidence: float
    relationship: Literal[
        "same_root_cause",
        "different_root_cause",
        "unclear",
        "blocked_by_generic_wrapper",
        "blocked_by_low_confidence",
    ]
    samePoints: list[str] = Field(default_factory=list)
    differentPoints: list[str] = Field(default_factory=list)
    reason: str

    @model_validator(mode="after")
    def normalize_comparison(self) -> "FailureFactComparison":
        self.confidence = max(0, min(float(self.confidence or 0), 1))
        if not self.sameFailure and self.relationship == "same_root_cause":
            self.relationship = "unclear"
        return self


class CiResponsibilityNotice(StrictModel):
    job: str
    buildNumber: int
    buildUrl: str
    result: str
    branch: str | None = None
    headCommit: str | None = None
    baseCommit: str | None = None
    owner: Owner
    failureReason: str
    evidence: list[EvidenceItem] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)
    responsibilityItems: list[ResponsibilityItem] = Field(default_factory=list)
    hasHighConfidenceOwner: bool

    @model_validator(mode="after")
    def enforce_owner_consistency(self) -> "CiResponsibilityNotice":
        for item in self.responsibilityItems:
            _normalize_responsibility_item(item, self)
        responsible_owners = {
            (
                item.owner.name,
                item.owner.email,
                item.owner.commit,
                item.responsibilityType,
            )
            for item in self.responsibilityItems
            if item.owner.type in {"high_confidence", "medium_confidence", "inherited_failure_owner"}
            and item.owner.name
            and item.owner.name != NO_OWNER_NAME
        }
        if len(responsible_owners) > 1:
            self.owner = Owner(
                type="no_high_confidence_owner",
                name=NO_OWNER_NAME,
                email=None,
                commit=None,
                confidence=0,
            )
            self.hasHighConfidenceOwner = False
            return self
        if self.owner.type == "high_confidence":
            self.hasHighConfidenceOwner = True
        else:
            self.hasHighConfidenceOwner = False
        if self.owner.name == NO_OWNER_NAME or self.owner.type == "no_high_confidence_owner":
            self.owner = Owner(
                type="no_high_confidence_owner",
                name=NO_OWNER_NAME,
                email=None,
                commit=None,
                confidence=0,
            )
            self.hasHighConfidenceOwner = False
        return self


def _no_owner() -> Owner:
    return Owner(type="no_high_confidence_owner", name=NO_OWNER_NAME, email=None, commit=None, confidence=0)


def stable_failure_id(item: ResponsibilityItem) -> str:
    basis = item.failureSignature or f"{item.failureTitle}\n{item.failureSummary or ''}"
    normalized = _normalize_identifier_basis(basis)
    return "failure-" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]


def stable_failure_fact_id(fact: FailureFact) -> str:
    normalized = _normalize_identifier_basis(fact.signatureKey)
    return "fact-" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]


def _fallback_failure_signature(item: ResponsibilityItem) -> str:
    basis = _normalize_identifier_basis(f"{item.failureTitle}\n{item.failureSummary or ''}")
    return "manual:" + hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


def _normalize_identifier_basis(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip().lower()


def _normalize_responsibility_item(item: ResponsibilityItem, notice: CiResponsibilityNotice) -> None:
    if not item.failureSignature or not item.failureSignature.strip():
        item.failureSignature = _fallback_failure_signature(item)
    item.failureId = stable_failure_id(item)
    item.confidence = max(0, min(float(item.confidence or 0), 1))
    if item.sourceBuildNumber == 0:
        item.sourceBuildNumber = None

    if item.responsibilityType == "inherited_failure_owner":
        valid_inherited = (
            item.owner.type == "inherited_failure_owner"
            and bool(item.owner.name)
            and item.owner.name != NO_OWNER_NAME
            and bool(item.sourceBuildNumber and item.sourceBuildNumber > 0)
        )
        if not valid_inherited:
            _downgrade_item(item, "历史持续失败未找到可继承责任人，降级为无高可信责任人。")
        return

    if item.responsibilityType == "current_build_owner":
        if item.owner.type == "no_high_confidence_owner" or not item.owner.name or item.owner.name == NO_OWNER_NAME:
            _downgrade_item(item, "当前失败未找到高可信责任人，降级为无高可信责任人。")
            return
        if not item.sourceBuildNumber:
            item.sourceBuildNumber = notice.buildNumber
        if not item.sourceCommit:
            item.sourceCommit = item.owner.commit or notice.headCommit
        return

    if item.responsibilityType in {"no_high_confidence_owner", "unknown"}:
        _downgrade_item(item, item.reason or "证据不足，无法确定高可信责任人。")


def _downgrade_item(item: ResponsibilityItem, reason: str) -> None:
    item.responsibilityType = "no_high_confidence_owner"
    item.owner = _no_owner()
    item.sourceBuildNumber = None
    item.sourceBuildUrl = None
    item.sourceCommit = None
    item.confidence = 0
    item.reason = reason
