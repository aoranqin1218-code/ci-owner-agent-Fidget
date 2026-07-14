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


def _notice_with_item(*, repo="sample-ts-repo", branch="dev", failure_id="failure-old", title="旧失败"):
    payload = notice_payload([item("张三")])
    payload["repo"] = repo
    payload["branch"] = branch
    payload["responsibilityItems"][0]["failureId"] = failure_id
    payload["responsibilityItems"][0]["failureSignature"] = f"signature-{failure_id}"
    payload["responsibilityItems"][0]["failureTitle"] = title
    return CiResponsibilityNotice.model_validate(payload)


def test_same_build_reuses_code_and_refreshes_responsibility_items():
    store = make_store()
    contexts = FeedbackContextStore(store)
    first_notice = _notice_with_item()
    first = contexts.get_or_create_for_notice(first_notice)
    original_code = first["code"]
    original_created_at = first["createdAt"]
    old_failure_id = first["responsibilityItems"][0]["failureId"]

    second_notice = _notice_with_item(failure_id="failure-new", title="新失败")
    second = contexts.get_or_create_for_notice(second_notice)
    resolved_context, resolved_item = contexts.resolve_item(original_code, 1)

    assert second["code"] == original_code
    assert second["createdAt"] == original_created_at
    assert second["responsibilityItems"][0]["failureTitle"] == "新失败"
    assert resolved_context["code"] == original_code
    assert resolved_item["failureId"] == second_notice.responsibilityItems[0].failureId
    assert resolved_item["failureId"] != old_failure_id
    scope = {"repo": second_notice.repo, "job": second_notice.job, "branch": second_notice.branch, "buildNumber": second_notice.buildNumber}
    assert len(list(store.feedback_contexts.find(scope))) == 1


def test_context_refresh_isolated_by_repo_and_branch():
    store = make_store()
    contexts = FeedbackContextStore(store)
    primary = contexts.get_or_create_for_notice(_notice_with_item())
    other_repo = contexts.get_or_create_for_notice(_notice_with_item(repo="other-repo", title="其他仓库失败"))
    other_branch = contexts.get_or_create_for_notice(_notice_with_item(branch="feature", title="其他分支失败"))

    refreshed = contexts.get_or_create_for_notice(_notice_with_item(failure_id="failure-new", title="刷新后的失败"))

    assert refreshed["code"] == primary["code"]
    assert contexts.get_active(other_repo["code"])["responsibilityItems"][0]["failureTitle"] == "其他仓库失败"
    assert contexts.get_active(other_branch["code"])["responsibilityItems"][0]["failureTitle"] == "其他分支失败"
    assert len(store.feedback_contexts.docs) == 3


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


def test_forbidden_user_cannot_reconcile_existing_operation():
    store, _, context = _setup()
    service = WeComFeedbackService(store)
    service.handle(_message("create", f"{context['code']} 1 判断正确"))
    pending = store.wecom_pending_feedback.docs[0]
    service.feedback.apply_feedback(repo=context["repo"], job=context["job"], branch=context["branch"], build_number=context["buildNumber"], failure_id=pending["feedbackContext"]["failureId"], failure_signature=None, action="confirm_owner", operation_id=pending["operationId"])
    assert "只能由发起" in service.handle(_message("other", f"确认 {pending['confirmationCode']}", userid="other"))
    assert pending["status"] == "pending"


def test_pending_confirmation_is_rejected_when_item_changed():
    store = make_store()
    first_notice = _notice_with_item(failure_id="failure-old", title="旧失败")
    store.upsert_notice_snapshot(first_notice, source="test")
    contexts = FeedbackContextStore(store)
    context = contexts.get_or_create_for_notice(first_notice)
    service = WeComFeedbackService(store)
    service.handle(_message("msg:create", f"{context['code']} 1 判断正确"))
    confirmation = store.wecom_pending_feedback.docs[0]["confirmationCode"]

    second_notice = _notice_with_item(failure_id="failure-new", title="新失败")
    store.upsert_notice_snapshot(second_notice, source="test")
    contexts.get_or_create_for_notice(second_notice)
    reply = service.handle(_message("msg:confirm", f"确认 {confirmation}"))

    assert "责任项已经更新" in reply
    assert store.feedback.docs == []
    assert store.wecom_pending_feedback.docs[0]["status"] == "stale"
    assert "已经更新" in service.handle(_message("msg:again", f"确认 {confirmation}"))


def test_pending_confirmation_succeeds_when_context_refreshes_but_item_is_same():
    store = make_store()
    notice = _notice_with_item(failure_id="failure-same", title="同一失败")
    store.upsert_notice_snapshot(notice, source="test")
    contexts = FeedbackContextStore(store)
    context = contexts.get_or_create_for_notice(notice)
    service = WeComFeedbackService(store)
    service.handle(_message("msg:create", f"{context['code']} 1 判断正确"))
    confirmation = store.wecom_pending_feedback.docs[0]["confirmationCode"]

    contexts.get_or_create_for_notice(notice)
    reply = service.handle(_message("msg:confirm", f"确认 {confirmation}"))

    assert "反馈已提交" in reply
    assert store.feedback.docs[0]["failureId"] == notice.responsibilityItems[0].failureId


