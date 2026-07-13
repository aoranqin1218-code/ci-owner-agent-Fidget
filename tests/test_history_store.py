from __future__ import annotations

from dataclasses import replace

from ci_owner_agent.schemas import BuildInfo, FailureFact
from ci_owner_agent.services.history_store import MongoHistoryStore, find_feedback_override_for_failure_signature, notice_hash
from ci_owner_agent.services.history_inheritance import is_no_owner_decision_item
from ci_owner_agent.services.failure_identity import build_failure_summary_signature
from ci_owner_agent.tools.history_tools import history_search_similar_failures
from tests.test_langchain_agent import high_confidence_payload, make_lc_context


class FakeCursor(list):
    def sort(self, key, direction):
        return FakeCursor(sorted(self, key=lambda item: item.get(key, 0), reverse=direction < 0))

    def limit(self, count):
        return FakeCursor(self[:count])


class FakeCollection:
    def __init__(self):
        self.docs = []
        self.indexes = []

    def create_index(self, spec, unique=False):
        self.indexes.append((tuple(spec), unique))

    def update_one(self, key, update, upsert=False):
        doc = self.find_one(key)
        is_insert = doc is None
        if doc is None:
            doc = dict(key)
            self.docs.append(doc)
        if is_insert:
            doc.update(update.get("$setOnInsert", {}))
        doc.update(update.get("$set", {}))

    def find_one(self, query):
        for doc in self.docs:
            if _matches(doc, query):
                return doc
        return None

    def find(self, query):
        return FakeCursor([doc for doc in self.docs if _matches(doc, query)])

    def delete_many(self, query):
        self.docs = [doc for doc in self.docs if not _matches(doc, query)]


class FakeDb:
    def __init__(self):
        self.collections = {}

    def __getitem__(self, name):
        self.collections.setdefault(name, FakeCollection())
        return self.collections[name]


class FakeClient:
    def __init__(self):
        self.dbs = {}

    def __getitem__(self, name):
        self.dbs.setdefault(name, FakeDb())
        return self.dbs[name]


def _matches(doc, query):
    for key, expected in query.items():
        value = doc.get(key)
        if isinstance(expected, dict):
            if "$lt" in expected and not (value < expected["$lt"]):
                return False
            if "$gt" in expected and not (value > expected["$gt"]):
                return False
            if "$gte" in expected and (value is None or not (value >= expected["$gte"])):
                return False
            if "$in" in expected and value not in expected["$in"]:
                return False
            if "$exists" in expected and (key in doc) is not expected["$exists"]:
                return False
            if "$ne" in expected and value == expected["$ne"]:
                return False
        elif value != expected:
            return False
    return True


def make_store():
    return MongoHistoryStore("mongodb://fake", "ci_owner_agent_test", client=FakeClient())


def focused_chunk(text: str, source: str = "local_test_failure_summary", signature: dict | None = None) -> dict:
    signature = signature or {
        "testName": "getJsSdkConfig dingtalk ua",
        "testCase": "dingtalk ua dingtalk corpId",
        "errorType": "Error",
        "errorMessage": "unknown",
        "testFile": "test/server/services/integrate/integrate.service.test.ts",
        "topStackFile": "node_modules/@fx/corp-core/src/errors/Factory.ts",
        "businessStackFiles": ["test/server/services/integrate/integrate.service.test.ts"],
        "signatureKey": "getJsSdkConfig dingtalk ua|dingtalk ua dingtalk corpId|Error|unknown|test/server/services/integrate/integrate.service.test.ts|node_modules/@fx/corp-core/src/errors/Factory.ts",
    }
    import hashlib

    signature = dict(signature)
    signature["signatureKey"] = build_failure_summary_signature(signature)

    return {
        "schemaVersion": 3,
        "chunkSource": source,
        "stageName": "Test",
        "stepName": "make docker-test",
        "anchorType": "mocha_failure_block",
        "startLine": 10,
        "endLine": 20,
        "score": 1.0,
        "content": text,
        "signature": signature,
        "signatureHash": hashlib.sha256(signature["signatureKey"].encode("utf-8")).hexdigest(),
    }


def inherited_notice_payload(context, *, source_build: int = 5104, owner_name: str = "Tang.Tangerine-唐嘉伟") -> dict:
    payload = high_confidence_payload(context)
    payload["owner"] = {
        "type": "no_high_confidence_owner",
        "name": "无高可信责任人",
        "email": None,
        "commit": None,
        "confidence": 0,
    }
    payload["hasHighConfidenceOwner"] = False
    payload["responsibilityItems"] = [
        {
            "failureId": "F1",
            "failureTitle": "getJsSdkConfig dingtalk ua",
            "failureSignature": "sig-1",
            "failureSummary": "same failure as first build",
            "owner": {
                "type": "inherited_failure_owner",
                "name": owner_name,
                "email": "tang@example.com",
                "commit": "ab286e5",
                "confidence": 0.9,
            },
            "responsibilityType": "inherited_failure_owner",
            "sourceBuildNumber": source_build,
            "sourceBuildUrl": f"local://services/fx-code-unittest/{source_build}",
            "sourceCommit": "ab286e5",
            "matchType": "signature_exact",
            "relationship": "very_likely_same_failure",
            "confidence": 1.0,
            "reason": "历史持续失败，继承首次失败责任人。",
            "evidenceIds": ["E1"],
        }
    ]
    return payload


def current_owner_item(
    *,
    failure_id: str,
    failure_title: str,
    failure_signature: str,
    owner_name: str,
    owner_email: str,
    owner_commit: str,
) -> dict:
    return {
        "failureId": failure_id,
        "failureTitle": failure_title,
        "failureSignature": failure_signature,
        "failureSummary": failure_title,
        "owner": {
            "type": "high_confidence",
            "name": owner_name,
            "email": owner_email,
            "commit": owner_commit,
            "confidence": 0.9,
        },
        "responsibilityType": "current_build_owner",
        "sourceCommit": owner_commit,
        "confidence": 0.9,
        "reason": "日志和 diff 支撑。",
        "evidenceIds": ["E1", "E2"],
    }


def inherited_owner_item(
    *,
    failure_id: str,
    failure_title: str,
    failure_signature: str,
    owner_name: str,
    source_build: int,
    source_commit: str,
) -> dict:
    return {
        "failureId": failure_id,
        "failureTitle": failure_title,
        "failureSignature": failure_signature,
        "failureSummary": failure_title,
        "owner": {
            "type": "inherited_failure_owner",
            "name": owner_name,
            "email": "tang@example.com",
            "commit": source_commit,
            "confidence": 0.9,
        },
        "responsibilityType": "inherited_failure_owner",
        "sourceBuildNumber": source_build,
        "sourceBuildUrl": f"local://services/fx-code-unittest/{source_build}",
        "sourceCommit": source_commit,
        "matchType": "signature_exact",
        "relationship": "very_likely_same_failure",
        "confidence": 1.0,
        "reason": "历史持续失败，继承首次失败责任人。",
        "evidenceIds": ["E1"],
    }


