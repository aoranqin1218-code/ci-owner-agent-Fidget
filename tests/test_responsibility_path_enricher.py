from __future__ import annotations

from ci_owner_agent.schemas import CiResponsibilityNotice
from ci_owner_agent.services.responsibility_path_enricher import enrich_responsibility_item_paths, normalize_repository_path
from tests.test_notification_formatter import item, notice_payload


def _notice(signature: str = "sig") -> CiResponsibilityNotice:
    payload = notice_payload([item("无高可信责任人", "no_high_confidence_owner", "no_high_confidence_owner", "证据不足。")])
    payload["responsibilityItems"][0]["failureSignature"] = signature
    return CiResponsibilityNotice.model_validate(payload)


def test_enriches_paths_from_deterministic_summary():
    notice = _notice("summary-sig")
    result = enrich_responsibility_item_paths(
        notice,
        {
            "chunks": [
                {
                    "signature": {
                        "signatureKey": "summary-sig",
                        "testFile": "/var/app/test/service/view/ViewDataQueryServiceTest.ts",
                        "topStackFile": "/var/app/server/service/view.ts",
                    }
                }
            ]
        },
        None,
        repo="fx-code",
    )

    assert result.repo == "fx-code"
    assert result.responsibilityItems[0].testFilePath == "test/service/view/ViewDataQueryServiceTest.ts"
    assert result.responsibilityItems[0].failureFilePath == "server/service/view.ts"


def test_enriches_test_or_failure_path_from_failure_fact():
    test_notice = _notice("test-fact")
    failure_notice = _notice("failure-fact")
    enrich_responsibility_item_paths(test_notice, None, {"facts": [{"signatureKey": "test-fact", "filePath": "packages/x/Foo.test.ts"}]})
    enrich_responsibility_item_paths(failure_notice, None, {"facts": [{"signatureKey": "failure-fact", "filePath": "packages/x/src/foo.ts"}]})

    assert test_notice.responsibilityItems[0].testFilePath == "packages/x/Foo.test.ts"
    assert failure_notice.responsibilityItems[0].failureFilePath == "packages/x/src/foo.ts"


def test_summary_without_test_file_falls_through_to_failure_fact():
    notice = _notice("shared-sig")
    enrich_responsibility_item_paths(
        notice,
        {"chunks": [{"signature": {"signatureKey": "shared-sig", "errorType": "Error"}}]},
        {"facts": [{"signatureKey": "shared-sig", "filePath": "packages/x/Foo.test.ts"}]},
    )
    assert notice.responsibilityItems[0].testFilePath == "packages/x/Foo.test.ts"


def test_enriches_conservative_test_path_from_item_and_evidence_text():
    notice = _notice("missing")
    notice.responsibilityItems[0].reason = "失败位于 test/service/quota/QuotaUpdatedEventHandlerTest.ts:12:3"
    enrich_responsibility_item_paths(notice, None, None)
    assert notice.responsibilityItems[0].testFilePath == "test/service/quota/QuotaUpdatedEventHandlerTest.ts"


def test_enriches_test_path_from_associated_evidence():
    notice = _notice("missing")
    notice.responsibilityItems[0].reason = ""
    notice.responsibilityItems[0].evidenceIds = ["E1"]
    notice.evidence[0].detail = "at test/service/etl/EtlPipelineTest.ts:8:2"
    enrich_responsibility_item_paths(notice, None, None)
    assert notice.responsibilityItems[0].testFilePath == "test/service/etl/EtlPipelineTest.ts"


def test_does_not_invent_path_and_rejects_traversal():
    notice = _notice("missing")
    notice.responsibilityItems[0].reason = "timeout without a file"
    enrich_responsibility_item_paths(notice, None, None)
    assert notice.responsibilityItems[0].testFilePath is None
    assert normalize_repository_path("../test/FooTest.ts") is None
