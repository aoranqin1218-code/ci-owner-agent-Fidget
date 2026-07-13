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


def test_model_no_owner_cannot_claim_historical_source():
    payload = {
        "job": "job",
        "buildNumber": 200,
        "buildUrl": "local://job/200",
        "result": "FAILURE",
        "owner": {"type": "no_high_confidence_owner", "name": "无高可信责任人", "confidence": 0},
        "failureReason": "insufficient evidence",
        "responsibilityItems": [
            {
                "failureId": "auto",
                "failureTitle": "failure",
                "failureSignature": "sig-a",
                "owner": {"type": "no_high_confidence_owner", "name": "无高可信责任人", "confidence": 0},
                "responsibilityType": "no_high_confidence_owner",
                "sourceBuildNumber": 123,
                "sourceBuildUrl": "fake",
                "sourceCommit": "fake",
                "matchType": "signature_exact",
                "relationship": "very_likely_same_failure",
                "confidence": 0,
                "reason": "model supplied",
            }
        ],
        "hasHighConfidenceOwner": False,
    }

    item = validate_notice(CiResponsibilityNotice.model_validate(payload)).responsibilityItems[0]

    assert item.sourceBuildNumber is None
    assert item.sourceBuildUrl is None
    assert item.sourceCommit is None
    assert item.matchType is None
    assert item.relationship is None