def no_owner_item(*, failure_id: str, failure_title: str, failure_signature: str) -> dict:
    return {
        "failureId": failure_id,
        "failureTitle": failure_title,
        "failureSignature": failure_signature,
        "failureSummary": failure_title,
        "owner": {
            "type": "no_high_confidence_owner",
            "name": "无高可信责任人",
            "email": None,
            "commit": None,
            "confidence": 0,
        },
        "responsibilityType": "no_high_confidence_owner",
        "confidence": 0,
        "reason": "历史分析无高可信责任人",
        "evidenceIds": ["E1"],
    }


class FocusedProvider:
    def __init__(self, text: str):
        self.text = text

    def find_focused_failure_chunks(self, tail_lines=500, max_chunks=3):
        return {"chunks": [focused_chunk(self.text)]}

    def find_test_failure_summaries(self, tail_lines=500, max_chunks=5):
        return {"chunks": [focused_chunk(self.text)]}


class ExplodingProvider:
    def find_test_failure_summaries(self, tail_lines=500, max_chunks=5):
        raise AssertionError("find_test_failure_summaries should not be called")


def test_mongo_history_store_upserts_build_notice_and_chunks(repo_cache, sample_repo, logs):
    context = make_lc_context(repo_cache, sample_repo, logs)
    notice = context.build_info.model_copy()
    payload = high_confidence_payload(context)
    from ci_owner_agent.schemas import CiResponsibilityNotice

    store = make_store()
    build_info = BuildInfo(
        job=context.job,
        buildNumber=5072,
        result="FAILURE",
        buildUrl=context.build_url,
        branch=context.branch,
        commit=context.head_commit,
    )
    ci_notice = CiResponsibilityNotice.model_validate(payload)
    chunks = [focused_chunk("FAIL getJsSdkConfig dingtalk ua\nError: UNKNOWN")]
    store.save_analysis(build_info, ci_notice, context.base_commit, context.head_commit, 5068, context.base_commit, chunks)
    store.save_analysis(build_info, ci_notice, context.base_commit, context.head_commit, 5068, context.base_commit, chunks)

    assert len(store.builds.docs) == 1
    assert len(store.notices.docs) == 1
    assert len(store.failure_chunks.docs) == 1
    assert store.failure_chunks.docs[0]["normalizedChunk"]
    assert store.failure_chunks.docs[0]["schemaVersion"] == 3
    assert store.failure_chunks.docs[0]["chunkSource"] == "local_test_failure_summary"
    assert store.failure_chunks.docs[0]["stageName"] == "Test"
    assert store.failure_chunks.docs[0]["stepName"] == "make docker-test"
    assert store.failure_chunks.docs[0]["anchorType"] == "mocha_failure_block"
    assert store.failure_chunks.docs[0]["startLine"] == 10
    assert store.failure_chunks.docs[0]["endLine"] == 20
    assert store.failure_chunks.docs[0]["score"] == 1.0
    assert store.failure_chunks.docs[0]["signatureHash"]
    assert store.notices.docs[0]["responsibilityItemCount"] == 0
    assert store.notices.docs[0]["inheritedOwnerCount"] == 0
    assert store.notices.docs[0]["currentBuildOwnerCount"] == 0


def test_mongo_history_store_does_not_save_console_tail_fallback(repo_cache, sample_repo, logs):
    context = make_lc_context(repo_cache, sample_repo, logs)
    from ci_owner_agent.schemas import CiResponsibilityNotice

    store = make_store()
    build_info = BuildInfo(job=context.job, buildNumber=5072, result="FAILURE", buildUrl=context.build_url, branch=context.branch, commit=context.head_commit)
    notice = CiResponsibilityNotice.model_validate(high_confidence_payload(context))
    fallback = focused_chunk("console fallback", source="local_console_tail_fallback")
    result = store.save_analysis(build_info, notice, context.base_commit, context.head_commit, 5068, context.base_commit, [fallback])
    assert result["chunksSaved"] == 0
    assert store.failure_chunks.docs == []


def test_save_analysis_empty_chunks_deletes_old_fatal_chunks(repo_cache, sample_repo, logs):
    context = make_lc_context(repo_cache, sample_repo, logs)
    from ci_owner_agent.schemas import CiResponsibilityNotice

    store = make_store()
    build_info = BuildInfo(job=context.job, buildNumber=7, result="FAILURE", buildUrl=context.build_url, branch=context.branch, commit=context.head_commit)
    notice = CiResponsibilityNotice.model_validate(high_confidence_payload(context))
    old_chunk = focused_chunk("ERROR: process wrapper")
    old_chunk["anchorType"] = "fatal_error_block"
    old_chunk["signature"] = {
        "testName": "test initialization",
        "testCase": "fatal error",
        "errorType": "Error",
        "errorMessage": "fatal error",
        "testFile": "",
        "topStackFile": "",
        "businessStackFiles": [],
        "signatureKey": "fatal|fatal error|fatal error||",
    }
    old_chunk["signatureHash"] = "old-bad-hash"

    store.save_analysis(build_info, notice, context.base_commit, context.head_commit, 6, context.base_commit, [old_chunk])
    assert len(store.failure_chunks.docs) == 1
    assert store.failure_chunks.docs[0]["anchorType"] == "fatal_error_block"

    store.save_analysis(build_info, notice, context.base_commit, context.head_commit, 6, context.base_commit, [])

    assert store.failure_chunks.docs == []


def test_non_structured_build_failures_without_summaries_do_not_inherit_owner(repo_cache, sample_repo, logs):
    context = make_lc_context(repo_cache, sample_repo, logs)
    from ci_owner_agent.schemas import CiResponsibilityNotice

    settings = replace(context.settings, history_enabled=True)
    store = make_store()
    notice = CiResponsibilityNotice.model_validate(high_confidence_payload(context))
    build_7 = BuildInfo(job=context.job, buildNumber=7, result="FAILURE", buildUrl="local://job/7", branch=context.branch, commit=context.head_commit)
    store.save_analysis(build_7, notice, context.base_commit, context.head_commit, 6, context.base_commit, [])

    build_8_context = replace(
        context,
        settings=settings,
        build_number=8,
        last_successful_build_number=6,
        failure_summaries={"chunks": []},
    )
    result = history_search_similar_failures(build_8_context, store=store)

    assert result["currentChunks"] == []
    assert result["candidates"] == []


