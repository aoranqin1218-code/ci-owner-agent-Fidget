from __future__ import annotations

from scripts.backfill_test_file_failures import backfill
from tests.test_history_store import make_store


def test_backfill_dry_run_is_read_only_and_missing_timestamp_stays_missing():
    store = make_store()
    store.notices.docs.append({
        "repo": "r", "job": "j", "branch": "dev", "buildNumber": 1,
        "notice": {
            "repo": "r", "job": "j", "branch": "dev", "buildNumber": 1, "buildUrl": "u", "result": "FAILURE",
            "owner": {"type": "no_high_confidence_owner", "name": "暂无高置信责任人", "confidence": 0},
            "failureReason": "x", "hasHighConfidenceOwner": False,
            "responsibilityItems": [{"failureId": "a", "failureTitle": "a", "failureSignature": "a",
              "testFilePath": "test/A.test.ts", "owner": {"type": "no_high_confidence_owner", "name": "暂无高置信责任人", "confidence": 0},
              "responsibilityType": "no_high_confidence_owner", "confidence": 0, "reason": "x"}],
        },
    })
    result = backfill(store, repo="r", dry_run=True)
    assert result["createdEventCount"] == 1
    assert result["missingBuildTimestampCount"] == 1
    assert store.test_file_failures.docs == []
    backfill(store, repo="r")
    assert store.test_file_failures.docs[0]["buildTimestamp"] is None
    again = backfill(store, repo="r")
    assert again["skippedEventCount"] == 1
