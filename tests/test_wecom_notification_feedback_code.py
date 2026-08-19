from __future__ import annotations

from dataclasses import replace

from ci_owner_agent.config import load_settings
from ci_owner_agent.schemas import CiResponsibilityNotice
from ci_owner_agent.services.wecom_notification_routing import notification_digest
from ci_owner_agent.services.wecom_bot_models import WeComInboundMessage, WeComTemplateCardEvent
from ci_owner_agent.services.wecom_feedback_service import WeComFeedbackService
from ci_owner_agent.services.wecom_notice_service import notify_notice as _notify_notice
from tests.test_history_store import make_store
from tests.test_notification_formatter import item, notice_payload


def _message(event_key: str, content: str) -> WeComInboundMessage:
    return WeComInboundMessage(event_key=event_key, sender_userid="reviewer", sender_name="Reviewer", content=content)


def test_notify_notice_persists_notice_for_bot_feedback(monkeypatch):
    store = make_store()
    notice = CiResponsibilityNotice.model_validate(notice_payload([item("Tang")]))
    settings = replace(load_settings(), notification_dedup_enabled=False, wecom_user_mapping_file=None)
    monkeypatch.setattr("ci_owner_agent.services.wecom_notice_service.get_history_store", lambda settings: store)

    first = _notify_notice(notice, settings, dry_run=True, force=False, feedback_base_url=None)
    code = store.feedback_contexts.docs[0]["code"]
    assert f"\u53cd\u9988\u7801\uff1a{code}" in first["markdown"]
    assert store.notices.find_one({"repo": notice.repo, "job": notice.job, "branch": notice.branch, "buildNumber": notice.buildNumber})

    service = WeComFeedbackService(store)
    reply = service.handle_text(_message("create", f"{code} 1 \u5224\u65ad\u6b63\u786e"))
    assert reply.reply_type == "template_card"
    task_id = reply.template_card["task_id"]

    event = WeComTemplateCardEvent(
        event_key="wecom-card:msg:confirm",
        message_id="msg:confirm",
        sender_userid="reviewer",
        task_id=task_id,
        button_key="confirm",
    )
    confirm_reply = service.handle_template_card_event(event)
    assert "\u5df2\u63d0\u4ea4" in (confirm_reply.template_card or {}).get("main_title", {}).get("title", "")
    assert store.feedback.docs


def test_feedback_code_does_not_change_notification_digest(monkeypatch):
    store = make_store()
    notice_a = CiResponsibilityNotice.model_validate(notice_payload([item("Tang")]))
    notice_b = CiResponsibilityNotice.model_validate(notice_payload([item("Tang")]))
    settings = replace(load_settings(), notification_dedup_enabled=False, wecom_user_mapping_file=None)
    monkeypatch.setattr("ci_owner_agent.services.wecom_notice_service.get_history_store", lambda settings: store)
    digest_before = notification_digest(notice_a)

    first = _notify_notice(notice_a, settings, dry_run=True, force=False, feedback_base_url=None)
    second = _notify_notice(notice_b, settings, dry_run=True, force=False, feedback_base_url=None)

    code = store.feedback_contexts.docs[0]["code"]
    digest_after_a = notification_digest(notice_a)
    digest_after_b = notification_digest(notice_b)
    assert code
    assert len(first["markdown"]) > 0
    assert second["markdown"] == first["markdown"]
    assert digest_before == digest_after_a == digest_after_b