def test_mongo_history_store_queries_by_last_successful_build(repo_cache, sample_repo, logs):
    context = make_lc_context(repo_cache, sample_repo, logs)
    from ci_owner_agent.schemas import CiResponsibilityNotice

    store = make_store()
    notice = CiResponsibilityNotice.model_validate(high_confidence_payload(context))
    for build in [5067, 5072, 5075]:
        build_info = BuildInfo(job=context.job, buildNumber=build, result="FAILURE", buildUrl=context.build_url, branch=context.branch, commit=context.head_commit)
        store.save_analysis(build_info, notice, context.base_commit, context.head_commit, 5068, context.base_commit, [focused_chunk(f"FAIL getJsSdkConfig {build}")])
    chunks = store.find_historical_failure_chunks(context.job, context.branch, current_build_number=5076, last_successful_build_number=5068)
    assert {item["buildNumber"] for item in chunks} == {5072, 5075}


def test_history_store_find_previous_build(repo_cache, sample_repo, logs):
    context = make_lc_context(repo_cache, sample_repo, logs)
    from ci_owner_agent.schemas import CiResponsibilityNotice

    store = make_store()
    notice = CiResponsibilityNotice.model_validate(high_confidence_payload(context))
    old_build = BuildInfo(job=context.job, buildNumber=5086, result="SUCCESS", buildUrl="local://job/5086", branch="dev", commit="old-head")
    previous_build = BuildInfo(job=context.job, buildNumber=5087, result="FAILURE", buildUrl="local://job/5087", branch="dev", commit="previous-head")
    other_branch_build = BuildInfo(job=context.job, buildNumber=5088, result="FAILURE", buildUrl="local://job/5088", branch="feature", commit="feature-head")
    store.save_analysis(old_build, notice, context.base_commit, "old-head", 5086, context.base_commit, [])
    store.save_analysis(previous_build, notice, context.base_commit, "previous-head", 5086, context.base_commit, [])
    store.save_analysis(other_branch_build, notice, context.base_commit, "feature-head", 5086, context.base_commit, [])

    result = store.find_previous_build(job=context.job, branch="dev", current_build_number=5088)

    assert result["buildNumber"] == 5087
    assert result["headCommit"] == "previous-head"


def test_mongo_history_store_notice_query_filters_branch(repo_cache, sample_repo, logs):
    context = make_lc_context(repo_cache, sample_repo, logs)
    from ci_owner_agent.schemas import CiResponsibilityNotice

    store = make_store()
    dev_notice = CiResponsibilityNotice.model_validate(high_confidence_payload(context))
    other_payload = high_confidence_payload(context)
    other_payload["owner"]["name"] = "Other Branch Owner"
    other_notice = CiResponsibilityNotice.model_validate(other_payload)

    dev_build = BuildInfo(job=context.job, buildNumber=5075, result="FAILURE", buildUrl=context.build_url, branch="dev", commit=context.head_commit)
    other_build = BuildInfo(job=context.job, buildNumber=5075, result="FAILURE", buildUrl=context.build_url, branch="feature", commit=context.head_commit)
    store.save_analysis(dev_build, dev_notice, context.base_commit, context.head_commit, 5068, context.base_commit, [focused_chunk("FAIL dev branch")])
    store.save_analysis(other_build, other_notice, context.base_commit, context.head_commit, 5068, context.base_commit, [focused_chunk("FAIL feature branch")])

    chunks = store.find_historical_failure_chunks(context.job, "dev", current_build_number=5076, last_successful_build_number=5068)
    assert {item["branch"] for item in chunks} == {"dev"}
    assert all(item["noticeDoc"]["ownerName"] != "Other Branch Owner" for item in chunks)


def test_mongo_history_store_replaces_old_chunks_when_new_run_has_fewer(repo_cache, sample_repo, logs):
    context = make_lc_context(repo_cache, sample_repo, logs)
    from ci_owner_agent.schemas import CiResponsibilityNotice

    store = make_store()
    notice = CiResponsibilityNotice.model_validate(high_confidence_payload(context))
    build_info = BuildInfo(job=context.job, buildNumber=5076, result="FAILURE", buildUrl=context.build_url, branch=context.branch, commit=context.head_commit)
    first_chunks = [focused_chunk(f"old chunk {idx}") for idx in range(5)]
    second_chunks = [focused_chunk(f"new chunk {idx}") for idx in range(2)]
    store.save_analysis(build_info, notice, context.base_commit, context.head_commit, 5068, context.base_commit, first_chunks)
    assert len(store.failure_chunks.docs) == 5
    store.save_analysis(build_info, notice, context.base_commit, context.head_commit, 5068, context.base_commit, second_chunks)
    assert len(store.failure_chunks.docs) == 2
    assert {doc["chunkText"] for doc in store.failure_chunks.docs} == {"new chunk 0", "new chunk 1"}


def test_history_search_similar_failures_disabled(repo_cache, sample_repo, logs):
    context = make_lc_context(repo_cache, sample_repo, logs)
    settings = replace(context.settings, history_enabled=False)
    context = replace(context, settings=settings)
    result = history_search_similar_failures(context)
    assert result["ok"] is False
    assert result["historyEnabled"] is False


def test_history_search_similar_failures_returns_likely_match(repo_cache, sample_repo, logs):
    context = make_lc_context(repo_cache, sample_repo, logs)
    settings = replace(context.settings, history_enabled=True)
    current_chunk = "FAIL getJsSdkConfig dingtalk ua\nError: UNKNOWN\nExpected: dingtalk\nActual: unknown"
    context = replace(
        context,
        settings=settings,
        build_number=5076,
        last_successful_build_number=5068,
        log_provider=FocusedProvider(current_chunk),
    )
    store = make_store()
    from ci_owner_agent.schemas import CiResponsibilityNotice

    notice = CiResponsibilityNotice.model_validate(high_confidence_payload(context))
    build_info = BuildInfo(job=context.job, buildNumber=5075, result="FAILURE", buildUrl=context.build_url, branch=context.branch, commit=context.head_commit)
    store.save_analysis(build_info, notice, context.base_commit, context.head_commit, 5068, context.base_commit, [focused_chunk(current_chunk)])
    result = history_search_similar_failures(context, store=store)
    assert result["ok"] is True
    assert result["candidates"][0]["relationship"] == "very_likely_same_failure"
    assert result["candidates"][0]["similarity"] == 1.0
    assert result["candidates"][0]["matchType"] == "signature_exact"
    assert result["candidates"][0]["buildNumber"] == 5075
    assert result["currentChunks"][0]["inheritedOwner"]["found"] is True
    assert result["currentChunks"][0]["inheritedOwner"]["sourceBuildNumber"] == 5075