def test_context_lookup_exception_marks_pending_failed(monkeypatch):
    store, _, context = _setup()
    service = WeComFeedbackService(store)
    service.handle(_message("msg:create", f"{context['code']} 1 判断正确"))
    code = store.wecom_pending_feedback.docs[0]["confirmationCode"]
    monkeypatch.setattr(service.contexts, "resolve_item", lambda *args: (_ for _ in ()).throw(RuntimeError("lookup failed")))
    assert "反馈写入失败" in service.handle(_message("msg:confirm", f"确认 {code}"))
    assert store.wecom_pending_feedback.docs[0]["status"] == "failed"
    assert store.feedback.docs == []


def test_corrupted_pending_intent_marks_pending_failed():
    store, _, context = _setup()
    service = WeComFeedbackService(store)
    service.handle(_message("msg:create", f"{context['code']} 1 判断正确"))
    pending = store.wecom_pending_feedback.docs[0]
    pending["intent"] = {"intent_type": "create_feedback"}
    assert "反馈写入失败" in service.handle(_message("msg:confirm", f"确认 {pending['confirmationCode']}"))
    assert pending["status"] == "failed"


def test_user_directory_exception_marks_pending_failed(monkeypatch):
    store, _, context = _setup()
    service = WeComFeedbackService(store)
    service.handle(_message("msg:create", f"{context['code']} 1 责任人改为 @李四", mentions=(("lisi", "李四"),)))
    code = store.wecom_pending_feedback.docs[0]["confirmationCode"]
    monkeypatch.setattr(service.users, "search_users", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("directory failed")))
    assert "反馈写入失败" in service.handle(_message("msg:confirm", f"确认 {code}"))
    assert store.wecom_pending_feedback.docs[0]["status"] == "failed"


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
    expired["confirmationExpiresAt"] = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1)
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

import datetime as dt



import datetime as dt

# --- Regression: operation written, post-insert lookup failure ---

def test_operation_inserted_then_post_insert_lookup_failure_reconciles_applied(monkeypatch):
    store, _, context = _setup()
    service = WeComFeedbackService(store)
    service.handle(_message('msg:create', context['code'] + ' 1 \u5224\u65ad\u6b63\u786e'))
    pending = store.wecom_pending_feedback.docs[0]
    code = pending['confirmationCode']

    original = service.feedback.is_current_operation
    def failing_is_current(op):
        raise RuntimeError('post-insert lookup failure')
    monkeypatch.setattr(service.feedback, 'is_current_operation', failing_is_current)

    mark_failed_calls = []
    monkeypatch.setattr(service.pending, 'mark_failed', lambda *a, **kw: mark_failed_calls.append(1) or False)

    reply = service.handle(_message('msg:confirm', '\u786e\u8ba4 ' + code))

    assert len(mark_failed_calls) == 0
    assert '\u5df2\u63d0\u4ea4' in reply
    assert store.wecom_pending_feedback.docs[0]['status'] == 'applied'
    ops = [d for d in store.feedback.docs if d.get('recordType') == 'operation']
    assert len(ops) == 1


def test_existing_operation_is_not_marked_failed_after_apply_exception(monkeypatch):
    store, _, context = _setup()
    service = WeComFeedbackService(store)
    service.handle(_message('msg:create', context['code'] + ' 1 \u5224\u65ad\u6b63\u786e'))
    pending = store.wecom_pending_feedback.docs[0]
    code = pending['confirmationCode']

    store.feedback.insert_one({
        '_id': 'operation:' + pending['operationId'],
        'recordType': 'operation',
        'repo': context['repo'],
        'job': context['job'],
        'branch': context['branch'],
        'buildNumber': context['buildNumber'],
        'operationId': pending['operationId'],
        'action': 'confirm_owner',
        'feedbackItemKey': 'id:test-failure',
        'submittedAt': dt.datetime.now(dt.timezone.utc),
    })

    monkeypatch.setattr(service.feedback, 'apply_feedback',
                        lambda **kw: (_ for _ in ()).throw(RuntimeError('apply failed')))

    mark_failed_calls = []
    monkeypatch.setattr(service.pending, 'mark_failed', lambda *a, **kw: mark_failed_calls.append(1) or False)

    reply = service.handle(_message('msg:confirm', '\u786e\u8ba4 ' + code))

    assert len(mark_failed_calls) == 0
    assert '\u5df2\u63d0\u4ea4' in reply
    assert store.wecom_pending_feedback.docs[0]['status'] == 'applied'


# --- Regression: expired status persistence ---

def test_expired_claim_persists_expired_status():
    store, _, context = _setup()
    service = WeComFeedbackService(store)
    service.handle(_message('msg:create', context['code'] + ' 1 \u5224\u65ad\u6b63\u786e'))
    pending = store.wecom_pending_feedback.docs[0]
    code = pending['confirmationCode']

    pending['confirmationExpiresAt'] = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1)

    reply = service.handle(_message('msg:confirm', '\u786e\u8ba4 ' + code))

    assert '\u5df2\u8fc7\u671f' in reply
    assert store.wecom_pending_feedback.docs[0]['status'] == 'expired'


