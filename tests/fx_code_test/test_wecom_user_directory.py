from __future__ import annotations

from ci_owner_agent.services.wecom_user_directory import WeComUserDirectory
from tests.fx_code_test.test_history_store import make_store


def _insert(store, **doc):
    store.wecom_users.update_one({"mappingKey": doc.get("mappingKey") or doc.get("authorName")}, {"$set": doc}, upsert=True)


def test_search_users_groups_by_wecom_userid_and_prefers_company_email():
    store = make_store()
    _insert(
        store,
        mappingKey="tang-1",
        authorName="Tang",
        normalizedEmail="tang@gmail.com",
        wecomUserId="tang.userid",
        commitCount=3,
        searchText="tang tang@gmail.com tang.userid",
    )
    _insert(
        store,
        mappingKey="tang-2",
        authorName="Tang.Tangerine-唐嘉伟",
        normalizedEmail="tang@fanruan.com",
        wecomUserId="tang.userid",
        commitCount=10,
        searchText="tang.tangerine 唐嘉伟 tang@fanruan.com tang.userid",
    )

    result = WeComUserDirectory(store).search_users("唐嘉伟")

    assert len(result) == 1
    assert result[0]["wecomUserId"] == "tang.userid"
    assert result[0]["displayName"] == "Tang.Tangerine-唐嘉伟"
    assert result[0]["preferredEmail"] == "tang@fanruan.com"
    assert result[0]["commitCount"] == 13


def test_search_users_by_userid_and_email():
    store = make_store()
    _insert(
        store,
        mappingKey="mars",
        authorName="Mars",
        normalizedEmail="mars@fanruan.com",
        authorEmail="Mars@Fanruan.com",
        wecomUserId="mars.userid",
        commitCount=5,
        searchText="mars mars.userid mars@fanruan.com",
    )

    by_userid = WeComUserDirectory(store).search_users("mars.userid")
    by_email = WeComUserDirectory(store).search_users("MARS@FANRUAN")

    assert by_userid[0]["displayName"] == "Mars"
    assert by_email[0]["wecomUserId"] == "mars.userid"


def test_preferred_email_falls_back_to_non_company_email():
    store = make_store()
    _insert(
        store,
        mappingKey="u1",
        authorName="User One",
        normalizedEmail="old@example.com",
        wecomUserId="user.one",
        commitCount=1,
    )
    _insert(
        store,
        mappingKey="u2",
        authorName="User One",
        normalizedEmail="new@example.com",
        wecomUserId="user.one",
        commitCount=20,
    )

    result = WeComUserDirectory(store).search_users("user.one")

    assert result[0]["preferredEmail"] == "new@example.com"


def test_empty_query_returns_common_users_by_commit_count():
    store = make_store()
    _insert(store, mappingKey="low", authorName="Low", normalizedEmail="low@fanruan.com", wecomUserId="low", commitCount=1)
    _insert(store, mappingKey="high", authorName="High", normalizedEmail="high@fanruan.com", wecomUserId="high", commitCount=10)

    result = WeComUserDirectory(store).search_users("", limit=2)

    assert [item["wecomUserId"] for item in result] == ["high", "low"]


def test_search_users_ignores_unmapped_rows_and_limits_results():
    store = make_store()
    _insert(store, mappingKey="missing", authorName="No User", normalizedEmail="no@fanruan.com", wecomUserId="", commitCount=100)
    for idx in range(3):
        _insert(
            store,
            mappingKey=f"user-{idx}",
            authorName=f"User {idx}",
            normalizedEmail=f"user{idx}@fanruan.com",
            wecomUserId=f"user{idx}",
            commitCount=idx + 1,
            searchText=f"user {idx} user{idx}@fanruan.com",
        )

    result = WeComUserDirectory(store).search_users("user", limit=2)

    assert [item["wecomUserId"] for item in result] == ["user2", "user1"]


def test_search_users_caps_limit_at_50():
    store = make_store()
    for idx in range(60):
        _insert(
            store,
            mappingKey=f"user-{idx}",
            authorName=f"User {idx}",
            normalizedEmail=f"user{idx}@fanruan.com",
            wecomUserId=f"user{idx}",
            commitCount=idx + 1,
            searchText="user",
        )

    result = WeComUserDirectory(store).search_users("user", limit=100)

    assert len(result) == 50


def test_resolve_preferred_email_by_userid():
    store = make_store()
    _insert(
        store,
        mappingKey="mars",
        authorName="Mars",
        normalizedEmail="mars@fanruan.com",
        wecomUserId="mars.userid",
        commitCount=5,
    )

    assert WeComUserDirectory(store).resolve_preferred_email(wecom_userid="mars.userid") == "mars@fanruan.com"