def test_history_search_similar_failures_uses_context_failure_summaries(repo_cache, sample_repo, logs):
    context = make_lc_context(repo_cache, sample_repo, logs)
    settings = replace(context.settings, history_enabled=True)
    current_chunk = "FAIL getJsSdkConfig dingtalk ua\nError: UNKNOWN\nExpected: dingtalk\nActual: unknown"
    context = replace(
        context,
        settings=settings,
        build_number=5076,
        last_successful_build_number=5068,
        log_provider=ExplodingProvider(),
        failure_summaries={"chunks": [focused_chunk(current_chunk)]},
    )
    store = make_store()
    from ci_owner_agent.schemas import CiResponsibilityNotice

    notice = CiResponsibilityNotice.model_validate(high_confidence_payload(context))
    build_info = BuildInfo(job=context.job, buildNumber=5075, result="FAILURE", buildUrl=context.build_url, branch=context.branch, commit=context.head_commit)
    store.save_analysis(build_info, notice, context.base_commit, context.head_commit, 5068, context.base_commit, [focused_chunk(current_chunk)])
    result = history_search_similar_failures(context, store=store)
    assert result["ok"] is True
    assert result["candidates"][0]["matchType"] == "signature_exact"


def test_history_search_traces_inherited_owner_back_to_first_high_confidence(repo_cache, sample_repo, logs):
    context = make_lc_context(repo_cache, sample_repo, logs)
    settings = replace(context.settings, history_enabled=True)
    current_chunk = "FAIL getJsSdkConfig dingtalk ua\nError: UNKNOWN\nExpected: dingtalk\nActual: unknown"
    context = replace(
        context,
        settings=settings,
        build_number=5106,
        last_successful_build_number=5103,
        log_provider=FocusedProvider(current_chunk),
    )
    store = make_store()
    from ci_owner_agent.schemas import CiResponsibilityNotice

    first_payload = high_confidence_payload(context)
    first_payload["owner"]["name"] = "Tang.Tangerine-唐嘉伟"
    first_payload["owner"]["email"] = "tang@example.com"
    first_payload["owner"]["commit"] = "ab286e5"
    first_notice = CiResponsibilityNotice.model_validate(first_payload)
    inherited_notice = CiResponsibilityNotice.model_validate(inherited_notice_payload(context, source_build=5104))
    build_5104 = BuildInfo(job=context.job, buildNumber=5104, result="FAILURE", buildUrl="local://services/fx-code-unittest/5104", branch=context.branch, commit=context.head_commit)
    build_5105 = BuildInfo(job=context.job, buildNumber=5105, result="FAILURE", buildUrl="local://services/fx-code-unittest/5105", branch=context.branch, commit=context.head_commit)
    store.save_analysis(build_5104, first_notice, context.base_commit, context.head_commit, 5103, context.base_commit, [focused_chunk(current_chunk)])
    store.save_analysis(build_5105, inherited_notice, context.base_commit, context.head_commit, 5103, context.base_commit, [focused_chunk(current_chunk)])

    result = history_search_similar_failures(context, store=store)
    assert result["ok"] is True
    assert [item["buildNumber"] for item in result["candidates"][:2]] == [5105, 5104]
    inherited_owner = result["currentChunks"][0]["inheritedOwner"]
    assert inherited_owner["found"] is True
    assert inherited_owner["sourceBuildNumber"] == 5104
    assert inherited_owner["ownerName"] == "Tang.Tangerine-唐嘉伟"
    assert inherited_owner["ownerType"] == "high_confidence"
    assert result["candidates"][0]["inheritedOwner"]["sourceBuildNumber"] == 5104


def test_history_search_does_not_inherit_unrelated_owner_from_multi_failure_notice(repo_cache, sample_repo, logs):
    context = make_lc_context(repo_cache, sample_repo, logs)
    settings = replace(context.settings, history_enabled=True)
    view_signature = {
        "testName": "ViewDataQueryServiceTest",
        "testCase": "view data query",
        "errorType": "AssertionError",
        "errorMessage": "view data failed",
        "testFile": "test/view-data.test.ts",
        "topStackFile": "server/view-data.ts",
        "businessStackFiles": ["test/view-data.test.ts"],
        "signatureKey": "sig-view-data",
    }
    quota_signature = {
        "testName": "QuotaServiceTest",
        "testCase": "quota exceeded",
        "errorType": "AssertionError",
        "errorMessage": "quota failed",
        "testFile": "test/quota.test.ts",
        "topStackFile": "server/quota.ts",
        "businessStackFiles": ["test/quota.test.ts"],
        "signatureKey": "sig-quota",
    }
    view_chunk = focused_chunk("FAIL ViewDataQueryServiceTest\nAssertionError: view data failed", signature=view_signature)
    quota_chunk = focused_chunk("FAIL QuotaServiceTest\nAssertionError: quota failed", signature=quota_signature)
    context = replace(
        context,
        settings=settings,
        build_number=5112,
        last_successful_build_number=5103,
        failure_summaries={"chunks": [view_chunk]},
    )
    store = make_store()
    from ci_owner_agent.schemas import CiResponsibilityNotice

    tang_payload = high_confidence_payload(context)
    tang_payload["owner"] = {
        "type": "high_confidence",
        "name": "Tang.Tangerine-唐嘉伟",
        "email": "tang@example.com",
        "commit": "ab286e5",
        "confidence": 0.9,
    }
    tang_payload["responsibilityItems"] = [
        current_owner_item(
            failure_id="F1",
            failure_title="ViewDataQueryServiceTest",
            failure_signature="sig-view-data",
            owner_name="Tang.Tangerine-唐嘉伟",
            owner_email="tang@example.com",
            owner_commit="ab286e5",
        )
    ]
    tang_notice = CiResponsibilityNotice.model_validate(tang_payload)

    multi_payload = high_confidence_payload(context)
    multi_payload["owner"] = {
        "type": "no_high_confidence_owner",
        "name": "无高可信责任人",
        "email": None,
        "commit": None,
        "confidence": 0,
    }
    multi_payload["hasHighConfidenceOwner"] = False
    multi_payload["responsibilityItems"] = [
        inherited_owner_item(
            failure_id="F1",
            failure_title="ViewDataQueryServiceTest",
            failure_signature="sig-view-data",
            owner_name="Tang.Tangerine-唐嘉伟",
            source_build=5104,
            source_commit="ab286e5",
        ),
        current_owner_item(
            failure_id="F2",
            failure_title="QuotaServiceTest",
            failure_signature="sig-quota",
            owner_name="Li Si",
            owner_email="lisi@example.com",
            owner_commit="f" * 40,
        ),
    ]
    multi_notice = CiResponsibilityNotice.model_validate(multi_payload)
    build_5104 = BuildInfo(job=context.job, buildNumber=5104, result="FAILURE", buildUrl="local://services/fx-code-unittest/5104", branch=context.branch, commit="ab286e5")
    build_5111 = BuildInfo(job=context.job, buildNumber=5111, result="FAILURE", buildUrl="local://services/fx-code-unittest/5111", branch=context.branch, commit=context.head_commit)
    store.save_analysis(build_5104, tang_notice, context.base_commit, "ab286e5", 5103, context.base_commit, [view_chunk])
    store.save_analysis(build_5111, multi_notice, context.base_commit, context.head_commit, 5103, context.base_commit, [view_chunk, quota_chunk])

    result = history_search_similar_failures(context, store=store)
    assert result["ok"] is True
    inherited_owner = result["currentChunks"][0]["inheritedOwner"]
    assert inherited_owner["found"] is True
    assert inherited_owner["sourceBuildNumber"] == 5104
    assert inherited_owner["ownerName"] == "Tang.Tangerine-唐嘉伟"
    assert inherited_owner["ownerName"] != "Li Si"
    matched_5111 = next(candidate for candidate in result["candidates"] if candidate["buildNumber"] == 5111)
    assert matched_5111["historicalSignature"]["signatureKey"] == "sig-view-data"
    assert matched_5111["historicalSignatureHash"] == view_chunk["signatureHash"]
    assert matched_5111["inheritedOwner"]["ownerName"] == "Tang.Tangerine-唐嘉伟"
    assert "Li Si" not in str(matched_5111["inheritedOwner"])


