from __future__ import annotations

from ci_owner_agent.schemas import BuildInfo, CiResponsibilityNotice, Owner, ResponsibilityItem
from tests.fx_code_test.test_history_store import make_store


def _notice(items, result="FAILURE"):
    owner = Owner(type="no_high_confidence_owner", name="暂无高置信责任人", confidence=0)
    return CiResponsibilityNotice(repo="r", job="j", buildNumber=1, buildUrl="u", result=result, branch="dev",
                                  owner=owner, failureReason="x", responsibilityItems=items, hasHighConfidenceOwner=False)


def _item(identity, path):
    return ResponsibilityItem(failureId=identity, failureTitle=identity, failureSignature=identity,
                              testFilePath=path, owner=Owner(type="no_high_confidence_owner", name="暂无高置信责任人", confidence=0),
                              responsibilityType="no_high_confidence_owner", confidence=0, reason="x")


def test_event_store_groups_items_replaces_rerun_and_normalizes_utc():
    store = make_store(); build = BuildInfo(job="j", buildNumber=1, result="FAILURE", buildUrl="u", branch="dev",
                                            timestamp="2026-07-13T16:35:00+08:00")
    assert store.replace_test_file_failures(build_info=build, notice=_notice([_item("a", "test/A.test.ts"), _item("b", "test/A.test.ts")]))["testFileFailuresSaved"] == 1
    assert store.test_file_failures.docs[0]["failureItemCount"] == 2
    assert store.test_file_failures.docs[0]["buildTimestamp"].isoformat() == "2026-07-13T08:35:00+00:00"
    store.replace_test_file_failures(build_info=build, notice=_notice([_item("c", "test/B.test.ts")]))
    assert [d["testFilePath"] for d in store.test_file_failures.docs] == ["test/B.test.ts"]


def test_event_store_rejects_missing_unsafe_and_source_paths_and_success_has_no_events():
    store = make_store(); build = BuildInfo(job="j", buildNumber=1, result="FAILURE", buildUrl="u", branch="dev")
    result = store.replace_test_file_failures(build_info=build, notice=_notice([
        _item("a", None), _item("b", "node_modules/x.test.ts"), _item("c", "src/app.ts")]))
    assert result == {"testFileFailuresSaved": 0, "unidentifiedTestFileItems": 3}
    build.result = "SUCCESS"
    store.replace_test_file_failures(build_info=build, notice=_notice([_item("d", "test/D.test.ts")], result="SUCCESS"))
    assert store.test_file_failures.docs == []
