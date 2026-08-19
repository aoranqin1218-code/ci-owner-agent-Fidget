from __future__ import annotations

import json

import pytest

from ci_owner_agent.schemas import BuildInfo, CiResponsibilityNotice, FailureFact
from ci_owner_agent.services.feedback_store import FeedbackStore
from ci_owner_agent.services.wecom_notification_routing import notification_digest
from ci_owner_agent.services.responsibility_signature_enricher import enrich_responsibility_item_signatures
from tests.test_history_store import make_store
from tests.test_notification_formatter import item, notice_payload


OBJECT_A = "6a546a6c5740ffb771af5462"
OBJECT_B = "7b1234567890abcdef123456"


def _fact(object_id: str) -> FailureFact:
    return FailureFact(
        signatureKey=f"model-e11000-{object_id}",
        historyEligible=True,
        failureKind="mongodb_duplicate_key",
        errorCode="E11000",
        errorType="MongoServerError",
        message=(
            "E11000 duplicate key error collection: finex.bpm_tasks "
            f"index: _id_ dup key: {{ _id: ObjectId('{object_id}') }}"
        ),
        rootCauseSummary="MongoDB duplicate key error collection: finex.bpm_tasks index: _id_",
        confidence=0.95,
    )


def _notice(object_id: str) -> CiResponsibilityNotice:
    notice = CiResponsibilityNotice.model_validate(
        {
            "repo": "repo",
            "job": "job",
            "buildNumber": 8,
            "buildUrl": "local://8",
            "result": "FAILURE",
            "owner": {"type": "no_high_confidence_owner", "name": "无高可信责任人", "confidence": 0},
            "failureReason": f"duplicate key ObjectId('{object_id}')",
            "evidence": [
                {
                    "id": "E1",
                    "type": "log",
                    "summary": f"E11000 {object_id}",
                    "detail": f"dup key ObjectId('{object_id}')",
                }
            ],
            "responsibilityItems": [
                {
                    "failureId": "model-id",
                    "failureTitle": "E11000 duplicate key error on finex.bpm_tasks._id_",
                    "failureSummary": (
                        "MongoDB duplicate key error collection: finex.bpm_tasks index: _id_ "
                        f"dup key ObjectId('{object_id}')"
                    ),
                    "failureSignature": f"E11000_duplicate_key_finex.bpm_tasks__id_{object_id}",
                    "owner": {"type": "no_high_confidence_owner", "name": "无高可信责任人", "confidence": 0},
                    "responsibilityType": "no_high_confidence_owner",
                    "confidence": 0,
                    "reason": f"same ObjectId('{object_id}')",
                    "evidenceIds": ["E1"],
                }
            ],
            "hasHighConfidenceOwner": False,
        }
    )
    return enrich_responsibility_item_signatures(notice, None, {"facts": [_fact(object_id).model_dump(mode="json")]})


def test_notice_and_notification_digest_ignore_object_id():
    first = _notice(OBJECT_A)
    second = _notice(OBJECT_B)
    first_item = first.responsibilityItems[0]
    second_item = second.responsibilityItems[0]
    assert first_item.failureSignature == second_item.failureSignature
    assert first_item.failureId == second_item.failureId
    assert notification_digest(first) == notification_digest(second)
    assert OBJECT_A not in first.model_dump_json()
    assert OBJECT_B not in second.model_dump_json()


