from __future__ import annotations

from dataclasses import replace

from ci_owner_agent.config import load_settings
from ci_owner_agent.main import _notify_notice
from ci_owner_agent.schemas import CiResponsibilityNotice
from ci_owner_agent.services.notification_formatter import notification_digest
from ci_owner_agent.services.wecom_bot_models import WeComInboundMessage
from ci_owner_agent.services.wecom_feedback_service import WeComFeedbackService
from tests.test_history_store import make_store
from tests.test_notification_formatter import item, notice_payload


def _message(event_key: str, content: str) -> WeComInboundMessage:
    return WeComInboundMessage(event_key=event_key, sender_userid="reviewer", sender_name="Reviewer", content=content)


def test_notify_notice_persists_notice_for_bot_feedback(monkeypatch):
    store = make_store()
    notice = CiResponsibilityNotice.model_validate(notice_payload([item("Tang")]))
    settings = replace(load_settings(), notification_dedup_enabled=False, wecom_user_mapping_file=None)
    monkeypatch.setattr("ci_owner_agent.main.get_history_store", lambda settings: store)

    first = _notify_notice(notice, settings, dry_run=True, force=False, feedback_base_url=None)
    code = store.feedback_contexts.docs[0]["code"]
    assert f"反馈码：{code}" in first["markdown"]
    assert store.notices.find_one({"repo": notice.repo, "job": notice.job, "branch": notice.branch, "buildNumber": notice.buildNumber})

    service = WeComFeedbackService(store)
    service.handle(_message("create", f"{code} 1 判断正确"))
    confirmation = store.wecom_pending_feedback.docs[0]["confirmationCode"]
    assert "反馈已提交" in service.handle(_message("confirm", f"确认 {confirmation}"))
    assert store.feedback.docs


def test_feedback_code_does_not_change_notification_digest(monkeypatch):
    store = make_store()
    notice_a = CiResponsibilityNotice.model_validate(notice_payload([item("Tang")]))
    notice_b = CiResponsibilityNotice.model_validate(notice_payload([item("Tang")]))
    settings = replace(load_settings(), notification_dedup_enabled=False, wecom_user_mapping_file=None)
    monkeypatch.setattr("ci_owner_agent.main.get_history_store", lambda settings: store)
    digest_before = notification_digest(notice_a)

    first = _notify_notice(notice_a, settings, dry_run=True, force=False, feedback_base_url=None)
    second = _notify_notice(notice_b, settings, dry_run=True, force=False, feedback_base_url=None)

    code = store.feedback_contexts.docs[0]["code"]
    digest_after_a = notification_digest(notice_a)
    digest_after_b = notification_digest(notice_b)
    assert f"反馈码：{code}" in first["markdown"]
    assert f"反馈码：{code}" in second["markdown"]
    assert digest_before == digest_after_a == digest_after_b
    assert code not in digest_after_a


def test_feedback_code_is_hidden_when_notice_snapshot_persistence_fails(monkeypatch, capsys):
    store = make_store()
    notice = CiResponsibilityNotice.model_validate(notice_payload([item("Tang")]))
    settings = replace(load_settings(), notification_dedup_enabled=False, wecom_user_mapping_file=None)
    monkeypatch.setattr("ci_owner_agent.main.get_history_store", lambda settings: store)
    monkeypatch.setattr(store, "upsert_notice_snapshot", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("write failed")))

    result = _notify_notice(notice, settings, dry_run=True, force=False, feedback_base_url=None)

    assert result["ok"] is True
    assert "反馈码：" not in result["markdown"]
    assert store.feedback_contexts.docs == []
    assert "feedback context unavailable" in capsys.readouterr().err


def test_notification_without_mongodb_still_formats(monkeypatch):
    notice = CiResponsibilityNotice.model_validate(notice_payload([item("Tang")]))
    settings = replace(load_settings(), notification_dedup_enabled=False, wecom_user_mapping_file=None)
    monkeypatch.setattr("ci_owner_agent.main.get_history_store", lambda settings: None)
    result = _notify_notice(notice, settings, dry_run=True, force=False, feedback_base_url="https://feedback.example")
    assert result["ok"] is True
    assert "反馈码：" not in result["markdown"]
    assert "提交反馈" in result["markdown"]