def test_expired_pending_remains_expired_on_next_request():
    store, _, context = _setup()
    service = WeComFeedbackService(store)
    service.handle(_message('msg:create', context['code'] + ' 1 \u5224\u65ad\u6b63\u786e'))
    pending = store.wecom_pending_feedback.docs[0]
    code = pending['confirmationCode']

    pending['confirmationExpiresAt'] = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1)

    reply1 = service.handle(_message('msg:confirm1', '\u786e\u8ba4 ' + code))
    assert '\u5df2\u8fc7\u671f' in reply1
    assert store.wecom_pending_feedback.docs[0]['status'] == 'expired'

    reply2 = service.handle(_message('msg:confirm2', '\u786e\u8ba4 ' + code))
    assert '\u5df2\u8fc7\u671f' in reply2
    assert store.wecom_pending_feedback.docs[0]['status'] == 'expired'


# --- Regression: cancel state classification ---

def test_cancel_returns_forbidden_for_other_user():
    store, _, context = _setup()
    service = WeComFeedbackService(store)
    service.handle(_message('msg:create', context['code'] + ' 1 \u5224\u65ad\u6b63\u786e'))
    code = store.wecom_pending_feedback.docs[0]['confirmationCode']

    reply = service.handle(_message('msg:cancel', '\u53d6\u6d88 ' + code, userid='other_user'))

    assert '\u53ea\u80fd\u7531\u53d1\u8d77' in reply


def test_cancel_returns_expired_after_confirmation_window():
    store, _, context = _setup()
    service = WeComFeedbackService(store)
    service.handle(_message('msg:create', context['code'] + ' 1 \u5224\u65ad\u6b63\u786e'))
    pending = store.wecom_pending_feedback.docs[0]
    code = pending['confirmationCode']

    pending['confirmationExpiresAt'] = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1)

    reply = service.handle(_message('msg:cancel', '\u53d6\u6d88 ' + code))

    assert '\u5df2\u8fc7\u671f' in reply
    assert store.wecom_pending_feedback.docs[0]['status'] == 'expired'


# --- Regression: CAS token safety ---

def test_old_apply_token_cannot_mark_applied():
    store, _, context = _setup()
    service = WeComFeedbackService(store)
    service.handle(_message('msg:create', context['code'] + ' 1 \u5224\u65ad\u6b63\u786e'))
    pending = store.wecom_pending_feedback.docs[0]
    code = pending['confirmationCode']

    status, claimed = service.pending.claim(code, 'wangwu')
    assert status == 'claimed'
    original_token = claimed['applyToken']

    claimed['applyToken'] = 'different_token'

    result = service.pending.mark_applied(claimed, original_token)
    assert result is False
    assert store.wecom_pending_feedback.docs[0]['status'] == 'applying'


def test_old_apply_token_cannot_mark_failed():
    store, _, context = _setup()
    service = WeComFeedbackService(store)
    service.handle(_message('msg:create', context['code'] + ' 1 \u5224\u65ad\u6b63\u786e'))
    pending = store.wecom_pending_feedback.docs[0]
    code = pending['confirmationCode']

    status, claimed = service.pending.claim(code, 'wangwu')
    assert status == 'claimed'
    original_token = claimed['applyToken']

    claimed['applyToken'] = 'different_token'

    result = service.pending.mark_failed(claimed, original_token, 'test error')
    assert result is False
    assert store.wecom_pending_feedback.docs[0]['status'] == 'applying'


def test_old_apply_token_cannot_mark_stale():
    store, _, context = _setup()
    service = WeComFeedbackService(store)
    service.handle(_message('msg:create', context['code'] + ' 1 \u5224\u65ad\u6b63\u786e'))
    pending = store.wecom_pending_feedback.docs[0]
    code = pending['confirmationCode']

    status, claimed = service.pending.claim(code, 'wangwu')
    assert status == 'claimed'
    original_token = claimed['applyToken']

    claimed['applyToken'] = 'different_token'

    result = service.pending.mark_stale(claimed, original_token, 'test reason')
    assert result is False
    assert store.wecom_pending_feedback.docs[0]['status'] == 'applying'


def test_mark_applied_clears_token_and_lease():
    store, _, context = _setup()
    service = WeComFeedbackService(store)
    service.handle(_message('msg:create', context['code'] + ' 1 \u5224\u65ad\u6b63\u786e'))
    pending = store.wecom_pending_feedback.docs[0]
    code = pending['confirmationCode']

    status, claimed = service.pending.claim(code, 'wangwu')
    assert status == 'claimed'
    assert claimed['applyToken'] is not None
    assert claimed['applyLeaseUntil'] is not None

    result = service.pending.mark_applied(claimed, claimed['applyToken'])
    assert result is True

    refreshed = store.wecom_pending_feedback.docs[0]
    assert refreshed['status'] == 'applied'
    assert refreshed.get('applyToken') is None
    assert refreshed.get('applyLeaseUntil') is None
