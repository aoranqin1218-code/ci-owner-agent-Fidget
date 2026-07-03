from __future__ import annotations

from dataclasses import replace

from ci_owner_agent.schemas import BuildInfo
from ci_owner_agent.services.history_store import MongoHistoryStore
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
        if doc is None:
            doc = dict(key)
            self.docs.append(doc)
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
            if "$in" in expected and value not in expected["$in"]:
                return False
        elif value != expected:
            return False
    return True


def make_store():
    return MongoHistoryStore("mongodb://fake", "ci_owner_agent_test", client=FakeClient())


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
    chunks = [{"content": "FAIL getJsSdkConfig dingtalk ua\nError: UNKNOWN"}]
    store.save_analysis(build_info, ci_notice, context.base_commit, context.head_commit, 5068, context.base_commit, chunks)
    store.save_analysis(build_info, ci_notice, context.base_commit, context.head_commit, 5068, context.base_commit, chunks)

    assert len(store.builds.docs) == 1
    assert len(store.notices.docs) == 1
    assert len(store.failure_chunks.docs) == 1
    assert store.failure_chunks.docs[0]["normalizedChunk"]


def test_mongo_history_store_queries_by_last_successful_build(repo_cache, sample_repo, logs):
    context = make_lc_context(repo_cache, sample_repo, logs)
    from ci_owner_agent.schemas import CiResponsibilityNotice

    store = make_store()
    notice = CiResponsibilityNotice.model_validate(high_confidence_payload(context))
    for build in [5067, 5072, 5075]:
        build_info = BuildInfo(job=context.job, buildNumber=build, result="FAILURE", buildUrl=context.build_url, branch=context.branch, commit=context.head_commit)
        store.save_analysis(build_info, notice, context.base_commit, context.head_commit, 5068, context.base_commit, [{"content": f"FAIL getJsSdkConfig {build}"}])
    chunks = store.find_historical_failure_chunks(context.job, context.branch, current_build_number=5076, last_successful_build_number=5068)
    assert {item["buildNumber"] for item in chunks} == {5072, 5075}


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
    store.save_analysis(dev_build, dev_notice, context.base_commit, context.head_commit, 5068, context.base_commit, [{"content": "FAIL dev branch"}])
    store.save_analysis(other_build, other_notice, context.base_commit, context.head_commit, 5068, context.base_commit, [{"content": "FAIL feature branch"}])

    chunks = store.find_historical_failure_chunks(context.job, "dev", current_build_number=5076, last_successful_build_number=5068)
    assert {item["branch"] for item in chunks} == {"dev"}
    assert all(item["noticeDoc"]["ownerName"] != "Other Branch Owner" for item in chunks)


def test_mongo_history_store_replaces_old_chunks_when_new_run_has_fewer(repo_cache, sample_repo, logs):
    context = make_lc_context(repo_cache, sample_repo, logs)
    from ci_owner_agent.schemas import CiResponsibilityNotice

    store = make_store()
    notice = CiResponsibilityNotice.model_validate(high_confidence_payload(context))
    build_info = BuildInfo(job=context.job, buildNumber=5076, result="FAILURE", buildUrl=context.build_url, branch=context.branch, commit=context.head_commit)
    first_chunks = [{"content": f"old chunk {idx}"} for idx in range(5)]
    second_chunks = [{"content": f"new chunk {idx}"} for idx in range(2)]
    store.save_analysis(build_info, notice, context.base_commit, context.head_commit, 5068, context.base_commit, first_chunks)
    assert len(store.failure_chunks.docs) == 5
    store.save_analysis(build_info, notice, context.base_commit, context.head_commit, 5068, context.base_commit, second_chunks)
    assert len(store.failure_chunks.docs) == 2
    assert {doc["chunkText"] for doc in store.failure_chunks.docs} == {"new chunk 0", "new chunk 1"}


def test_history_search_similar_failures_disabled(repo_cache, sample_repo, logs):
    context = make_lc_context(repo_cache, sample_repo, logs)
    result = history_search_similar_failures(context)
    assert result["ok"] is False
    assert result["historyEnabled"] is False


def test_history_search_similar_failures_returns_likely_match(repo_cache, sample_repo, logs):
    context = make_lc_context(repo_cache, sample_repo, logs)
    settings = replace(context.settings, history_enabled=True)
    context = replace(context, settings=settings, build_number=5076, last_successful_build_number=5068)
    store = make_store()
    from ci_owner_agent.schemas import CiResponsibilityNotice

    notice = CiResponsibilityNotice.model_validate(high_confidence_payload(context))
    build_info = BuildInfo(job=context.job, buildNumber=5075, result="FAILURE", buildUrl=context.build_url, branch=context.branch, commit=context.head_commit)
    current_chunk = context.log_provider.find_error_chunks(200, 5)["chunks"][0]["content"]
    store.save_analysis(build_info, notice, context.base_commit, context.head_commit, 5068, context.base_commit, [{"content": current_chunk}])
    result = history_search_similar_failures(context, store=store)
    assert result["ok"] is True
    assert result["candidates"][0]["relationship"] == "very_likely_same_failure"
    assert result["candidates"][0]["buildNumber"] == 5075


def test_history_search_similar_failures_lookback_when_last_success_missing(repo_cache, sample_repo, logs):
    context = make_lc_context(repo_cache, sample_repo, logs)
    settings = replace(context.settings, history_enabled=True)
    context = replace(context, settings=settings, build_number=5076, last_successful_build_number=None)
    store = make_store()
    result = history_search_similar_failures(context, store=store)
    assert result["ok"] is True
    assert result["lastSuccessfulBuildNumberMissing"] is True
