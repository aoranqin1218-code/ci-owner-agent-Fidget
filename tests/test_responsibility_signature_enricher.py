from __future__ import annotations

import re

import pytest

from ci_owner_agent.schemas import CiResponsibilityNotice
from ci_owner_agent.services.responsibility_signature_enricher import enrich_responsibility_item_signatures


OBJECT_A = "6a546a6c5740ffb771af5462"
OBJECT_B = "7b1234567890abcdef123456"


def _item(
    signature: str | None,
    *,
    title: str = "failure",
    summary: str = "summary",
    path: str | None = None,
    responsibility_type: str = "no_high_confidence_owner",
) -> dict:
    no_owner = responsibility_type == "no_high_confidence_owner"
    owner_type = "no_high_confidence_owner" if no_owner else responsibility_type.replace("current_build_owner", "high_confidence")
    if responsibility_type == "inherited_failure_owner":
        owner_type = "inherited_failure_owner"
    return {
        "failureId": "model-id",
        "failureTitle": title,
        "failureSignature": signature,
        "failureSummary": summary,
        "testFilePath": path,
        "owner": {
            "type": owner_type,
            "name": "无高可信责任人" if no_owner else "Owner",
            "commit": None if no_owner else "head",
            "confidence": 0 if no_owner else 0.9,
        },
        "responsibilityType": responsibility_type,
        "sourceBuildNumber": 7 if responsibility_type == "inherited_failure_owner" else None,
        "confidence": 0 if no_owner else 0.9,
        "reason": "reason",
    }


def _notice(items: list[dict]) -> CiResponsibilityNotice:
    return CiResponsibilityNotice.model_validate(
        {
            "job": "job",
            "buildNumber": 8,
            "buildUrl": "local://8",
            "result": "FAILURE",
            "headCommit": "head",
            "baseCommit": "base",
            "owner": {"type": "no_high_confidence_owner", "name": "无高可信责任人", "confidence": 0},
            "failureReason": "failed",
            "responsibilityItems": items,
            "hasHighConfidenceOwner": False,
        }
    )


def test_dynamic_mongodb_item_signature_and_id_are_stable():
    def build(object_id: str):
        return _notice(
            [
                _item(
                    f"E11000_duplicate_key_finex.bpm_tasks__id_{object_id}",
                    title="E11000 duplicate key error on finex.bpm_tasks._id_",
                    summary=(
                        "MongoDB duplicate key error collection: finex.bpm_tasks index: _id_ "
                        f"dup key ObjectId('{object_id}')"
                    ),
                )
            ]
        ).responsibilityItems[0]

    first = build(OBJECT_A)
    second = build(OBJECT_B)
    assert first.failureSignature == "mongodb_duplicate_key|e11000|finex.bpm_tasks|_id_"
    assert second.failureSignature == first.failureSignature
    assert second.failureId == first.failureId
    assert re.fullmatch(r"failure-[0-9a-f]{12}", first.failureId)


def test_display_text_preserves_case_while_redacting_dynamic_values():
    notice = _notice(
        [
            _item(
                f"failure-{OBJECT_A}",
                title="EtlUtils - getInputEntryInfo",
                summary=f"Failed for ObjectId('{OBJECT_A}')",
            )
        ]
    )

    enrich_responsibility_item_signatures(notice, None, None)

    item = notice.responsibilityItems[0]
    assert item.failureTitle == "EtlUtils - getInputEntryInfo"
    assert item.failureSummary == "Failed for <object_id>"


def test_unique_summary_path_overrides_model_signature():
    notice = _notice([_item("model-wrong", path="test/a.test.ts")])
    summaries = {
        "chunks": [
            {
                "signature": {"signatureKey": "summary-a", "testFile": "test/a.test.ts"},
                "signatureHash": "hash-a",
            }
        ]
    }

    enrich_responsibility_item_signatures(notice, summaries, None)

    assert notice.responsibilityItems[0].failureSignature == "summary-a"


def test_unique_failure_fact_path_is_used_when_summary_missing():
    notice = _notice([_item("model-wrong", path="test/a.test.ts")])
    facts = {
        "facts": [
            {
                "signatureKey": "model-fact",
                "failureKind": "typescript_compile_error",
                "errorCode": "TS2305",
                "filePath": "test/a.test.ts",
                "message": "missing export",
                "rootCauseSummary": "missing export",
            }
        ]
    }

    enrich_responsibility_item_signatures(notice, None, facts)

    assert notice.responsibilityItems[0].failureSignature.startswith("typescript_compile_error|ts2305|test/a.test.ts")


def test_multiple_failures_are_matched_by_own_paths():
    notice = _notice(
        [
            _item("wrong-a", path="test/a.test.ts"),
            _item("wrong-b", path="test/b.test.ts"),
        ]
    )
    summaries = {
        "chunks": [
            {"signature": {"signatureKey": "summary-a", "testFile": "test/a.test.ts"}},
            {"signature": {"signatureKey": "summary-b", "testFile": "test/b.test.ts"}},
        ]
    }

    enrich_responsibility_item_signatures(notice, summaries, None)

    assert [item.failureSignature for item in notice.responsibilityItems] == ["summary-a", "summary-b"]


def test_ambiguous_candidates_do_not_replace_item_signature():
    notice = _notice([_item("own-signature", title="TypeError", summary="same error")])
    summaries = {
        "chunks": [
            {"signature": {"signatureKey": "summary-a", "errorType": "TypeError"}},
            {"signature": {"signatureKey": "summary-b", "errorType": "TypeError"}},
        ]
    }

    enrich_responsibility_item_signatures(notice, summaries, None)

    assert notice.responsibilityItems[0].failureSignature == "own-signature"


@pytest.mark.parametrize("responsibility_type", ["no_high_confidence_owner", "current_build_owner", "inherited_failure_owner"])
def test_all_responsibility_types_use_canonical_signature(responsibility_type):
    item = _notice([_item(f"failure-{OBJECT_A}", responsibility_type=responsibility_type)]).responsibilityItems[0]
    assert OBJECT_A not in (item.failureSignature or "")


def test_missing_signature_gets_deterministic_fallback():
    first = _notice([_item(None, title="Webhook", summary="Run AwaitFunc Timeout")]).responsibilityItems[0]
    second = _notice([_item(None, title="Webhook", summary="Run AwaitFunc Timeout")]).responsibilityItems[0]
    assert first.failureSignature == second.failureSignature
    assert first.failureId == second.failureId
