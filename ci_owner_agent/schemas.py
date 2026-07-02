from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


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


OwnerType = Literal["high_confidence", "medium_confidence", "no_high_confidence_owner"]


class Owner(StrictModel):
    type: OwnerType
    name: str
    email: str | None = None
    commit: str | None = None
    confidence: float


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
    hasHighConfidenceOwner: bool

    @model_validator(mode="after")
    def enforce_owner_consistency(self) -> "CiResponsibilityNotice":
        if self.owner.type == "high_confidence":
            self.hasHighConfidenceOwner = True
        else:
            self.hasHighConfidenceOwner = False
        if self.owner.name == "无高可信责任人" or self.owner.type == "no_high_confidence_owner":
            self.owner = Owner(
                type="no_high_confidence_owner",
                name="无高可信责任人",
                email=None,
                commit=None,
                confidence=0,
            )
            self.hasHighConfidenceOwner = False
        return self
