from __future__ import annotations

from ci_owner_agent.constants import NO_OWNER_NAME
from ci_owner_agent.schemas import CiResponsibilityNotice, Owner


def no_owner() -> Owner:
    return Owner(
        type="no_high_confidence_owner",
        name=NO_OWNER_NAME,
        email=None,
        commit=None,
        confidence=0,
    )


def downgrade_to_no_high_confidence(notice: CiResponsibilityNotice, reason: str) -> CiResponsibilityNotice:
    notice.owner = no_owner()
    notice.hasHighConfidenceOwner = False
    notice.failureReason = reason
    if "已因证据不足降级为无高可信责任人。" not in notice.suggestions:
        notice.suggestions.append("已因证据不足降级为无高可信责任人。")
    return CiResponsibilityNotice.model_validate(notice.model_dump())


def validate_notice(notice: CiResponsibilityNotice) -> CiResponsibilityNotice:
    evidence_types = {item.type for item in notice.evidence}
    if notice.owner.type == "high_confidence":
        node_modules_only = bool(notice.evidence) and all(
            "node_modules" in ((item.source or "") + " " + item.detail).replace("\\", "/")
            for item in notice.evidence
        )
        invalid = (
            not notice.owner.name
            or not notice.owner.commit
            or notice.owner.confidence < 0.8
            or len(evidence_types) < 2
            or evidence_types in ({"commit"}, {"diff"}, {"keyword_match"})
            or "log" not in evidence_types
            or not ({"diff", "keyword_match", "ts_symbol"} & evidence_types)
            or node_modules_only
        )
        if invalid:
            return downgrade_to_no_high_confidence(
                notice,
                "构建失败，但当前证据不足以输出高可信责任人；必须至少具备日志线索和本次变更相关证据。",
            )
        notice.hasHighConfidenceOwner = True
    elif notice.owner.type == "medium_confidence":
        notice.hasHighConfidenceOwner = False
    else:
        notice.owner = no_owner()
        notice.hasHighConfidenceOwner = False
    return CiResponsibilityNotice.model_validate(notice.model_dump())
