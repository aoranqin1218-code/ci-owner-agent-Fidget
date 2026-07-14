from __future__ import annotations

import datetime as dt

from ci_owner_agent.schemas import CiResponsibilityNotice
from ci_owner_agent.services.feedback_context_store import FeedbackContextStore
from ci_owner_agent.services.wecom_bot_models import WeComInboundMessage, WeComMentionedUser
from ci_owner_agent.services.wecom_feedback_service import WeComFeedbackService
from tests.test_history_store import make_store
from tests.test_notification_formatter import item, notice_payload


def _setup():
    store = make_store()
    notice = CiResponsibilityNotice.model_validate(notice_payload([item("张三")]))
    key = {"repo": notice.repo, "job": notice.job, "branch": notice.branch, "buildNumber": notice.buildNumber}
    store.notices.update_one(key, {"$set": {**key, "notice": notice.model_dump(mode="json")}}, upsert=True)
    context = FeedbackContextStore(store).get_or_create_for_notice(notice)
    return store, notice, context


def _message(event: str, content: str, userid: str = "wangwu", mentions=()):
    users = [WeComMentionedUser(userid=value, display_name=name) for value, name in mentions]
    return WeComInboundMessage(
        event_key=event,
        chat_id="chat",
        sender_userid=userid,
        sender_name="王五" if userid == "wangwu" else "其他人",
        content=content,
        mentioned_userids=[user.userid for user in users],
        mentioned_users=users,
    )


def test_context_reuses_code_and_validates_index():
    store, notice, context = _setup()
    assert FeedbackContextStore(store).get_or_create_for_notice(notice)["code"] == context["code"]
    try:
        FeedbackContextStore(store).resolve_item(context["code"], 2)
    except ValueError as exc:
        assert "序号越界" in str(exc)
    else:
        raise AssertionError("expected out-of-range error")


def test_pending_then_original_sender_confirms_and_audits():
    store, _, context = _setup()
    service = WeComFeedbackService(store)
    prompt = service.handle(_message("msg:create", f"{context['code']} 1 判断正确"))
    assert "请确认反馈" in prompt
    assert store.feedback.docs == []
    confirmation = store.wecom_pending_feedback.docs[0]["confirmationCode"]

    forbidden = service.handle(_message("msg:other", f"确认 {confirmation}", userid="other"))
    assert "只能由发起" in forbidden
    applied = service.handle(_message("msg:confirm", f"确认 {confirmation}"))
    assert "反馈已提交" in applied
    assert store.feedback.docs[0]["reviewerWeComUserId"] == "wangwu"
    assert store.feedback.docs[0]["source"] == "wecom_bot"
    assert "已经提交过" in service.handle(_message("msg:again", f"确认 {confirmation}"))


def test_cancel_and_expired_pending_do_not_write():
    store, _, context = _setup()
    service = WeComFeedbackService(store)
    service.handle(_message("msg:create", f"{context['code']} 1 标记偶发"))
    pending = store.wecom_pending_feedback.docs[0]
    code = pending["confirmationCode"]
    assert "已取消" in service.handle(_message("msg:cancel", f"取消 {code}"))
    assert store.feedback.docs == []

    service.handle(_message("msg:create2", f"{context['code']} 1 无法定责"))
    expired = store.wecom_pending_feedback.docs[1]
    expired["expiresAt"] = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1)
    assert "已过期" in service.handle(_message("msg:expired", f"确认 {expired['confirmationCode']}"))
    assert store.feedback.docs == []


def test_correct_owner_keeps_real_userid_without_directory_entry():
    store, _, context = _setup()
    service = WeComFeedbackService(store)
    service.handle(
        _message("msg:create", f"{context['code']} 1 责任人改为 @李四", mentions=(("lisi.userid", "李四"),))
    )
    code = store.wecom_pending_feedback.docs[0]["confirmationCode"]
    service.handle(_message("msg:confirm", f"确认 {code}"))
    assert store.feedback.docs[0]["correctedOwnerWeComUserId"] == "lisi.userid"
    assert store.feedback.docs[0]["correctedOwner"]["name"] == "李四"


def test_feedback_write_failure_marks_pending_failed(monkeypatch):
    store, _, context = _setup()
    service = WeComFeedbackService(store)
    service.handle(_message("msg:create", f"{context['code']} 1 判断正确"))
    code = store.wecom_pending_feedback.docs[0]["confirmationCode"]
    monkeypatch.setattr(service.feedback, "apply_feedback", lambda **kwargs: (_ for _ in ()).throw(RuntimeError("write failed")))

    reply = service.handle(_message("msg:confirm", f"确认 {code}"))

    assert "反馈写入失败" in reply
    assert store.wecom_pending_feedback.docs[0]["status"] == "failed"
