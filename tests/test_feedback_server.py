from __future__ import annotations

from dataclasses import replace

from fastapi.testclient import TestClient

from ci_owner_agent.config import load_settings
from ci_owner_agent.schemas import BuildInfo, CiResponsibilityNotice
from ci_owner_agent.server import create_app
from tests.test_history_store import focused_chunk, make_store
from tests.test_notification_formatter import item, notice_payload


def _client_with_notice(*, token: str | None = None, notice: CiResponsibilityNotice | None = None):
    settings = replace(load_settings(), history_enabled=True, feedback_shared_token=token)
    store = make_store()
    notice = notice or CiResponsibilityNotice.model_validate(notice_payload([item("Tang")]))
    build_info = BuildInfo(
        job=notice.job,
        buildNumber=notice.buildNumber,
        result=notice.result,
        buildUrl=notice.buildUrl,
        branch=notice.branch,
        commit=notice.headCommit,
    )
    store.save_analysis(build_info, notice, notice.baseCommit, notice.headCommit, None, None, [focused_chunk("FAIL same\nError: UNKNOWN")])
    return TestClient(create_app(settings=settings, history_store=store)), store, notice


def test_health_returns_ok():
    client = TestClient(create_app(settings=replace(load_settings(), history_enabled=False), history_store=make_store()))

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_feedback_renders_notice_items():
    client, _, notice = _client_with_notice()

    response = client.get("/feedback", params={"repo": notice.repo, "job": notice.job, "build": notice.buildNumber})

    assert response.status_code == 200
    assert "CI 反馈" in response.text
    assert "EtlUtils - getInputEntryInfo" in response.text
    assert "Tang" in response.text
    assert '<select name="action">' in response.text
    assert 'data-owner-search="' in response.text
    assert "/api/wecom-users/search" in response.text
    assert "<datalist" in response.text


def test_feedback_post_correct_owner_writes_active_feedback():
    client, store, notice = _client_with_notice()

    response = client.post(
        "/feedback",
        data={
            "repo": notice.repo,
            "job": notice.job,
            "build": str(notice.buildNumber),
            "failureId": notice.responsibilityItems[0].failureId,
            "action": "correct_owner",
            "ownerName": "Li Si",
            "ownerEmail": "lisi@example.com",
            "ownerType": "high_confidence",
            "reviewer": "qa",
            "note": "人工确认",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    active = [doc for doc in store.feedback.docs if doc.get("isActive")]
    assert len(active) == 1
    assert active[0]["correctedOwner"]["name"] == "Li Si"


def test_feedback_post_saves_wecom_userid():
    client, store, notice = _client_with_notice()

    response = client.post(
        "/feedback",
        data={
            "repo": notice.repo,
            "job": notice.job,
            "build": str(notice.buildNumber),
            "failureId": notice.responsibilityItems[0].failureId,
            "action": "correct_owner",
            "ownerName": "Li Si",
            "ownerEmail": "lisi@fanruan.com",
            "ownerType": "high_confidence",
            "wecomUserId": "lisi.userid",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    active = [doc for doc in store.feedback.docs if doc.get("isActive")]
    assert active[0]["correctedOwnerWeComUserId"] == "lisi.userid"


def test_feedback_token_required():
    client, _, notice = _client_with_notice(token="secret")

    assert client.get("/feedback", params={"repo": notice.repo, "job": notice.job, "build": notice.buildNumber}).status_code == 403
    assert client.get("/feedback", params={"repo": notice.repo, "job": notice.job, "build": notice.buildNumber, "token": "wrong"}).status_code == 403
    assert client.get("/feedback", params={"repo": notice.repo, "job": notice.job, "build": notice.buildNumber, "token": "secret"}).status_code == 200


def test_wecom_user_search_api_requires_token():
    client, store, _ = _client_with_notice(token="secret")
    store.wecom_users.update_one(
        {"normalizedEmail": "tang@fanruan.com"},
        {"$set": {"authorName": "Tang.Tangerine-唐嘉伟", "normalizedEmail": "tang@fanruan.com", "wecomUserId": "tang.userid", "commitCount": 1}},
        upsert=True,
    )

    denied = client.get("/api/wecom-users/search", params={"q": "Tang"})
    allowed = client.get("/api/wecom-users/search", params={"q": "Tang", "token": "secret"})

    assert denied.status_code == 403
    assert denied.json()["ok"] is False
    assert allowed.status_code == 200
    assert allowed.json()["items"][0]["wecomUserId"] == "tang.userid"


def test_wecom_user_search_api_returns_grouped_users():
    client, store, _ = _client_with_notice()
    store.wecom_users.update_one(
        {"mappingKey": "tang-1"},
        {
            "$set": {
                "mappingKey": "tang-1",
                "authorName": "Tang",
                "normalizedEmail": "tang@gmail.com",
                "wecomUserId": "tang.userid",
                "commitCount": 1,
                "searchText": "tang tang.userid",
            }
        },
        upsert=True,
    )
    store.wecom_users.update_one(
        {"mappingKey": "tang-2"},
        {
            "$set": {
                "mappingKey": "tang-2",
                "authorName": "Tang.Tangerine-唐嘉伟",
                "normalizedEmail": "tang@fanruan.com",
                "wecomUserId": "tang.userid",
                "commitCount": 9,
                "searchText": "tang 唐嘉伟 tang.userid",
            }
        },
        upsert=True,
    )

    response = client.get("/api/wecom-users/search", params={"q": "tang"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["items"][0]["displayName"] == "Tang.Tangerine-唐嘉伟"
    assert payload["items"][0]["preferredEmail"] == "tang@fanruan.com"


def test_feedback_post_requires_token():
    client, store, notice = _client_with_notice(token="secret")

    response = client.post(
        "/feedback",
        data={
            "repo": notice.repo,
            "job": notice.job,
            "build": str(notice.buildNumber),
            "failureId": notice.responsibilityItems[0].failureId,
            "action": "confirm_owner",
        },
        follow_redirects=False,
    )

    assert response.status_code == 403
    assert store.feedback.docs == []


def test_feedback_post_accepts_valid_token():
    client, store, notice = _client_with_notice(token="secret")

    response = client.post(
        "/feedback",
        data={
            "repo": notice.repo,
            "job": notice.job,
            "build": str(notice.buildNumber),
            "failureId": notice.responsibilityItems[0].failureId,
            "action": "confirm_owner",
            "token": "secret",
            "reviewer": "qa",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    active = [doc for doc in store.feedback.docs if doc.get("isActive")]
    assert len(active) == 1
    assert active[0]["action"] == "confirm_owner"


def test_feedback_html_escapes_notice_text():
    payload = notice_payload([item()])
    payload["responsibilityItems"][0]["failureTitle"] = "<script>alert(1)</script>"
    payload["responsibilityItems"][0]["reason"] = "<script>alert(2)</script>"
    notice = CiResponsibilityNotice.model_validate(payload)
    client, _, _ = _client_with_notice(notice=notice)

    response = client.get("/feedback", params={"repo": notice.repo, "job": notice.job, "build": notice.buildNumber})

    assert response.status_code == 200
    assert "<script>alert" not in response.text
    assert "&lt;script&gt;alert" in response.text


def test_feedback_notice_not_found():
    client = TestClient(create_app(settings=replace(load_settings(), history_enabled=True, feedback_shared_token=None), history_store=make_store()))

    response = client.get("/feedback", params={"repo": "missing-repo", "job": "missing", "build": 1})

    assert response.status_code == 404
    assert "notice not found" in response.text