def test_history_no_owner_decision_item_detected():
    assert is_no_owner_decision_item(no_owner_item(failure_id="F1", failure_title="Timeout", failure_signature="sig-timeout"))


def test_no_owner_decision_does_not_create_inherited_owner(repo_cache, sample_repo, logs):
    context = make_lc_context(repo_cache, sample_repo, logs)
    settings = replace(context.settings, history_enabled=True)
    chunk = focused_chunk("FAIL Timeout\nRun AwaitFunc Timeout")
    signature = chunk["signature"]["signatureKey"]
    context = replace(context, settings=settings, build_number=5089, last_successful_build_number=5087, failure_summaries={"chunks": [chunk]})
    store = make_store()
    from ci_owner_agent.schemas import CiResponsibilityNotice

    payload = high_confidence_payload(context)
    payload["owner"] = {"type": "no_high_confidence_owner", "name": "无高可信责任人", "email": None, "commit": None, "confidence": 0}
    payload["hasHighConfidenceOwner"] = False
    payload["responsibilityItems"] = [no_owner_item(failure_id="F1", failure_title="Timeout", failure_signature=signature)]
    notice = CiResponsibilityNotice.model_validate(payload)
    build_info = BuildInfo(job=context.job, buildNumber=5088, result="FAILURE", buildUrl="local://job/5088", branch=context.branch, commit=context.head_commit)
    store.save_analysis(build_info, notice, context.base_commit, context.head_commit, 5087, context.base_commit, [chunk])

    result = history_search_similar_failures(context, store=store)

    chunk_result = result["currentChunks"][0]
    assert chunk_result["inheritedOwner"]["found"] is False
    assert chunk_result["noOwnerDecision"]["found"] is True
    assert chunk_result["noOwnerDecision"]["sourceBuildNumber"] == 5088


def test_mark_flaky_blocks_owner_and_returns_no_owner_decision(repo_cache, sample_repo, logs):
    context = make_lc_context(repo_cache, sample_repo, logs)
    settings = replace(context.settings, history_enabled=True)
    chunk = focused_chunk("FAIL Timeout\nRun AwaitFunc Timeout")
    signature = chunk["signature"]["signatureKey"]
    context = replace(context, settings=settings, build_number=5089, last_successful_build_number=5087, failure_summaries={"chunks": [chunk]})
    store = make_store()
    from ci_owner_agent.schemas import CiResponsibilityNotice

    payload = high_confidence_payload(context)
    payload["responsibilityItems"] = [
        current_owner_item(
            failure_id="F1",
            failure_title="Timeout",
            failure_signature=signature,
            owner_name="Charlie.Guo",
            owner_email="charlie@example.com",
            owner_commit=context.head_commit,
        )
    ]
    notice = CiResponsibilityNotice.model_validate(payload)
    build_info = BuildInfo(job=context.job, buildNumber=5088, result="FAILURE", buildUrl="local://job/5088", branch=context.branch, commit=context.head_commit)
    store.save_analysis(build_info, notice, context.base_commit, context.head_commit, 5087, context.base_commit, [chunk])
    store.feedback.docs.append(
        {
            "job": context.job,
            "branch": context.branch,
            "buildNumber": 5088,
            "failureSignature": signature,
            "action": "mark_flaky",
            "isActive": True,
            "updatedAt": "2026-01-02T00:00:00",
        }
    )

    result = history_search_similar_failures(context, store=store)

    chunk_result = result["currentChunks"][0]
    assert chunk_result["inheritedOwner"]["found"] is False
    assert chunk_result["noOwnerDecision"]["found"] is True
    assert chunk_result["noOwnerDecision"]["feedbackAction"] == "mark_flaky"


def test_correct_owner_has_priority_over_no_owner(repo_cache, sample_repo, logs):
    context = make_lc_context(repo_cache, sample_repo, logs)
    settings = replace(context.settings, history_enabled=True)
    chunk = focused_chunk("FAIL Timeout\nRun AwaitFunc Timeout")
    signature = chunk["signature"]["signatureKey"]
    context = replace(context, settings=settings, build_number=5089, last_successful_build_number=5087, failure_summaries={"chunks": [chunk]})
    store = make_store()
    from ci_owner_agent.schemas import CiResponsibilityNotice

    payload = high_confidence_payload(context)
    payload["owner"] = {"type": "no_high_confidence_owner", "name": "无高可信责任人", "email": None, "commit": None, "confidence": 0}
    payload["hasHighConfidenceOwner"] = False
    payload["responsibilityItems"] = [no_owner_item(failure_id="F1", failure_title="Timeout", failure_signature=signature)]
    notice = CiResponsibilityNotice.model_validate(payload)
    build_info = BuildInfo(job=context.job, buildNumber=5088, result="FAILURE", buildUrl="local://job/5088", branch=context.branch, commit=context.head_commit)
    store.save_analysis(build_info, notice, context.base_commit, context.head_commit, 5087, context.base_commit, [chunk])
    store.feedback.docs.append(
        {
            "job": context.job,
            "branch": context.branch,
            "buildNumber": 5088,
            "failureSignature": signature,
            "action": "correct_owner",
            "correctedOwner": {"type": "high_confidence", "name": "Alice", "email": "alice@example.com", "commit": "abc", "confidence": 1},
            "sourceBuildNumber": 5088,
            "isActive": True,
            "updatedAt": "2026-01-02T00:00:00",
        }
    )

    result = history_search_similar_failures(context, store=store)

    chunk_result = result["currentChunks"][0]
    assert chunk_result["inheritedOwner"]["found"] is True
    assert chunk_result["inheritedOwner"]["ownerName"] == "Alice"
    assert chunk_result["noOwnerDecision"]["found"] is False


