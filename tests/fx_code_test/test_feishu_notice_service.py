from __future__ import annotations

from dataclasses import replace
from unittest.mock import Mock

import pytest

from ci_owner_agent.config import load_settings
from ci_owner_agent.schemas import CiResponsibilityNotice
from ci_owner_agent.services import feishu_notice_service
from tests.fx_code_test.test_history_store import make_store


def notice_payload(items, **overrides):
    payload = {
        "repo": "fxp-fidget",
        "job": "fidget-build",
        "buildNumber": 100,
        "buildUrl": "https://jenkins.test/job/fidget-build/100/",
        "result": "FAILURE",
        "branch": "master",
        "owner": {"type": "no_high_confidence_owner", "name": "无高可信责任人", "email": None, "commit": None, "confidence": 0},
        "failureReason": "构建失败。",
        "evidence": [],
        "responsibilityItems": items,
        "suggestions": [],
        "hasHighConfidenceOwner": False,
    }
    payload.update(overrides)
    return payload


def current_item(name="张三", email="zs@example.com"):
    return {
        "failureId": "failure-1",
        "failureTitle": "F-1: group by fails",
        "failureSignature": None,
        "failureSummary": None,
        "testFilePath": None,
        "failureFilePath": None,
        "owner": {"type": "high_confidence", "name": name, "email": email, "commit": "abc", "confidence": 0.9},
        "responsibilityType": "current_build_owner",
        "sourceBuildNumber": 100,
        "sourceBuildUrl": None,
        "sourceCommit": "abc",
        "matchType": None,
        "relationship": None,
        "confidence": 0.9,
        "reason": "期望值不符",
        "evidenceIds": [],
    }


def _notice(**overrides):
    return CiResponsibilityNotice.model_validate(notice_payload([current_item()], **overrides))


def _settings(**overrides):
    base = dict(
        feishu_notify_enabled=True,
        feishu_webhook_url="https://example.test/hook/secret-token",
        feishu_webhook_secret=None,
        feishu_notify_dry_run=False,
        feishu_notify_on_success=False,
        feishu_notify_on_no_owner=True,
        feishu_user_mapping_file=None,
        feishu_fallback_userids=(),
        history_enabled=False,
        notification_dedup_enabled=True,
    )
    base.update(overrides)
    return replace(load_settings(), **base)


def _ok_send(monkeypatch):
    calls = []

    def fake_send(url, payload, *, secret=None, timeout=10):
        calls.append((url, payload, secret))
        return {"ok": True, "statusCode": 200, "response": "{}", "error": None}

    monkeypatch.setattr("ci_owner_agent.services.feishu_notice_service.send_feishu_payload", fake_send)
    return calls


def test_maybe_notify_notice_disabled_is_noop(monkeypatch):
    calls = _ok_send(monkeypatch)
    feishu_notice_service.maybe_notify_notice(_notice(), _settings(feishu_notify_enabled=False))
    assert calls == []


def test_maybe_notify_notice_success_gate(monkeypatch):
    calls = _ok_send(monkeypatch)
    notice = _notice(result="SUCCESS", owner={"type": "no_high_confidence_owner", "name": "无高可信责任人", "email": None, "commit": None, "confidence": 0}, responsibilityItems=[])
    feishu_notice_service.maybe_notify_notice(notice, _settings(feishu_notify_on_success=False))
    assert calls == []


def test_maybe_notify_notice_sends_when_enabled_and_not_dry_run(monkeypatch):
    calls = _ok_send(monkeypatch)
    feishu_notice_service.maybe_notify_notice(_notice(), _settings())
    assert len(calls) == 1
    assert calls[0][1]["msg_type"] == "interactive"


def test_notify_notice_dry_run_has_no_network_and_no_store_write(monkeypatch):
    get_store = Mock(side_effect=AssertionError("dry-run must not initialize Mongo history store"))
    monkeypatch.setattr("ci_owner_agent.services.feishu_notice_service.get_history_store", get_store)
    calls = _ok_send(monkeypatch)
    settings = _settings(feishu_notify_dry_run=True)
    result = feishu_notice_service.notify_notice(_notice(), settings, dry_run=True, force=False)
    assert result["ok"] is True and result["status"] == "dry_run"
    assert calls == []
    get_store.assert_not_called()


