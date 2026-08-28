from __future__ import annotations

import datetime as dt

from ci_owner_agent.services.wecom_mongo_user_mapping import MongoWeComUserMapper
from tests.fx_code_test.test_history_store import make_store


def _insert(store, **doc):
    store.wecom_users.update_one({"mappingKey": doc.get("mappingKey") or doc.get("authorName")}, {"$set": doc}, upsert=True)


def test_mongo_mapping_resolves_by_email():
    store = make_store()
    _insert(store, mappingKey="Tang <a@fanruan.com>", authorName="Tang", normalizedEmail="a@fanruan.com", wecomUserId="userid.a")

    entry = MongoWeComUserMapper(store).resolve_owner("Tang", "a@fanruan.com")

    assert entry is not None
    assert entry.wecom_userid == "userid.a"


def test_mongo_mapping_prefers_email_over_name():
    store = make_store()
    _insert(store, mappingKey="Tang <a@fanruan.com>", authorName="Tang", normalizedEmail="a@fanruan.com", wecomUserId="userid.a")
    _insert(store, mappingKey="Tang <b@fanruan.com>", authorName="Tang", normalizedEmail="b@fanruan.com", wecomUserId="userid.b")

    assert MongoWeComUserMapper(store).mention_owner("Tang", "b@fanruan.com") == "<@userid.b>"


def test_mongo_mapping_prefers_fanruan_email_and_commit_count():
    store = make_store()
    _insert(
        store,
        mappingKey="Tang <other@example.com>",
        authorName="Tang",
        normalizedEmail="other@example.com",
        emailDomain="example.com",
        wecomUserId="userid.other",
        commitCount=100,
        updatedAt=dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc),
    )
    _insert(
        store,
        mappingKey="Tang <small@fanruan.com>",
        authorName="Tang",
        normalizedEmail="small@fanruan.com",
        emailDomain="fanruan.com",
        wecomUserId="userid.small",
        commitCount=1,
        updatedAt=dt.datetime(2024, 1, 2, tzinfo=dt.timezone.utc),
    )
    _insert(
        store,
        mappingKey="Tang <big@fanruan.com>",
        authorName="Tang",
        normalizedEmail="big@fanruan.com",
        emailDomain="fanruan.com",
        wecomUserId="userid.big",
        commitCount=20,
        updatedAt=dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc),
    )

    assert MongoWeComUserMapper(store).mention_owner("Tang", None) == "<@userid.big>"


def test_mongo_mapping_mode_name_ignores_userid():
    store = make_store()
    _insert(store, mappingKey="Tang <a@fanruan.com>", authorName="Tang", normalizedEmail="a@fanruan.com", wecomUserId="userid.a")

    assert MongoWeComUserMapper(store).mention_owner("Tang", "a@fanruan.com", mode="name") == "@Tang"


def test_mongo_mapping_falls_back_to_name_when_missing():
    assert MongoWeComUserMapper(make_store()).mention_owner("Tang", "missing@fanruan.com") == "@Tang"