def test_history_search_signature_structural_match(repo_cache, sample_repo, logs):
    context = make_lc_context(repo_cache, sample_repo, logs)
    settings = replace(context.settings, history_enabled=True)
    current_chunk = "FAIL getJsSdkConfig dingtalk ua\nError: UNKNOWN\nExpected: dingtalk\nActual: unknown"
    context = replace(
        context,
        settings=settings,
        build_number=5076,
        last_successful_build_number=5068,
        log_provider=FocusedProvider(current_chunk),
    )
    store = make_store()
    from ci_owner_agent.schemas import CiResponsibilityNotice

    notice = CiResponsibilityNotice.model_validate(high_confidence_payload(context))
    build_info = BuildInfo(job=context.job, buildNumber=5075, result="FAILURE", buildUrl=context.build_url, branch=context.branch, commit=context.head_commit)
    store.save_analysis(build_info, notice, context.base_commit, context.head_commit, 5068, context.base_commit, [focused_chunk(current_chunk + "\nnoise")])
    store.failure_chunks.docs[0]["signatureHash"] = "different-hash"
    result = history_search_similar_failures(context, store=store)
    assert result["candidates"][0]["relationship"] == "very_likely_same_failure"
    assert result["candidates"][0]["matchType"] == "signature_structural"


def test_history_search_candidates_sort_by_similarity_then_recent_build(repo_cache, sample_repo, logs):
    context = make_lc_context(repo_cache, sample_repo, logs)
    settings = replace(context.settings, history_enabled=True)
    current_chunk = "FAIL getJsSdkConfig dingtalk ua\nError: UNKNOWN\nExpected: dingtalk\nActual: unknown"
    context = replace(
        context,
        settings=settings,
        build_number=5112,
        last_successful_build_number=5103,
        log_provider=FocusedProvider(current_chunk),
    )
    store = make_store()
    from ci_owner_agent.schemas import CiResponsibilityNotice

    notice = CiResponsibilityNotice.model_validate(high_confidence_payload(context))
    for build in [5104, 5111, 5108]:
        build_info = BuildInfo(job=context.job, buildNumber=build, result="FAILURE", buildUrl=context.build_url, branch=context.branch, commit=context.head_commit)
        store.save_analysis(build_info, notice, context.base_commit, context.head_commit, 5103, context.base_commit, [focused_chunk(current_chunk)])
    lower_build = BuildInfo(job=context.job, buildNumber=5110, result="FAILURE", buildUrl=context.build_url, branch=context.branch, commit=context.head_commit)
    lower_signature = dict(focused_chunk(current_chunk)["signature"])
    lower_signature["signatureKey"] += "|different_root_cause"
    lower_chunk = focused_chunk(current_chunk + "\nextra", signature=lower_signature)
    store.save_analysis(lower_build, notice, context.base_commit, context.head_commit, 5103, context.base_commit, [lower_chunk])

    result = history_search_similar_failures(context, maxCandidates=4, store=store)
    assert [item["buildNumber"] for item in result["candidates"][:3]] == [5111, 5108, 5104]
    assert result["candidates"][3]["buildNumber"] == 5110


def test_history_search_ignores_legacy_and_fallback_chunks(repo_cache, sample_repo, logs):
    context = make_lc_context(repo_cache, sample_repo, logs)
    settings = replace(context.settings, history_enabled=True)
    current_chunk = "FAIL getJsSdkConfig dingtalk ua\nError: UNKNOWN\nExpected: dingtalk\nActual: unknown"
    context = replace(
        context,
        settings=settings,
        build_number=5076,
        last_successful_build_number=5068,
        log_provider=FocusedProvider(current_chunk),
    )
    store = make_store()
    from ci_owner_agent.schemas import CiResponsibilityNotice

    notice = CiResponsibilityNotice.model_validate(high_confidence_payload(context))
    build_info = BuildInfo(job=context.job, buildNumber=5075, result="FAILURE", buildUrl=context.build_url, branch=context.branch, commit=context.head_commit)
    store.save_analysis(build_info, notice, context.base_commit, context.head_commit, 5068, context.base_commit, [focused_chunk(current_chunk)])
    key = {"job": context.job, "branch": context.branch, "buildNumber": 5074}
    store.builds.update_one(key, {"$set": {**key, "result": "FAILURE", "headCommit": context.head_commit}}, upsert=True)
    store.failure_chunks.docs.append({**key, "chunkIndex": 0, "chunkText": current_chunk})
    store.failure_chunks.docs.append({**key, "chunkIndex": 1, "schemaVersion": 2, "chunkSource": "local_test_stage_tail", "chunkText": current_chunk})
    store.failure_chunks.docs.append({**key, "chunkIndex": 2, "schemaVersion": 3, "chunkSource": "error_window_fallback", "chunkText": current_chunk})

    result = history_search_similar_failures(context, store=store)
    assert [item["buildNumber"] for item in result["candidates"]] == [5075]
    assert result["candidates"][0]["matchType"] == "signature_exact"


def test_history_search_similar_failures_lookback_when_last_success_missing(repo_cache, sample_repo, logs):
    context = make_lc_context(repo_cache, sample_repo, logs)
    settings = replace(context.settings, history_enabled=True)
    context = replace(context, settings=settings, build_number=5076, last_successful_build_number=None)
    store = make_store()
    result = history_search_similar_failures(context, store=store)
    assert result["ok"] is True
    assert result["lastSuccessfulBuildNumberMissing"] is True


def test_save_notification_preserves_created_at(repo_cache, sample_repo, logs):
    context = make_lc_context(repo_cache, sample_repo, logs)
    store = make_store()
    from ci_owner_agent.schemas import CiResponsibilityNotice

    notice = CiResponsibilityNotice.model_validate(high_confidence_payload(context))
    digest = notice_hash(notice)
    store.save_notification(notice=notice, notice_hash=digest, channel="wecom", status="sent", message="first")
    first_doc = dict(store.notifications.docs[0])
    store.save_notification(notice=notice, notice_hash=digest, channel="wecom", status="failed", message="second", error="boom")
    second_doc = store.notifications.docs[0]

    assert second_doc["createdAt"] == first_doc["createdAt"]
    assert second_doc["updatedAt"] != first_doc["updatedAt"]
    assert second_doc["status"] == "failed"


def test_save_failure_facts_insert_and_delete(repo_cache, sample_repo, logs):
    context = make_lc_context(repo_cache, sample_repo, logs)
    from ci_owner_agent.schemas import CiResponsibilityNotice

    store = make_store()
    notice = CiResponsibilityNotice.model_validate(high_confidence_payload(context))
    build_info = BuildInfo(job=context.job, buildNumber=5099, result="FAILURE", buildUrl=context.build_url, branch=context.branch, commit=context.head_commit)
    fact = FailureFact(
        signatureKey="typescript_compile_error|TS2305|packages/fxp-ai/src/index.ts|classifyErrorMessage",
        historyEligible=True,
        failureKind="typescript_compile_error",
        errorCode="TS2305",
        message="Module './errors' has no exported member 'classifyErrorMessage'",
        rootCauseSummary="missing export",
        confidence=0.9,
    )

    result = store.save_failure_facts(build_info=build_info, notice=notice, facts=[fact])
    assert result["factsSaved"] == 1
    assert len(store.failure_facts.docs) == 1
    assert store.failure_facts.docs[0]["factId"].startswith("fact-")

    result = store.save_failure_facts(build_info=build_info, notice=notice, facts=[])
    assert result["factsSaved"] == 0
    assert store.failure_facts.docs == []


