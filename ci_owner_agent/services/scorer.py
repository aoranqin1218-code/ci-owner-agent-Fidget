from __future__ import annotations

from ci_owner_agent.schemas import CiResponsibilityNotice, Owner


NO_OWNER_NAME = "无高可信责任人"


def no_owner() -> Owner:
    return Owner(
        type="no_high_confidence_owner",
        name=NO_OWNER_NAME,
        email=None,
        commit=None,
        confidence=0,
    )


def validate_notice(notice: CiResponsibilityNotice) -> CiResponsibilityNotice:
    evidence_types = {item.type for item in notice.evidence}
    if notice.owner.type == "high_confidence":
        if len(evidence_types) < 2 or "log" not in evidence_types or not ({"diff", "keyword_match", "ts_symbol"} & evidence_types):
            notice.owner = no_owner()
            notice.hasHighConfidenceOwner = False
            notice.failureReason = (
                "构建失败，但当前证据不足以输出高可信责任人；必须至少具备日志线索和本次变更相关证据。"
            )
    elif notice.owner.type == "medium_confidence":
        notice.hasHighConfidenceOwner = False
    else:
        notice.owner = no_owner()
        notice.hasHighConfidenceOwner = False
    return CiResponsibilityNotice.model_validate(notice.model_dump())