def test_history_store_only_saves_canonical_notice_and_fact_identity():
    store = make_store()
    notice = _notice(OBJECT_A)
    fact = _fact(OBJECT_A)
    build = BuildInfo(job="job", buildNumber=7, result="FAILURE", buildUrl="local://7", branch="dev", commit="head")
    chunk = {
        "schemaVersion": 3,
        "chunkSource": "local_test_failure_summary",
        "content": f"E11000 duplicate key ObjectId('{OBJECT_A}')",
        "signature": {
            "signatureKey": f"E11000_duplicate_key_finex.bpm_tasks__id_{OBJECT_A}",
            "errorCode": "E11000",
            "errorMessage": f"duplicate key ObjectId('{OBJECT_A}')",
        },
    }

    store.save_analysis(build, notice, "base", "head", 6, "base", [chunk])
    store.save_failure_facts(build_info=build, notice=notice, facts=[fact])

    serialized = json.dumps(
        {
            "notices": store.notices.docs,
            "chunks": store.failure_chunks.docs,
            "facts": store.failure_facts.docs,
        },
        default=str,
    )
    assert OBJECT_A not in serialized
    assert store.failure_facts.docs[0]["signatureKey"] == "mongodb_duplicate_key|e11000|finex.bpm_tasks|_id_"


def test_same_canonical_fact_is_recalled_and_feedback_uses_canonical_signature():
    store = make_store()
    first = _fact(OBJECT_A)
    notice = _notice(OBJECT_A)
    build = BuildInfo(job="job", buildNumber=7, result="FAILURE", buildUrl="local://7", branch="dev", commit="head")
    store.save_analysis(build, notice, "base", "head", 6, "base", [])
    store.save_failure_facts(build_info=build, notice=notice, facts=[first])

    historical = store.find_historical_failure_facts(
        repo="repo",
        job="job",
        branch="dev",
        current_build_number=8,
        last_successful_build_number=6,
    )
    second = _fact(OBJECT_B)
    assert historical[0]["signatureKey"] == second.signatureKey

    feedback = FeedbackStore(store).apply_feedback(
        repo="repo",
        job="job",
        branch="dev",
        build_number=7,
        failure_id=None,
        failure_signature=notice.responsibilityItems[0].failureSignature,
        action="mark_flaky",
    )["feedback"]
    assert OBJECT_B not in (feedback["failureSignature"] or "")


def test_distinct_long_semantic_identities_do_not_collide_or_reuse_feedback():
    first_signature = "typescript_compile_error_ts2305_classifyErrorMessage"
    second_signature = "workflow_back_task_timeout_123456789012"

    def make_notice(signature: str) -> CiResponsibilityNotice:
        responsibility = item()
        responsibility["failureTitle"] = signature
        responsibility["failureSignature"] = signature
        responsibility["failureSummary"] = "semantic failure"
        return CiResponsibilityNotice.model_validate(notice_payload([responsibility]))

    first_notice = make_notice(first_signature)
    second_notice = make_notice(second_signature)
    first_fact = FailureFact(
        signatureKey="ignored-model-value",
        historyEligible=True,
        failureKind=first_signature,
        message="semantic failure",
        rootCauseSummary="semantic failure",
        confidence=0.95,
    )
    second_fact = FailureFact(
        signatureKey="ignored-model-value",
        historyEligible=True,
        failureKind=second_signature,
        message="semantic failure",
        rootCauseSummary="semantic failure",
        confidence=0.95,
    )

    first_item = first_notice.responsibilityItems[0]
    second_item = second_notice.responsibilityItems[0]
    assert first_item.failureSignature != second_item.failureSignature
    assert first_item.failureId != second_item.failureId
    assert first_fact.factId != second_fact.factId
    assert notification_digest(first_notice) != notification_digest(second_notice)

    store = make_store()
    build = BuildInfo(
        job=first_notice.job,
        buildNumber=first_notice.buildNumber,
        result=first_notice.result,
        buildUrl=first_notice.buildUrl,
        branch=first_notice.branch,
        commit=first_notice.headCommit,
    )
    store.save_analysis(build, first_notice, first_notice.baseCommit, first_notice.headCommit, None, None, [])
    with pytest.raises(ValueError, match="no longer exists"):
        FeedbackStore(store).apply_feedback(
            repo=first_notice.repo,
            job=first_notice.job,
            branch=first_notice.branch,
            build_number=first_notice.buildNumber,
            failure_id=None,
            failure_signature=second_signature,
            action="mark_flaky",
        )
