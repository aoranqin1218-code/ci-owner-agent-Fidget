from __future__ import annotations

from ci_owner_agent.schemas import CiResponsibilityNotice, EvidenceItem, Owner
from ci_owner_agent.services.scorer import validate_notice


def test_high_confidence_requires_two_evidence_types():
    notice = CiResponsibilityNotice(
        job="job",
        buildNumber=1,
        buildUrl="local://job/1",
        result="FAILURE",
        branch="dev",
        headCommit="h",
        baseCommit="b",
        owner=Owner(type="high_confidence", name="Zhang San", email="z@example.com", commit="h", confidence=0.9),
        failureReason="test",
        evidence=[EvidenceItem(id="E1", type="commit", summary="only commit", detail="commit")],
        suggestions=[],
        hasHighConfidenceOwner=True,
    )
    checked = validate_notice(notice)
    assert checked.owner.type == "no_high_confidence_owner"
    assert checked.hasHighConfidenceOwner is False