def test_save_failure_facts_stores_canonical_var_app_path(repo_cache, sample_repo, logs):
    context = make_lc_context(repo_cache, sample_repo, logs)
    from ci_owner_agent.schemas import CiResponsibilityNotice

    store = make_store()
    notice = CiResponsibilityNotice.model_validate(high_confidence_payload(context))
    build_info = BuildInfo(job=context.job, buildNumber=5099, result="FAILURE", buildUrl=context.build_url, branch=context.branch, commit=context.head_commit)
    fact = FailureFact(
        signatureKey="model-path",
        historyEligible=True,
        failureKind="typescript_compile_error",
        errorCode="TS2305",
        filePath="/var/app/server/workflow/service.ts",
        symbol="classifyErrorMessage",
        message="missing export",
        rootCauseSummary="missing export",
        confidence=0.9,
    )

    store.save_failure_facts(build_info=build_info, notice=notice, facts=[fact])

    saved = store.failure_facts.docs[0]
    assert saved["fact"]["filePath"] == "server/workflow/service.ts"
    assert "/var/app" not in str(saved)
    assert "server/workflow/service.ts" in saved["signatureKey"]


def test_save_failure_facts_drops_unsupported_absolute_path(repo_cache, sample_repo, logs):
    context = make_lc_context(repo_cache, sample_repo, logs)
    from ci_owner_agent.schemas import CiResponsibilityNotice

    store = make_store()
    notice = CiResponsibilityNotice.model_validate(high_confidence_payload(context))
    build_info = BuildInfo(job=context.job, buildNumber=5099, result="FAILURE", buildUrl=context.build_url, branch=context.branch, commit=context.head_commit)
    fact = FailureFact(
        signatureKey="model-wrapper",
        historyEligible=True,
        failureKind="build_failure",
        filePath="/var/lib/jenkins/workspace/services/fx-code-unittest/server/a.ts",
        message='ERROR: process "/bin/sh -c npm run build" did not complete successfully: exit code: 1',
        rootCauseSummary="command failed",
        confidence=0.9,
    )

    store.save_failure_facts(build_info=build_info, notice=notice, facts=[fact])

    saved = store.failure_facts.docs[0]
    assert saved["fact"]["filePath"] is None
    assert saved["historyEligible"] is False


def test_save_failure_facts_uses_matching_responsibility_item_owner(repo_cache, sample_repo, logs):
    context = make_lc_context(repo_cache, sample_repo, logs)
    from ci_owner_agent.schemas import CiResponsibilityNotice

    signature = "npm_dependency_resolution_error|ETARGET|@ai-sdk/provider|99.0.0-nonexistent"
    payload = high_confidence_payload(context)
    payload["responsibilityItems"] = [
        current_owner_item(
            failure_id="auto",
            failure_title="npm ETARGET",
            failure_signature=signature,
            owner_name="Li Si",
            owner_email="lisi@example.com",
            owner_commit=context.head_commit,
        )
    ]
    notice = CiResponsibilityNotice.model_validate(payload)
    build_info = BuildInfo(job=context.job, buildNumber=5099, result="FAILURE", buildUrl=context.build_url, branch=context.branch, commit=context.head_commit)
    store = make_store()
    fact = FailureFact(
        signatureKey=signature,
        historyEligible=True,
        failureKind="npm_dependency_resolution_error",
        errorCode="ETARGET",
        packageName="@ai-sdk/provider",
        message="No matching version found",
        rootCauseSummary="bad dependency version",
        confidence=0.95,
    )

    store.save_failure_facts(build_info=build_info, notice=notice, facts=[fact])

    assert store.failure_facts.docs[0]["factOwner"]["name"] == "Li Si"
    assert store.failure_facts.docs[0]["factOwner"]["email"] == "lisi@example.com"


def test_active_feedback_prefers_newer_update_for_same_signature():
    store = make_store()
    store.feedback.docs.extend(
        [
            {
                "job": "services/fx-code-unittest",
                "branch": "dev",
                "buildNumber": 1,
                "failureSignature": "sig-1",
                "action": "correct_owner",
                "correctedOwner": {"name": "Old Owner", "type": "high_confidence", "confidence": 1},
                "isActive": True,
                "createdAt": "2026-01-01T00:00:00",
                "updatedAt": "2026-01-01T00:00:00",
            },
            {
                "job": "services/fx-code-unittest",
                "branch": "dev",
                "buildNumber": 1,
                "failureSignature": "sig-1",
                "action": "correct_owner",
                "correctedOwner": {"name": "New Owner", "type": "high_confidence", "confidence": 1},
                "isActive": True,
                "createdAt": "2026-01-01T00:00:00",
                "updatedAt": "2026-01-02T00:00:00",
            },
        ]
    )

    feedback = find_feedback_override_for_failure_signature(
        store,
        job="services/fx-code-unittest",
        branch="dev",
        build_number=2,
        failure_signature="sig-1",
        notice_doc={"notice": {}},
    )

    assert feedback["correctedOwner"]["name"] == "New Owner"


def _save_fact_build(store, context, *, build: int, branch: str | None = "dev", fact: FailureFact | None = None):
    from ci_owner_agent.schemas import CiResponsibilityNotice

    notice = CiResponsibilityNotice.model_validate(high_confidence_payload(context))
    build_info = BuildInfo(job=context.job, buildNumber=build, result="FAILURE", buildUrl=f"local://job/{build}", branch=branch, commit=context.head_commit)
    store.save_analysis(build_info, notice, context.base_commit, context.head_commit, 6, context.base_commit, [])
    store.save_failure_facts(
        build_info=build_info,
        notice=notice,
        facts=[
            fact
            or FailureFact(
                signatureKey=f"typescript_compile_error|TS2305|src/{build}.ts|classifyErrorMessage",
                historyEligible=True,
                isGenericWrapper=False,
                failureKind="typescript_compile_error",
                errorCode="TS2305",
                message="missing export",
                rootCauseSummary="missing export",
                confidence=0.95,
            )
        ],
    )


def test_find_historical_failure_facts_returns_saved_facts(repo_cache, sample_repo, logs):
    context = make_lc_context(repo_cache, sample_repo, logs)
    store = make_store()
    _save_fact_build(store, context, build=7)

    facts = store.find_historical_failure_facts(
        job=context.job,
        branch="dev",
        current_build_number=8,
        last_successful_build_number=6,
    )

    assert len(facts) == 1
    assert facts[0]["buildNumber"] == 7
    assert facts[0]["fact"]["errorCode"] == "TS2305"