def test_notify_notice_secret_gate_fails_closed_before_network_and_store(monkeypatch):
    get_store = Mock(side_effect=AssertionError("blocked notice must not initialize Mongo history store"))
    monkeypatch.setattr("ci_owner_agent.services.feishu_notice_service.get_history_store", get_store)
    calls = _ok_send(monkeypatch)
    payload = notice_payload(
        [current_item()],
        evidence=[{"id": "E1", "type": "log", "summary": "db uri mongodb://root:secret@10.0.0.1:27017/admin", "detail": "x", "source": "log"}],
    )
    notice = CiResponsibilityNotice.model_validate(payload)
    result = feishu_notice_service.notify_notice(notice, _settings(), dry_run=False, force=False)
    assert result["ok"] is False and result["status"] == "blocked"
    assert calls == []
    get_store.assert_not_called()
    assert "plaintext secrets" in result["error"]


def test_notify_notice_dedup_skips_second_send_same_channel(monkeypatch):
    store = make_store()
    monkeypatch.setattr("ci_owner_agent.services.feishu_notice_service.get_history_store", lambda settings: store)
    calls = _ok_send(monkeypatch)
    settings = _settings()
    first = feishu_notice_service.notify_notice(_notice(), settings, dry_run=False, force=False)
    assert first["status"] == "sent"
    second = feishu_notice_service.notify_notice(_notice(), settings, dry_run=False, force=False)
    assert second["status"] == "skipped"
    assert len(calls) == 1


def test_notify_notice_force_bypasses_dedup(monkeypatch):
    store = make_store()
    monkeypatch.setattr("ci_owner_agent.services.feishu_notice_service.get_history_store", lambda settings: store)
    calls = _ok_send(monkeypatch)
    settings = _settings()
    assert feishu_notice_service.notify_notice(_notice(), settings, dry_run=False, force=False)["status"] == "sent"
    assert feishu_notice_service.notify_notice(_notice(), settings, dry_run=False, force=True)["status"] == "sent"
    assert len(calls) == 2


def test_notify_notice_channel_is_isolated_from_wecom(monkeypatch):
    store = make_store()
    monkeypatch.setattr("ci_owner_agent.services.feishu_notice_service.get_history_store", lambda settings: store)
    calls = _ok_send(monkeypatch)
    settings = _settings()
    result = feishu_notice_service.notify_notice(_notice(), settings, dry_run=False, force=False)
    assert result["status"] == "sent"
    assert store.notifications.find_one({"channel": "wecom"}) is None
    feishu_record = store.notifications.find_one({"channel": "feishu"})
    assert feishu_record is not None and feishu_record["status"] == "sent"


def test_notify_notice_missing_webhook_fails_without_send(monkeypatch):
    calls = _ok_send(monkeypatch)
    settings = _settings(feishu_webhook_url=None)
    result = feishu_notice_service.notify_notice(_notice(), settings, dry_run=False, force=False)
    assert result["ok"] is False and result["status"] == "failed"
    assert "CI_AGENT_FEISHU_WEBHOOK_URL is not configured" in result["error"]
    assert calls == []


def test_notify_notice_send_failure_does_not_raise(monkeypatch):
    monkeypatch.setattr(
        "ci_owner_agent.services.feishu_notice_service.send_feishu_payload",
        lambda *a, **k: {"ok": False, "statusCode": 400, "response": "{}", "error": "Feishu webhook returned code 9999: failed"},
    )
    result = feishu_notice_service.notify_notice(_notice(), _settings(), dry_run=False, force=False)
    assert result["ok"] is False and result["status"] == "failed"


def test_saved_notification_preview_redacts_open_ids(tmp_path, monkeypatch):
    mapping = tmp_path / "feishu_users.yml"
    mapping.write_text(
        "users:\n  - name: 张三\n    email: zs@example.com\n    openId: ou_owner123\n",
        encoding="utf-8",
    )
    store = make_store()
    monkeypatch.setattr("ci_owner_agent.services.feishu_notice_service.get_history_store", lambda settings: store)
    _ok_send(monkeypatch)
    settings = _settings(feishu_user_mapping_file=mapping)
    result = feishu_notice_service.notify_notice(_notice(), settings, dry_run=False, force=False)
    assert result["status"] == "sent"
    assert result["summary"]["atOpenIdCount"] == 1
    preview = store.notifications.find_one({"channel": "feishu"})["messagePreview"]
    assert "ou_owner123" not in preview
    assert "<at id=***></at>" in preview
