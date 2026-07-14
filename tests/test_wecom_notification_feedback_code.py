from __future__ import annotations

from dataclasses import replace

from ci_owner_agent.config import load_settings
from ci_owner_agent.main import _notify_notice
from ci_owner_agent.schemas import CiResponsibilityNotice
from ci_owner_agent.services.notification_formatter import notification_digest
from tests.test_history_store import make_store
from tests.test_notification_formatter import item, notice_payload


def test_notification_gets_stable_persisted_feedback_code(monkeypatch):
    store = make_store()
    notice = CiResponsibilityNotice.model_validate(notice_payload([item("Tang")]))
    settings = replace(load_settings(), notification_dedup_enabled=False, wecom_user_mapping_file=None)
    monkeypatch.setattr("ci_owner_agent.main.get_history_store", lambda settings: store)

    first = _notify_notice(notice, settings, dry_run=True, force=False, feedback_base_url=None)
    second = _notify_notice(notice, settings, dry_run=True, force=False, feedback_base_url=None)

    code = store.feedback_contexts.docs[0]["code"]
    assert f"反馈码：{code}" in first["markdown"]
    assert f"反馈码：{code}" in second["markdown"]
    assert notification_digest(notice) == notification_digest(notice)


def test_notification_without_mongodb_still_formats(monkeypatch):
    notice = CiResponsibilityNotice.model_validate(notice_payload([item("Tang")]))
    settings = replace(load_settings(), notification_dedup_enabled=False, wecom_user_mapping_file=None)
    monkeypatch.setattr("ci_owner_agent.main.get_history_store", lambda settings: None)
    result = _notify_notice(notice, settings, dry_run=True, force=False, feedback_base_url="https://feedback.example")
    assert result["ok"] is True
    assert "反馈码：" not in result["markdown"]
    assert "提交反馈" in result["markdown"]