def test_find_historical_failure_facts_respects_last_successful_build(repo_cache, sample_repo, logs):
    context = make_lc_context(repo_cache, sample_repo, logs)
    store = make_store()
    _save_fact_build(store, context, build=5)
    _save_fact_build(store, context, build=7)

    facts = store.find_historical_failure_facts(
        job=context.job,
        branch="dev",
        current_build_number=8,
        last_successful_build_number=6,
    )

    assert [item["buildNumber"] for item in facts] == [7]


def test_find_historical_failure_facts_excludes_current_and_future(repo_cache, sample_repo, logs):
    context = make_lc_context(repo_cache, sample_repo, logs)
    store = make_store()
    _save_fact_build(store, context, build=8)
    _save_fact_build(store, context, build=9)

    facts = store.find_historical_failure_facts(
        job=context.job,
        branch="dev",
        current_build_number=8,
        last_successful_build_number=6,
    )

    assert facts == []


def test_find_historical_failure_facts_filters_ineligible_and_generic(repo_cache, sample_repo, logs):
    context = make_lc_context(repo_cache, sample_repo, logs)
    store = make_store()
    _save_fact_build(store, context, build=5, fact=FailureFact(signatureKey="ineligible", historyEligible=False, failureKind="generic", message="x", rootCauseSummary="x", confidence=0.9))
    _save_fact_build(store, context, build=6, fact=FailureFact(signatureKey="generic", historyEligible=True, isGenericWrapper=True, failureKind="generic_wrapper", message="x", rootCauseSummary="x", confidence=0.9))
    _save_fact_build(store, context, build=7)

    facts = store.find_historical_failure_facts(
        job=context.job,
        branch="dev",
        current_build_number=8,
        last_successful_build_number=None,
    )

    assert [item["buildNumber"] for item in facts] == [7]


def test_find_historical_failure_facts_filters_branch(repo_cache, sample_repo, logs):
    context = make_lc_context(repo_cache, sample_repo, logs)
    store = make_store()
    _save_fact_build(store, context, build=5, branch="feature")
    _save_fact_build(store, context, build=6, branch=None)
    _save_fact_build(store, context, build=7, branch="dev")

    facts = store.find_historical_failure_facts(
        job=context.job,
        branch="dev",
        current_build_number=8,
        last_successful_build_number=None,
    )

    assert [item["buildNumber"] for item in facts] == [7, 6]


def test_find_historical_failure_facts_limits_max_facts(repo_cache, sample_repo, logs):
    context = make_lc_context(repo_cache, sample_repo, logs)
    store = make_store()
    for build in [5, 6, 7]:
        _save_fact_build(store, context, build=build)

    facts = store.find_historical_failure_facts(
        job=context.job,
        branch="dev",
        current_build_number=8,
        last_successful_build_number=None,
        max_facts=2,
    )

    assert [item["buildNumber"] for item in facts] == [7, 6]
from ci_owner_agent.schemas import BuildInfo, CiResponsibilityNotice
from tests.test_history_store import make_store
from tests.test_langchain_agent import high_confidence_payload, make_lc_context


def test_history_store_find_previous_build(repo_cache, sample_repo, logs):
    context = make_lc_context(repo_cache, sample_repo, logs)
    store = make_store()

    notice = CiResponsibilityNotice.model_validate(high_confidence_payload(context))

    # Save #5086 with headCommit="old"
    build_5086 = BuildInfo(
        job=context.job,
        buildNumber=5086,
        result="SUCCESS",
        buildUrl="local://job/5086",
        branch="dev",
        commit="old",
    )
    store.save_analysis(build_5086, notice, "base", "old", 5068, "base", [])

    # Save #5087 with headCommit="previous"
    build_5087 = BuildInfo(
        job=context.job,
        buildNumber=5087,
        result="FAILURE",
        buildUrl="local://job/5087",
        branch="dev",
        commit="previous",
    )
    store.save_analysis(build_5087, notice, "base", "previous", 5068, "base", [])

    # Save #5087 on branch "other" with headCommit="other-previous"
    build_5087_other = BuildInfo(
        job=context.job,
        buildNumber=5087,
        result="FAILURE",
        buildUrl="local://job/5087-other",
        branch="other",
        commit="other-previous",
    )
    store.save_analysis(build_5087_other, notice, "base", "other-previous", 5068, "base", [])

    # Query for branch="dev" should return #5087 with headCommit="previous"
    result = store.find_previous_build(
        job=context.job,
        branch="dev",
        current_build_number=5088,
    )
    assert result is not None
    assert result["buildNumber"] == 5087
    assert result["headCommit"] == "previous"


def test_history_store_find_previous_build_matches_branch_none(repo_cache, sample_repo, logs):
    context = make_lc_context(repo_cache, sample_repo, logs)
    store = make_store()

    notice = CiResponsibilityNotice.model_validate(high_confidence_payload(context))

    # Save #5086 with branch=None
    build_5086 = BuildInfo(
        job=context.job,
        buildNumber=5086,
        result="FAILURE",
        buildUrl="local://job/5086",
        branch=None,
        commit="previous",
    )
    store.save_analysis(build_5086, notice, "base", "previous", 5068, "base", [])

    # branch="dev" query should match branch=None builds
    result = store.find_previous_build(
        job=context.job,
        branch="dev",
        current_build_number=5087,
    )
    assert result is not None
    assert result["buildNumber"] == 5086
    assert result["headCommit"] == "previous"


def test_history_store_find_previous_build_skips_missing_head_commit(repo_cache, sample_repo, logs):
    context = make_lc_context(repo_cache, sample_repo, logs)
    store = make_store()

    notice = CiResponsibilityNotice.model_validate(high_confidence_payload(context))

    # #5087 with headCommit=None should be skipped
    store.builds.update_one(
        {"job": context.job, "branch": "dev", "buildNumber": 5087},
        {
            "$set": {
                "job": context.job,
                "branch": "dev",
                "buildNumber": 5087,
                "result": "FAILURE",
                "baseCommit": "base",
                "headCommit": None,
                "lastSuccessfulBuildNumber": 5068,
                "lastSuccessfulCommit": "base",
                "buildUrl": "local://job/5087",
                "createdAt": None,
            }
        },
        upsert=True,
    )

    # #5086 with headCommit="previous"
    build_5086 = BuildInfo(
        job=context.job,
        buildNumber=5086,
        result="FAILURE",
        buildUrl="local://job/5086",
        branch="dev",
        commit="previous",
    )
    store.save_analysis(build_5086, notice, "base", "previous", 5068, "base", [])

    result = store.find_previous_build(
        job=context.job,
        branch="dev",
        current_build_number=5088,
    )
    assert result is not None
    assert result["buildNumber"] == 5086
    assert result["headCommit"] == "previous"


def test_history_store_find_previous_build_returns_none_when_no_match(repo_cache, sample_repo, logs):
    context = make_lc_context(repo_cache, sample_repo, logs)
    store = make_store()

    result = store.find_previous_build(
        job=context.job,
        branch="dev",
        current_build_number=5088,
    )
    assert result is None
