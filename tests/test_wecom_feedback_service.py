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

def test_context_lookup_exception_keeps_pending_applying(monkeypatch):
    """Context lookup exception in normal flow: pending stays applying, no mark_failed."""
    store, _, context = _setup()
    service = WeComFeedbackService(store)
    service.handle(_message("msg:create", f"{context['code']} 1 判断正确"))
    code = store.wecom_pending_feedback.docs[0]["confirmationCode"]
    monkeypatch.setattr(service.contexts, "resolve_item", lambda *args: (_ for _ in ()).throw(RuntimeError("lookup failed")))
    reply = service.handle(_message("msg:confirm", f"确认 {code}"))
    assert "正在确认" in reply
    assert store.wecom_pending_feedback.docs[0]["status"] == "applying"
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



# --- Regression: operation written, post-insert lookup failure ---
# --- Regression: operation written, post-insert lookup failure ---
def test_post_insert_lookup_failure_finalizes_existing_prepared_operation(monkeypatch):
    """apply_feedback inserts prepared operation, then is_current_operation raises;
    service must finalize, not mark_failed."""
    store, _, context = _setup()
    service = WeComFeedbackService(store)
    service.handle(_message('msg:create', context['code'] + ' 1 判断正确'))
    pending = store.wecom_pending_feedback.docs[0]
    code = pending['confirmationCode']

    original = service.feedback.is_current_operation
    def failing_is_current(op):
        raise RuntimeError('post-insert lookup failure')
    monkeypatch.setattr(service.feedback, 'is_current_operation', failing_is_current)

    mark_failed_calls = []
    monkeypatch.setattr(service.pending, 'mark_failed', lambda *a, **kw: mark_failed_calls.append(1) or False)

    reply = service.handle(_message('msg:confirm', '确认 ' + code))

    assert len(mark_failed_calls) == 0, 'mark_failed must not be called when operation already exists'
    assert store.wecom_pending_feedback.docs[0]['status'] == 'applied'
    ops = [d for d in store.feedback.docs if d.get('recordType') == 'operation']
    assert len(ops) == 1
    assert ops[0].get('isCommitted') == True
    assert '已提交' in reply

def test_existing_prepared_operation_is_adopted_without_reapplying(monkeypatch):
    """Prepared operation exists; service adopts it without re-applying; mark_failed never called."""
    store, _, context = _setup()
    service = WeComFeedbackService(store)
    service.handle(_message('msg:create', context['code'] + ' 1 判断正确'))
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
        'isCommitted': False,
    })

    apply_feedback_calls = []
    monkeypatch.setattr(service.feedback, 'apply_feedback',
                        lambda **kw: apply_feedback_calls.append(1) or (_ for _ in ()).throw(RuntimeError('should not be called')))

    mark_failed_calls = []
    monkeypatch.setattr(service.pending, 'mark_failed', lambda *a, **kw: mark_failed_calls.append(1) or False)

    mark_applied_calls = []
    original_mark = service.pending.mark_applied
    def tracking_mark(pending_doc, token):
        mark_applied_calls.append(1)
        return original_mark(pending_doc, token)
    monkeypatch.setattr(service.pending, 'mark_applied', tracking_mark)

    reply = service.handle(_message('msg:confirm', '确认 ' + code))

    assert len(apply_feedback_calls) == 0, 'apply_feedback must not be called when prepared operation already exists'
    assert len(mark_failed_calls) == 0, 'mark_failed must not be called'
    assert len(mark_applied_calls) == 1, 'mark_applied must be called exactly once'
    ops = [d for d in store.feedback.docs if d.get('recordType') == 'operation']
    assert len(ops) == 1
    assert ops[0].get('isCommitted') == True
    assert store.wecom_pending_feedback.docs[0]['status'] == 'applied'
    assert '已提交' in reply

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

# --- Regression: mark_applied CAS failure and exception reconciliation ---

def test_mark_applied_cas_failure_reconciles_existing_operation(monkeypatch):
    """mark_applied returns False; must reconcile, not tell user to retry."""
    store, _, context = _setup()
    service = WeComFeedbackService(store)
    service.handle(_message('msg:create', context['code'] + ' 1 \u5224\u65ad\u6b63\u786e'))
    pending = store.wecom_pending_feedback.docs[0]
    code = pending['confirmationCode']

    reconcile_calls = []
    original_reconcile = service.pending.reconcile_applied
    def tracking_reconcile(code, op_id):
        reconcile_calls.append(1)
        return original_reconcile(code, op_id)
    monkeypatch.setattr(service.pending, 'mark_applied', lambda *a, **kw: False)
    monkeypatch.setattr(service.pending, 'reconcile_applied', tracking_reconcile)

    reply = service.handle(_message('msg:confirm', '\u786e\u8ba4 ' + code))

    assert len(reconcile_calls) == 0
    assert '\u6b63\u5728\u534f\u8c03' in reply
    assert store.wecom_pending_feedback.docs[0]['status'] == 'applying'
    ops = [d for d in store.feedback.docs if d.get('recordType') == 'operation']
    assert len(ops) == 1
    assert ops[0].get('isCommitted') == False


def test_mark_applied_exception_reconciles_existing_operation(monkeypatch):
    """mark_applied raises; must reconcile, not tell user to retry."""
    store, _, context = _setup()
    service = WeComFeedbackService(store)
    service.handle(_message('msg:create', context['code'] + ' 1 \u5224\u65ad\u6b63\u786e'))
    pending = store.wecom_pending_feedback.docs[0]
    code = pending['confirmationCode']

    reconcile_calls = []
    original_reconcile = service.pending.reconcile_applied
    def tracking_reconcile(code, op_id):
        reconcile_calls.append(1)
        return original_reconcile(code, op_id)
    monkeypatch.setattr(service.pending, 'mark_applied',
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError('mark_applied failed')))
    monkeypatch.setattr(service.pending, 'reconcile_applied', tracking_reconcile)

    reply = service.handle(_message('msg:confirm', '\u786e\u8ba4 ' + code))

    assert len(reconcile_calls) == 0
    assert '\u6b63\u5728\u534f\u8c03' in reply
    assert store.wecom_pending_feedback.docs[0]['status'] == 'applying'
    ops = [d for d in store.feedback.docs if d.get('recordType') == 'operation']
    assert len(ops) == 1
    assert ops[0].get('isCommitted') == False


def test_operation_lookup_failure_keeps_pending_applying(monkeypatch):
    """When both apply_feedback and find_by_operation_id raise, keep applying, no mark_failed."""
    store, _, context = _setup()
    service = WeComFeedbackService(store)
    service.handle(_message('msg:create', context['code'] + ' 1 \u5224\u65ad\u6b63\u786e'))
    pending = store.wecom_pending_feedback.docs[0]
    code = pending['confirmationCode']

    monkeypatch.setattr(service.feedback, 'apply_feedback',
                        lambda **kw: (_ for _ in ()).throw(RuntimeError('apply failed')))

    find_calls = [0]
    def stub_find_by_op_id(op_id):
        find_calls[0] += 1
        if find_calls[0] == 1:
            return None  # pre-check succeeds, no operation
        raise RuntimeError('mongo down')  # exception handler: lookup fails

    monkeypatch.setattr(service.feedback, 'find_by_operation_id', stub_find_by_op_id)

    mark_failed_calls = []
    monkeypatch.setattr(service.pending, 'mark_failed', lambda *a, **kw: mark_failed_calls.append(1) or False)

    reply = service.handle(_message('msg:confirm', '\u786e\u8ba4 ' + code))

    assert len(mark_failed_calls) == 0
    assert '\u5904\u7406\u4e2d' in reply or '\u6b63\u5728\u5904\u7406' in reply or '\u6b63\u5728\u786e\u8ba4' in reply
    assert store.wecom_pending_feedback.docs[0]['status'] == 'applying'


# --- Regression: lease reclamation preserves state ---

def test_expired_lease_reclaim_preserves_submitted_at():
    """Re-claim after lease expiry gives new token, increments attempt, preserves submitted_at."""
    store, _, context = _setup()
    service = WeComFeedbackService(store)
    service.handle(_message('msg:create', context['code'] + ' 1 \u5224\u65ad\u6b63\u786e'))
    pending = store.wecom_pending_feedback.docs[0]
    code = pending['confirmationCode']

    # Worker A claims
    status_a, claimed_a = service.pending.claim(code, 'wangwu')
    assert status_a == 'claimed'
    token_a = claimed_a['applyToken']
    first_submitted_at = claimed_a['operationSubmittedAt']
    assert claimed_a['applyAttemptCount'] == 1

    # Expire the lease
    claimed_a['applyLeaseUntil'] = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1)

    # Worker B re-claims
    status_b, claimed_b = service.pending.claim(code, 'wangwu')
    assert status_b == 'claimed'
    token_b = claimed_b['applyToken']

    assert token_b != token_a
    assert claimed_b['applyAttemptCount'] == 2
    assert claimed_b['operationSubmittedAt'] == first_submitted_at


def test_old_worker_cannot_finish_after_lease_reclaim():
    """After lease reclamation, old token cannot mark_applied, mark_failed, or mark_stale."""
    store, _, context = _setup()
    service = WeComFeedbackService(store)
    service.handle(_message('msg:create', context['code'] + ' 1 \u5224\u65ad\u6b63\u786e'))
    pending = store.wecom_pending_feedback.docs[0]
    code = pending['confirmationCode']

    # Worker A claims
    status_a, claimed_a = service.pending.claim(code, 'wangwu')
    assert status_a == 'claimed'
    token_a = claimed_a['applyToken']

    # Expire the lease
    claimed_a['applyLeaseUntil'] = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1)

    # Worker B re-claims
    status_b, claimed_b = service.pending.claim(code, 'wangwu')
    assert status_b == 'claimed'
    token_b = claimed_b['applyToken']

    # Old token A cannot finish
    assert service.pending.mark_applied(claimed_a, token_a) is False
    assert service.pending.mark_failed(claimed_a, token_a, 'err') is False
    assert service.pending.mark_stale(claimed_a, token_a, 'reason') is False

    # Pending still applying with token B
    refreshed = store.wecom_pending_feedback.docs[0]
    assert refreshed['status'] == 'applying'
    assert refreshed['applyToken'] == token_b
    assert refreshed['applyLeaseUntil'] is not None


# --- Regression: prepared/committed two-phase operation ---

def test_old_worker_prepared_operation_is_not_visible_after_lease_reclaim(monkeypatch):
    """Worker A creates prepared op; after lease expiry, prepared op not in current_feedback."""
    store, _, context = _setup()
    service = WeComFeedbackService(store)
    service.handle(_message('msg:create', context['code'] + ' 1 \u5224\u65ad\u6b63\u786e'))
    pending = store.wecom_pending_feedback.docs[0]
    code = pending['confirmationCode']

    # Worker A claims and creates prepared operation
    status_a, claimed_a = service.pending.claim(code, 'wangwu')
    assert status_a == 'claimed'
    token_a = claimed_a['applyToken']

    service.feedback.apply_feedback(
        repo=context['repo'], job=context['job'], branch=context['branch'],
        build_number=context['buildNumber'],
        failure_id=pending['feedbackContext'].get('failureId'),
        failure_signature=pending['feedbackContext'].get('failureSignature'),
        action='confirm_owner', source='wecom_bot',
        operation_id=pending['operationId'], is_committed=False,
    )
    ops_all = [d for d in store.feedback.docs if d.get('recordType') == 'operation']
    assert len(ops_all) == 1
    assert ops_all[0].get('isCommitted') == False

    # Prepared operation must NOT appear in list_feedback or current_feedback_operations
    visible = service.feedback.list_feedback(
        repo=context['repo'], job=context['job'], branch=context['branch'],
        build_number=context['buildNumber'],
    )
    assert len(visible) == 0

    # Expire lease; worker B re-claims
    claimed_a['applyLeaseUntil'] = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1)
    status_b, claimed_b = service.pending.claim(code, 'wangwu')
    assert status_b == 'claimed'
    token_b = claimed_b['applyToken']
    assert token_b != token_a

    # Worker A cannot activate (pending is still applying, not applied)
    activation = service.pending.activate_operation_if_pending_applied(
        pending['confirmationCode'], pending['operationId'], service.feedback.collection,
    )
    assert activation == 'not_applied', f'expected not_applied, got {activation}'


def test_old_worker_cannot_activate_operation_after_losing_token(monkeypatch):
    """Old token holder cannot activate the prepared operation; activate_operation_if_pending_applied
    requires pending.status == applied."""
    store, _, context = _setup()
    service = WeComFeedbackService(store)
    service.handle(_message('msg:create', context['code'] + ' 1 \u5224\u65ad\u6b63\u786e'))
    pending = store.wecom_pending_feedback.docs[0]
    code = pending['confirmationCode']

    # Worker A claims
    status_a, claimed_a = service.pending.claim(code, 'wangwu')
    token_a = claimed_a['applyToken']

    # Create prepared operation
    service.feedback.apply_feedback(
        repo=context['repo'], job=context['job'], branch=context['branch'],
        build_number=context['buildNumber'],
        failure_id=pending['feedbackContext'].get('failureId'),
        failure_signature=pending['feedbackContext'].get('failureSignature'),
        action='confirm_owner', source='wecom_bot',
        operation_id=pending['operationId'], is_committed=False,
    )

    # Expire lease; worker B re-claims
    claimed_a['applyLeaseUntil'] = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1)
    status_b, claimed_b = service.pending.claim(code, 'wangwu')
    token_b = claimed_b['applyToken']

    # Worker A's mark_applied fails (old token)
    result = service.pending.mark_applied(claimed_a, token_a)
    assert result is False

    # Worker A cannot activate operation (pending is still applying, not applied)
    activation = service.pending.activate_operation_if_pending_applied(
        pending['confirmationCode'], pending['operationId'], service.feedback.collection,
    )
    assert activation == 'not_applied', f'expected not_applied, got {activation}'

    # Prepared operation remains invisible
    visible = service.feedback.list_feedback(
        repo=context['repo'], job=context['job'], branch=context['branch'],
        build_number=context['buildNumber'],
    )
    assert len(visible) == 0

def test_new_worker_adopts_prepared_operation_and_commits_once(monkeypatch):
    """Worker B claims, re-validates context, adopts prepared op via service.handle()."""
    store, _, context = _setup()
    service = WeComFeedbackService(store)
    service.handle(_message('msg:create', context['code'] + ' 1 \u5224\u65ad\u6b63\u786e'))
    pending = store.wecom_pending_feedback.docs[0]
    code = pending['confirmationCode']

    # Worker A claims
    status_a, claimed_a = service.pending.claim(code, 'wangwu')
    token_a = claimed_a['applyToken']
    assert status_a == 'claimed'

    # Create prepared operation (simulating apply_feedback success)
    service.feedback.apply_feedback(
        repo=context['repo'], job=context['job'], branch=context['branch'],
        build_number=context['buildNumber'],
        failure_id=pending['feedbackContext'].get('failureId'),
        failure_signature=pending['feedbackContext'].get('failureSignature'),
        action='confirm_owner', source='wecom_bot',
        operation_id=pending['operationId'], is_committed=False,
    )

    # Expire lease; worker B claims via service.handle()
    claimed_a['applyLeaseUntil'] = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1)

    # Track calls on service.pending
    mark_applied_calls = []
    original_mark = service.pending.mark_applied
    def tracking_mark(pending_doc, token):
        mark_applied_calls.append(1)
        return original_mark(pending_doc, token)
    monkeypatch.setattr(service.pending, 'mark_applied', tracking_mark)

    activate_calls = []
    original_activate = service.pending.activate_operation_if_pending_applied
    def tracking_activate(confirmation_code, operation_id, feedback_collection):
        activate_calls.append(1)
        return original_activate(confirmation_code, operation_id, feedback_collection)
    monkeypatch.setattr(service.pending, 'activate_operation_if_pending_applied', tracking_activate)

    reply = service.handle(_message('msg:confirm', '\u786e\u8ba4 ' + code))
    assert '\u5df2\u63d0\u4ea4' in reply

    assert mark_applied_calls == [1], 'mark_applied must be called exactly once'
    assert activate_calls == [1], 'activate must be called exactly once'

    assert store.wecom_pending_feedback.docs[0]['status'] == 'applied'
    ops = [d for d in store.feedback.docs if d.get('recordType') == 'operation']
    assert len(ops) == 1
    assert ops[0].get('isCommitted') == True
def test_stale_retry_does_not_commit_prepared_operation():
    """When context changed, claimed worker marks stale via service, prepared op stays uncommitted."""
    store = make_store()
    first_notice = _notice_with_item(failure_id="failure-old", title="\u65e7\u5931\u8d25")
    store.upsert_notice_snapshot(first_notice, source="test")
    contexts = FeedbackContextStore(store)
    context = contexts.get_or_create_for_notice(first_notice)
    service = WeComFeedbackService(store)
    service.handle(_message('msg:create', context['code'] + ' 1 \u5224\u65ad\u6b63\u786e'))
    pending = store.wecom_pending_feedback.docs[0]
    code = pending['confirmationCode']

    # Worker A claims
    status_a, claimed_a = service.pending.claim(code, 'wangwu')
    assert status_a == 'claimed'

    # Create prepared operation
    service.feedback.apply_feedback(
        repo=context['repo'], job=context['job'], branch=context['branch'],
        build_number=context['buildNumber'],
        failure_id=pending['feedbackContext'].get('failureId'),
        failure_signature=pending['feedbackContext'].get('failureSignature'),
        action='confirm_owner', source='wecom_bot',
        operation_id=pending['operationId'], is_committed=False,
    )

    # Expire lease; change context
    claimed_a['applyLeaseUntil'] = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1)
    second_notice = _notice_with_item(failure_id="failure-new", title="\u65b0\u5931\u8d25")
    store.upsert_notice_snapshot(second_notice, source="test")
    contexts.get_or_create_for_notice(second_notice)

    # Worker B claims via service.handle()
    reply = service.handle(_message('msg:confirm', '\u786e\u8ba4 ' + code))
    assert '\u8d23\u4efb\u9879\u5df2\u7ecf\u66f4\u65b0' in reply

    assert store.wecom_pending_feedback.docs[0]['status'] == 'stale'
    ops = [d for d in store.feedback.docs if d.get('recordType') == 'operation']
    assert len(ops) == 1
    assert ops[0].get('isCommitted') == False
    # Prepared operation must not appear in list_feedback
    visible = service.feedback.list_feedback(
        repo=context['repo'], job=context['job'], branch=context['branch'],
        build_number=context['buildNumber'],
    )
    assert len(visible) == 0
def test_pending_applied_recovers_uncommitted_operation():
    """When pending is applied but operation is prepared (crash recovery), second confirm commits it."""
    store, _, context = _setup()
    service = WeComFeedbackService(store)
    service.handle(_message('msg:create', context['code'] + ' 1 \u5224\u65ad\u6b63\u786e'))
    pending = store.wecom_pending_feedback.docs[0]
    code = pending['confirmationCode']

    # Manually set pending to applied and create prepared operation
    pending['status'] = 'applied'
    pending['applyToken'] = None
    pending['applyLeaseUntil'] = None
    service.feedback.apply_feedback(
        repo=context['repo'], job=context['job'], branch=context['branch'],
        build_number=context['buildNumber'],
        failure_id=pending['feedbackContext'].get('failureId'),
        failure_signature=pending['feedbackContext'].get('failureSignature'),
        action='confirm_owner', source='wecom_bot',
        operation_id=pending['operationId'], is_committed=False,
    )
    assert [d for d in store.feedback.docs if d.get('recordType') == 'operation'][0].get('isCommitted') == False

    # Second confirm recovers by committing
    reply = service.handle(_message('msg:confirm', '\u786e\u8ba4 ' + code))
    assert '\u5df2\u63d0\u4ea4' in reply

    ops = [d for d in store.feedback.docs if d.get('recordType') == 'operation']
    assert len(ops) == 1
    assert ops[0].get('isCommitted') == True


def test_activation_failure_is_retried_on_next_confirmation(monkeypatch):
    """When activate_operation_if_pending_applied fails on first try, next confirmation retries and succeeds."""
    store, _, context = _setup()
    service = WeComFeedbackService(store)
    service.handle(_message('msg:create', context['code'] + ' 1 \u5224\u65ad\u6b63\u786e'))
    pending = store.wecom_pending_feedback.docs[0]
    code = pending['confirmationCode']

    activate_calls = []
    original_activate = service.pending.activate_operation_if_pending_applied
    def failing_activate(confirmation_code, operation_id, feedback_collection):
        activate_calls.append(1)
        if len(activate_calls) == 1:
            return 'not_applied'
        return original_activate(confirmation_code, operation_id, feedback_collection)
    monkeypatch.setattr(service.pending, 'activate_operation_if_pending_applied', failing_activate)

    reply1 = service.handle(_message('msg:confirm1', '\u786e\u8ba4 ' + code))
    assert '\u6b63\u5728\u540c\u6b65' in reply1
    assert store.wecom_pending_feedback.docs[0]['status'] == 'applied'

    # Second confirm retries activation
    reply2 = service.handle(_message('msg:confirm2', '\u786e\u8ba4 ' + code))
    assert '\u5df2\u63d0\u4ea4' in reply2
    assert len(activate_calls) == 2

    ops = [d for d in store.feedback.docs if d.get('recordType') == 'operation']
    assert len(ops) == 1
    assert ops[0].get('isCommitted') == True


def test_operation_precheck_exception_does_not_escape_service(monkeypatch):
    """When find_by_operation_id raises in pre-check, service returns safe message."""
    store, _, context = _setup()
    service = WeComFeedbackService(store)
    service.handle(_message('msg:create', context['code'] + ' 1 \u5224\u65ad\u6b63\u786e'))
    pending = store.wecom_pending_feedback.docs[0]
    code = pending['confirmationCode']

    monkeypatch.setattr(service.feedback, 'find_by_operation_id',
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError('mongo down')))

    reply = service.handle(_message('msg:confirm', '\u786e\u8ba4 ' + code))
    assert '\u5904\u7406\u4e2d' in reply or '\u6b63\u5728\u5904\u7406' in reply or '\u6b63\u5728\u786e\u8ba4' in reply
    assert store.wecom_pending_feedback.docs[0]['status'] == 'applying'


def test_reconcile_exception_does_not_escape_service(monkeypatch):
    """When reconcile_applied raises on a committed operation, service returns safe message."""
    store, _, context = _setup()
    service = WeComFeedbackService(store)
    service.handle(_message('msg:create', context['code'] + ' 1 \u5224\u65ad\u6b63\u786e'))
    pending = store.wecom_pending_feedback.docs[0]
    code = pending['confirmationCode']

    # Claim and create a committed operation
    status, claimed = service.pending.claim(code, 'wangwu')
    assert status == 'claimed'
    service.feedback.apply_feedback(
        repo=context['repo'], job=context['job'], branch=context['branch'],
        build_number=context['buildNumber'],
        failure_id=pending['feedbackContext'].get('failureId'),
        failure_signature=pending['feedbackContext'].get('failureSignature'),
        action='confirm_owner', source='wecom_bot',
        operation_id=pending['operationId'], is_committed=True,
    )

    # Make lease expire so next confirm can re-claim
    claimed['applyLeaseUntil'] = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1)

    # reconcile_applied raises
    reconcile_calls = []
    monkeypatch.setattr(service.pending, 'reconcile_applied',
                        lambda *a, **kw: reconcile_calls.append(1) or (_ for _ in ()).throw(RuntimeError('reconcile failed')))

    apply_feedback_calls = []
    monkeypatch.setattr(service.feedback, 'apply_feedback',
                        lambda **kw: apply_feedback_calls.append(1) or (_ for _ in ()).throw(RuntimeError('should not be called')))

    reply = service.handle(_message('msg:confirm2', '\u786e\u8ba4 ' + code))
    assert len(reconcile_calls) == 1, 'reconcile must be called exactly once'
    assert len(apply_feedback_calls) == 0, 'apply_feedback must not be called again'
    assert '\u5904\u7406\u4e2d' in reply or '\u6b63\u5728\u5904\u7406' in reply or '\u6b63\u5728\u786e\u8ba4' in reply

    # Operation stays committed
    ops = [d for d in store.feedback.docs if d.get('recordType') == 'operation']
    assert ops[0].get('isCommitted') is True

def test_mark_failed_exception_keeps_uncertain_state(monkeypatch):
    """When mark_failed raises, pending stays applying, no crash."""
    store, _, context = _setup()
    service = WeComFeedbackService(store)
    service.handle(_message('msg:create', context['code'] + ' 1 \u5224\u65ad\u6b63\u786e'))
    pending = store.wecom_pending_feedback.docs[0]
    code = pending['confirmationCode']

    # Make apply_feedback raise (no operation inserted)
    monkeypatch.setattr(service.feedback, 'apply_feedback',
                        lambda **kw: (_ for _ in ()).throw(RuntimeError('apply failed')))

    # Make mark_failed also raise
    monkeypatch.setattr(service.pending, 'mark_failed',
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError('mark_failed failed')))

    reply = service.handle(_message('msg:confirm', '\u786e\u8ba4 ' + code))
    assert '\u5904\u7406\u4e2d' in reply or '\u6b63\u5728\u5904\u7406' in reply or '\u6b63\u5728\u786e\u8ba4' in reply
    assert store.wecom_pending_feedback.docs[0]['status'] == 'applying'


# --- Regression: operation activation constraints ---

def test_old_worker_cannot_activate_prepared_operation_after_lease_reclaim():
    """Old worker cannot activate operation after losing lease; activate_operation_if_pending_applied returns not_applied."""
    store, _, context = _setup()
    service = WeComFeedbackService(store)
    service.handle(_message('msg:create', context['code'] + ' 1 \u5224\u65ad\u6b63\u786e'))
    pending = store.wecom_pending_feedback.docs[0]
    code = pending['confirmationCode']

    # Worker A claims
    status_a, claimed_a = service.pending.claim(code, 'wangwu')
    token_a = claimed_a['applyToken']

    # Create prepared operation
    service.feedback.apply_feedback(
        repo=context['repo'], job=context['job'], branch=context['branch'],
        build_number=context['buildNumber'],
        failure_id=pending['feedbackContext'].get('failureId'),
        failure_signature=pending['feedbackContext'].get('failureSignature'),
        action='confirm_owner', source='wecom_bot',
        operation_id=pending['operationId'], is_committed=False,
    )

    # Expire lease; worker B re-claims
    claimed_a['applyLeaseUntil'] = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1)
    status_b, claimed_b = service.pending.claim(code, 'wangwu')
    assert status_b == 'claimed'
    token_b = claimed_b['applyToken']
    assert token_b != token_a

    # Worker A cannot mark_applied (old token)
    result = service.pending.mark_applied(claimed_a, token_a)
    assert result == False

    # Old worker cannot activate operation (pending is still applying, not applied)
    activation = service.pending.activate_operation_if_pending_applied(
        pending['confirmationCode'], pending['operationId'], service.feedback.collection,
    )
    assert activation == 'not_applied', f'expected not_applied, got {activation}'

    # Operation still not committed
    ops = [d for d in store.feedback.docs if d.get('recordType') == 'operation']
    assert len(ops) == 1
    assert ops[0].get('isCommitted') == False

    # Prepared operation not visible
    visible = service.feedback.list_feedback(
        repo=context['repo'], job=context['job'], branch=context['branch'],
        build_number=context['buildNumber'],
    )
    assert len(visible) == 0
def test_operation_activation_is_idempotent():
    """First activation returns committed, second returns already_committed; only one operation."""
    store, _, context = _setup()
    service = WeComFeedbackService(store)
    service.handle(_message('msg:create', context['code'] + ' 1 判断正确'))
    pending = store.wecom_pending_feedback.docs[0]

    # Create a prepared operation (isCommitted=False)
    service.feedback.apply_feedback(
        repo=context['repo'], job=context['job'], branch=context['branch'],
        build_number=context['buildNumber'],
        failure_id=pending['feedbackContext'].get('failureId'),
        failure_signature=pending['feedbackContext'].get('failureSignature'),
        action='confirm_owner', source='wecom_bot',
        operation_id=pending['operationId'], is_committed=False,
    )
    ops_before = [d for d in store.feedback.docs if d.get('recordType') == 'operation']
    assert len(ops_before) == 1
    assert ops_before[0].get('isCommitted') == False

    # Mark pending as applied via real production path
    code = pending['confirmationCode']
    status, claimed = service.pending.claim(code, 'wangwu')
    assert status == 'claimed'
    mark_ok = service.pending.mark_applied(claimed, claimed['applyToken'])
    assert mark_ok == True
    assert store.wecom_pending_feedback.docs[0]['status'] == 'applied'

    # First activation: should commit
    activation1 = service.pending.activate_operation_if_pending_applied(
        pending['confirmationCode'], pending['operationId'], service.feedback.collection,
    )
    assert activation1 == 'committed', f'expected committed, got {activation1}'

    # Second activation: should return already_committed
    activation2 = service.pending.activate_operation_if_pending_applied(
        pending['confirmationCode'], pending['operationId'], service.feedback.collection,
    )
    assert activation2 == 'already_committed', f'expected already_committed, got {activation2}'

    # Only one operation, now committed
    ops = [d for d in store.feedback.docs if d.get('recordType') == 'operation']
    assert len(ops) == 1
    assert ops[0].get('isCommitted') == True

    # Visible in list_feedback
    visible = service.feedback.list_feedback(
        repo=context['repo'], job=context['job'], branch=context['branch'],
        build_number=context['buildNumber'],
    )
    assert len(visible) == 1

def test_claimed_worker_marks_stale_without_committing_prepared_operation():
    """Worker B claims via service, finds stale context, marks stale without committing prepared op."""
    store = make_store()
    first_notice = _notice_with_item(failure_id="failure-old", title="\u65e7\u5931\u8d25")
    store.upsert_notice_snapshot(first_notice, source="test")
    contexts = FeedbackContextStore(store)
    context = contexts.get_or_create_for_notice(first_notice)
    service = WeComFeedbackService(store)
    service.handle(_message('msg:create', context['code'] + ' 1 \u5224\u65ad\u6b63\u786e'))
    pending = store.wecom_pending_feedback.docs[0]
    code = pending['confirmationCode']

    # Worker A claims
    status_a, claimed_a = service.pending.claim(code, 'wangwu')
    assert status_a == 'claimed'

    # Create prepared operation
    service.feedback.apply_feedback(
        repo=context['repo'], job=context['job'], branch=context['branch'],
        build_number=context['buildNumber'],
        failure_id=pending['feedbackContext'].get('failureId'),
        failure_signature=pending['feedbackContext'].get('failureSignature'),
        action='confirm_owner', source='wecom_bot',
        operation_id=pending['operationId'], is_committed=False,
    )

    # Expire lease; change context
    claimed_a['applyLeaseUntil'] = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1)
    second_notice = _notice_with_item(failure_id="failure-new", title="\u65b0\u5931\u8d25")
    store.upsert_notice_snapshot(second_notice, source="test")
    contexts.get_or_create_for_notice(second_notice)

    # Worker B claims via service.handle()
    reply = service.handle(_message('msg:confirm', '\u786e\u8ba4 ' + code))
    assert '\u8d23\u4efb\u9879\u5df2\u7ecf\u66f4\u65b0' in reply

    assert store.wecom_pending_feedback.docs[0]['status'] == 'stale'
    ops = [d for d in store.feedback.docs if d.get('recordType') == 'operation']
    assert len(ops) == 1
    assert ops[0].get('isCommitted') == False

    # Prepared operation not visible
    visible = service.feedback.list_feedback(
        repo=context['repo'], job=context['job'], branch=context['branch'],
        build_number=context['buildNumber'],
    )
    assert len(visible) == 0



# --- Regression: exception safety in prepared operation adoption ---

def test_adopt_prepared_context_lookup_exception_does_not_escape(monkeypatch):
    """Context lookup exception during prepared operation adoption does not escape service."""
    store, _, context = _setup()
    service = WeComFeedbackService(store)
    service.handle(_message('msg:create', context['code'] + ' 1 判断正确'))
    pending = store.wecom_pending_feedback.docs[0]
    code = pending['confirmationCode']

    # Worker A claims
    status_a, claimed_a = service.pending.claim(code, 'wangwu')
    assert status_a == 'claimed'

    # Create prepared operation
    service.feedback.apply_feedback(
        repo=context['repo'], job=context['job'], branch=context['branch'],
        build_number=context['buildNumber'],
        failure_id=pending['feedbackContext'].get('failureId'),
        failure_signature=pending['feedbackContext'].get('failureSignature'),
        action='confirm_owner', source='wecom_bot',
        operation_id=pending['operationId'], is_committed=False,
    )

    # Expire lease
    claimed_a['applyLeaseUntil'] = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1)

    # Worker B claims via service.handle(), but context lookup raises
    monkeypatch.setattr(service.contexts, 'resolve_item',
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError('mongo down')))

    mark_stale_calls = []
    monkeypatch.setattr(service.pending, 'mark_stale', lambda *a, **kw: mark_stale_calls.append(1) or False)

    activate_calls = []
    original_activate = service.pending.activate_operation_if_pending_applied
    def tracking_activate(confirmation_code, operation_id, feedback_collection):
        activate_calls.append(1)
        return original_activate(confirmation_code, operation_id, feedback_collection)
    monkeypatch.setattr(service.pending, 'activate_operation_if_pending_applied', tracking_activate)

    reply = service.handle(_message('msg:confirm', '确认 ' + code))

    assert '正在确认' in reply
    assert len(mark_stale_calls) == 0, 'mark_stale must not be called on DB error'
    assert len(activate_calls) == 0, 'activate must not be called on DB error'
    assert store.wecom_pending_feedback.docs[0]['status'] == 'applying'
    ops = [d for d in store.feedback.docs if d.get('recordType') == 'operation']
    assert ops[0].get('isCommitted') == False


def test_adopt_prepared_mark_stale_exception_does_not_escape(monkeypatch):
    """mark_stale exception during prepared operation adoption does not escape service."""
    store, _, context = _setup()
    service = WeComFeedbackService(store)
    service.handle(_message('msg:create', context['code'] + ' 1 判断正确'))
    pending = store.wecom_pending_feedback.docs[0]
    code = pending['confirmationCode']

    # Worker A claims
    status_a, claimed_a = service.pending.claim(code, 'wangwu')
    assert status_a == 'claimed'

    # Create prepared operation
    service.feedback.apply_feedback(
        repo=context['repo'], job=context['job'], branch=context['branch'],
        build_number=context['buildNumber'],
        failure_id=pending['feedbackContext'].get('failureId'),
        failure_signature=pending['feedbackContext'].get('failureSignature'),
        action='confirm_owner', source='wecom_bot',
        operation_id=pending['operationId'], is_committed=False,
    )

    # Expire lease; change context to make it stale
    claimed_a['applyLeaseUntil'] = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1)
    # Monkeypatch resolve_item to return different item (stale)
    original_resolve = service.contexts.resolve_item
    def stale_resolve(code, idx):
        current_context, current_item = original_resolve(code, idx)
        # Change failureId to simulate stale
        current_item = dict(current_item)
        current_item['failureId'] = 'different-failure'
        return current_context, current_item
    monkeypatch.setattr(service.contexts, 'resolve_item', stale_resolve)

    # mark_stale raises
    monkeypatch.setattr(service.pending, 'mark_stale',
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError('mark_stale failed')))

    activate_calls = []
    original_activate = service.pending.activate_operation_if_pending_applied
    def tracking_activate(confirmation_code, operation_id, feedback_collection):
        activate_calls.append(1)
        return original_activate(confirmation_code, operation_id, feedback_collection)
    monkeypatch.setattr(service.pending, 'activate_operation_if_pending_applied', tracking_activate)

    reply = service.handle(_message('msg:confirm', '确认 ' + code))

    assert '正在确认' in reply
    assert len(activate_calls) == 0, 'activate must not be called when mark_stale fails'
    ops = [d for d in store.feedback.docs if d.get('recordType') == 'operation']
    assert ops[0].get('isCommitted') == False
    # Must not say "responsibility updated" since the write did not succeed
    assert '责任项已经更新' not in reply


def test_adopt_prepared_mark_stale_cas_failure_uses_real_status(monkeypatch):
    """When mark_stale CAS fails, service reads real pending status and responds accordingly."""
    store, _, context = _setup()
    service = WeComFeedbackService(store)
    service.handle(_message('msg:create', context['code'] + ' 1 \u5224\u65ad\u6b63\u786e'))
    pending = store.wecom_pending_feedback.docs[0]
    code = pending['confirmationCode']

    # Worker A claims
    status_a, claimed_a = service.pending.claim(code, 'wangwu')
    assert status_a == 'claimed'

    # Create prepared operation
    service.feedback.apply_feedback(
        repo=context['repo'], job=context['job'], branch=context['branch'],
        build_number=context['buildNumber'],
        failure_id=pending['feedbackContext'].get('failureId'),
        failure_signature=pending['feedbackContext'].get('failureSignature'),
        action='confirm_owner', source='wecom_bot',
        operation_id=pending['operationId'], is_committed=False,
    )

    # Expire lease; change context to make it stale
    claimed_a['applyLeaseUntil'] = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1)
    original_resolve = service.contexts.resolve_item
    def stale_resolve(code, idx):
        current_context, current_item = original_resolve(code, idx)
        current_item = dict(current_item)
        current_item['failureId'] = 'different-failure'
        return current_context, current_item
    monkeypatch.setattr(service.contexts, 'resolve_item', stale_resolve)

    # mark_stale returns False, and another worker already wrote stale
    mark_stale_calls = []
    def fake_mark_stale(doc, token, reason):
        mark_stale_calls.append(1)
        # Simulate another worker writing stale before us
        store.wecom_pending_feedback.docs[0]['status'] = 'stale'
        return False
    monkeypatch.setattr(service.pending, 'mark_stale', fake_mark_stale)

    reply = service.handle(_message('msg:confirm', '\u786e\u8ba4 ' + code))
    assert len(mark_stale_calls) == 1, 'mark_stale must be called exactly once'
    assert '\u8d23\u4efb\u9879\u5df2\u7ecf\u66f4\u65b0' in reply or '\u5df2\u7ecf\u5904\u7406' in reply
    ops = [d for d in store.feedback.docs if d.get('recordType') == 'operation']
    assert ops[0].get('isCommitted') is False


def test_adopt_prepared_mark_stale_cas_failure_still_applying(monkeypatch):
    """When mark_stale CAS fails and pending is still applying, service returns processing."""
    store, _, context = _setup()
    service = WeComFeedbackService(store)
    service.handle(_message('msg:create', context['code'] + ' 1 \u5224\u65ad\u6b63\u786e'))
    pending = store.wecom_pending_feedback.docs[0]
    code = pending['confirmationCode']

    # Worker A claims
    status_a, claimed_a = service.pending.claim(code, 'wangwu')
    assert status_a == 'claimed'

    # Create prepared operation
    service.feedback.apply_feedback(
        repo=context['repo'], job=context['job'], branch=context['branch'],
        build_number=context['buildNumber'],
        failure_id=pending['feedbackContext'].get('failureId'),
        failure_signature=pending['feedbackContext'].get('failureSignature'),
        action='confirm_owner', source='wecom_bot',
        operation_id=pending['operationId'], is_committed=False,
    )

    # Expire lease; change context to make it stale
    claimed_a['applyLeaseUntil'] = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1)
    original_resolve = service.contexts.resolve_item
    def stale_resolve(code, idx):
        current_context, current_item = original_resolve(code, idx)
        current_item = dict(current_item)
        current_item['failureId'] = 'different-failure'
        return current_context, current_item
    monkeypatch.setattr(service.contexts, 'resolve_item', stale_resolve)

    # mark_stale returns False, pending stays applying
    mark_stale_calls = []
    monkeypatch.setattr(service.pending, 'mark_stale', lambda *a, **kw: mark_stale_calls.append(1) or False)

    reply = service.handle(_message('msg:confirm', '\u786e\u8ba4 ' + code))
    assert len(mark_stale_calls) == 1, 'mark_stale must be called exactly once'
    assert '\u6b63\u5728\u5904\u7406' in reply or '\u6b63\u5728\u534f\u8c03' in reply
    ops = [d for d in store.feedback.docs if d.get('recordType') == 'operation']
    assert ops[0].get('isCommitted') is False


def test_adopt_prepared_mark_stale_cas_failure_pending_read_error(monkeypatch):
    """When mark_stale CAS fails and pending read also fails, service returns safe message."""
    store, _, context = _setup()
    service = WeComFeedbackService(store)
    service.handle(_message('msg:create', context['code'] + ' 1 \u5224\u65ad\u6b63\u786e'))
    pending = store.wecom_pending_feedback.docs[0]
    code = pending['confirmationCode']

    # Worker A claims
    status_a, claimed_a = service.pending.claim(code, 'wangwu')
    assert status_a == 'claimed'

    # Create prepared operation
    service.feedback.apply_feedback(
        repo=context['repo'], job=context['job'], branch=context['branch'],
        build_number=context['buildNumber'],
        failure_id=pending['feedbackContext'].get('failureId'),
        failure_signature=pending['feedbackContext'].get('failureSignature'),
        action='confirm_owner', source='wecom_bot',
        operation_id=pending['operationId'], is_committed=False,
    )

    # Expire lease; change context to make it stale
    claimed_a['applyLeaseUntil'] = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1)
    original_resolve = service.contexts.resolve_item
    def stale_resolve(code, idx):
        current_context, current_item = original_resolve(code, idx)
        current_item = dict(current_item)
        current_item['failureId'] = 'different-failure'
        return current_context, current_item
    monkeypatch.setattr(service.contexts, 'resolve_item', stale_resolve)

    # mark_stale returns False
    mark_stale_calls = []
    monkeypatch.setattr(service.pending, 'mark_stale', lambda *a, **kw: mark_stale_calls.append(1) or False)

    # Replace _safe_read_pending to simulate read failure after mark_stale CAS fails
    original_safe_read = service._safe_read_pending
    # Replace _safe_read_pending to simulate read failure after mark_stale CAS fails
    def failing_safe_read(p):
        if mark_stale_calls:
            return {}  # simulate read failure, defaults to "applying"
        return original_safe_read(p)
    monkeypatch.setattr(service, '_safe_read_pending', failing_safe_read)

    reply = service.handle(_message('msg:confirm', '\u786e\u8ba4 ' + code))
    assert len(mark_stale_calls) == 1, 'mark_stale must be called exactly once'
    assert '\u5904\u7406\u4e2d' in reply or '\u6b63\u5728\u5904\u7406' in reply or '\u6b63\u5728\u786e\u8ba4' in reply

def test_normal_flow_context_lookup_exception_does_not_mark_stale(monkeypatch):
    """Context lookup exception in normal flow: pending stays applying, no mark_stale, no mark_failed."""
    store, _, context = _setup()
    service = WeComFeedbackService(store)
    service.handle(_message('msg:create', context['code'] + ' 1 \u5224\u65ad\u6b63\u786e'))
    pending = store.wecom_pending_feedback.docs[0]
    code = pending['confirmationCode']

    # Make resolve_item raise (MongoDB failure, NOT stale)
    monkeypatch.setattr(service.contexts, 'resolve_item',
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError('mongo down')))

    mark_stale_calls = []
    monkeypatch.setattr(service.pending, 'mark_stale', lambda *a, **kw: mark_stale_calls.append(1) or False)

    mark_failed_calls = []
    monkeypatch.setattr(service.pending, 'mark_failed', lambda *a, **kw: mark_failed_calls.append(1) or False)

    apply_feedback_calls = []
    monkeypatch.setattr(service.feedback, 'apply_feedback',
                        lambda **kw: apply_feedback_calls.append(1) or (_ for _ in ()).throw(RuntimeError('should not be called')))

    reply = service.handle(_message('msg:confirm', '\u786e\u8ba4 ' + code))

    assert '\u5904\u7406\u4e2d' in reply or '\u6b63\u5728\u5904\u7406' in reply or '\u6b63\u5728\u786e\u8ba4' in reply
    assert store.wecom_pending_feedback.docs[0]['status'] == 'applying'
    assert len(mark_stale_calls) == 0, 'mark_stale must not be called on DB error'
    assert len(mark_failed_calls) == 0, 'mark_failed must not be called on DB error'
    assert len(apply_feedback_calls) == 0, 'apply_feedback must not be called on DB error'
    ops = [d for d in store.feedback.docs if d.get('recordType') == 'operation']
    assert len(ops) == 0


def test_normal_flow_context_lookup_recovers_after_lease_expiry(monkeypatch):
    """After context lookup failure, lease expires; next confirm with restored resolve_item succeeds."""
    store, _, context = _setup()
    service = WeComFeedbackService(store)
    service.handle(_message('msg:create', context['code'] + ' 1 \u5224\u65ad\u6b63\u786e'))
    pending = store.wecom_pending_feedback.docs[0]
    code = pending['confirmationCode']

    # First confirm: resolve_item raises, pending stays applying
    original_resolve = service.contexts.resolve_item
    monkeypatch.setattr(service.contexts, 'resolve_item',
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError('mongo down')))
    reply1 = service.handle(_message('msg:confirm1', '\u786e\u8ba4 ' + code))
    assert '\u5904\u7406\u4e2d' in reply1 or '\u6b63\u5728\u5904\u7406' in reply1 or '\u6b63\u5728\u786e\u8ba4' in reply1
    assert store.wecom_pending_feedback.docs[0]['status'] == 'applying'

    # Expire lease, restore resolve_item
    store.wecom_pending_feedback.docs[0]['applyLeaseUntil'] = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1)
    monkeypatch.setattr(service.contexts, 'resolve_item', original_resolve)

    # Second confirm succeeds
    reply2 = service.handle(_message('msg:confirm2', '\u786e\u8ba4 ' + code))
    assert '\u5df2\u63d0\u4ea4' in reply2
    assert store.wecom_pending_feedback.docs[0]['status'] == 'applied'
    assert store.wecom_pending_feedback.docs[0]['applyAttemptCount'] == 2
    ops = [d for d in store.feedback.docs if d.get('recordType') == 'operation']
    assert len(ops) == 1
    assert ops[0].get('isCommitted') is True

def test_normal_flow_mark_stale_exception_does_not_escape(monkeypatch):
    """mark_stale exception in normal flow does not escape; no committed operation created."""
    store, _, context = _setup()
    service = WeComFeedbackService(store)
    service.handle(_message('msg:create', context['code'] + ' 1 \u5224\u65ad\u6b63\u786e'))
    pending = store.wecom_pending_feedback.docs[0]
    code = pending['confirmationCode']

    # Make resolve_item return different item (stale)
    original_resolve = service.contexts.resolve_item
    def stale_resolve(code, idx):
        current_context, current_item = original_resolve(code, idx)
        current_item = dict(current_item)
        current_item['failureId'] = 'different-failure'
        return current_context, current_item
    monkeypatch.setattr(service.contexts, 'resolve_item', stale_resolve)

    # mark_stale raises
    mark_stale_calls = []
    monkeypatch.setattr(service.pending, 'mark_stale',
                        lambda *a, **kw: mark_stale_calls.append(1) or (_ for _ in ()).throw(RuntimeError('mark_stale failed')))

    apply_feedback_calls = []
    monkeypatch.setattr(service.feedback, 'apply_feedback',
                        lambda **kw: apply_feedback_calls.append(1) or (_ for _ in ()).throw(RuntimeError('should not be called')))

    reply = service.handle(_message('msg:confirm', '\u786e\u8ba4 ' + code))

    assert len(mark_stale_calls) == 1, 'mark_stale must be called'
    assert len(apply_feedback_calls) == 0, 'apply_feedback must not be called'
    assert '\u5904\u7406\u4e2d' in reply or '\u6b63\u5728\u5904\u7406' in reply or '\u6b63\u5728\u786e\u8ba4' in reply
    assert '\u8d23\u4efb\u9879\u5df2\u7ecf\u66f4\u65b0' not in reply
    ops = [d for d in store.feedback.docs if d.get('recordType') == 'operation']
    assert len(ops) == 0


# --- Parameterized: activation requires pending.status == applied ---

import pytest as _pytest


@_pytest.mark.parametrize(
    "pending_status",
    ["pending", "applying", "stale", "failed", "cancelled", "expired"],
)
def test_non_applied_pending_cannot_activate_prepared_operation(pending_status):
    """Only pending.status == applied allows activation; all other statuses return not_applied."""
    store, _, context = _setup()
    service = WeComFeedbackService(store)
    service.handle(_message('msg:create', context['code'] + ' 1 \u5224\u65ad\u6b63\u786e'))
    pending = store.wecom_pending_feedback.docs[0]

    # Create a prepared operation
    service.feedback.apply_feedback(
        repo=context['repo'], job=context['job'], branch=context['branch'],
        build_number=context['buildNumber'],
        failure_id=pending['feedbackContext'].get('failureId'),
        failure_signature=pending['feedbackContext'].get('failureSignature'),
        action='confirm_owner', source='wecom_bot',
        operation_id=pending['operationId'], is_committed=False,
    )

    # Set pending to the target status
    store.wecom_pending_feedback.docs[0]['status'] = pending_status

    activation = service.pending.activate_operation_if_pending_applied(
        pending['confirmationCode'], pending['operationId'], service.feedback.collection,
    )
    assert activation == 'not_applied', f'status={pending_status}: expected not_applied, got {activation}'

    # Operation remains uncommitted
    ops = [d for d in store.feedback.docs if d.get('recordType') == 'operation']
    assert ops[0].get('isCommitted') is False

    # Not visible in list_feedback
    visible = service.feedback.list_feedback(
        repo=context['repo'], job=context['job'], branch=context['branch'],
        build_number=context['buildNumber'],
    )
    assert len(visible) == 0


def test_applied_pending_can_activate_prepared_operation():
    """pending.status == applied allows activation; committed on first call, already_committed on second."""
    store, _, context = _setup()
    service = WeComFeedbackService(store)
    service.handle(_message('msg:create', context['code'] + ' 1 \u5224\u65ad\u6b63\u786e'))
    pending = store.wecom_pending_feedback.docs[0]

    # Create a prepared operation
    service.feedback.apply_feedback(
        repo=context['repo'], job=context['job'], branch=context['branch'],
        build_number=context['buildNumber'],
        failure_id=pending['feedbackContext'].get('failureId'),
        failure_signature=pending['feedbackContext'].get('failureSignature'),
        action='confirm_owner', source='wecom_bot',
        operation_id=pending['operationId'], is_committed=False,
    )

    # Mark pending as applied via real production path
    code = pending['confirmationCode']
    status, claimed = service.pending.claim(code, 'wangwu')
    assert status == 'claimed'
    mark_ok = service.pending.mark_applied(claimed, claimed['applyToken'])
    assert mark_ok is True
    assert store.wecom_pending_feedback.docs[0]['status'] == 'applied'

    # First activation: committed
    activation1 = service.pending.activate_operation_if_pending_applied(
        pending['confirmationCode'], pending['operationId'], service.feedback.collection,
    )
    assert activation1 == 'committed'

    # Second activation: already_committed
    activation2 = service.pending.activate_operation_if_pending_applied(
        pending['confirmationCode'], pending['operationId'], service.feedback.collection,
    )
    assert activation2 == 'already_committed'

    # Only one operation, committed
    ops = [d for d in store.feedback.docs if d.get('recordType') == 'operation']
    assert len(ops) == 1
    assert ops[0].get('isCommitted') is True

    # Visible in list_feedback
    visible = service.feedback.list_feedback(
        repo=context['repo'], job=context['job'], branch=context['branch'],
        build_number=context['buildNumber'],
    )
    assert len(visible) == 1


def test_activate_with_mismatched_operation_id_returns_not_applied():
    """When pending.operationId != activation operationId, return not_applied."""
    store, _, context = _setup()
    service = WeComFeedbackService(store)
    service.handle(_message('msg:create', context['code'] + ' 1 \u5224\u65ad\u6b63\u786e'))
    pending = store.wecom_pending_feedback.docs[0]

    # Create a prepared operation with a different operationId
    import uuid
    other_id = uuid.uuid4().hex
    service.feedback.apply_feedback(
        repo=context['repo'], job=context['job'], branch=context['branch'],
        build_number=context['buildNumber'],
        failure_id=pending['feedbackContext'].get('failureId'),
        failure_signature=pending['feedbackContext'].get('failureSignature'),
        action='confirm_owner', source='wecom_bot',
        operation_id=other_id, is_committed=False,
    )

    # Mark pending as applied
    code = pending['confirmationCode']
    status, claimed = service.pending.claim(code, 'wangwu')
    assert status == 'claimed'
    service.pending.mark_applied(claimed, claimed['applyToken'])

    # Try to activate with the other operationId - pending has different operationId
    activation = service.pending.activate_operation_if_pending_applied(
        pending['confirmationCode'], other_id, service.feedback.collection,
    )
    assert activation == 'not_applied'


def test_activate_nonexistent_operation_returns_not_found():
    """When operation does not exist, return not_found."""
    store, _, context = _setup()
    service = WeComFeedbackService(store)
    service.handle(_message('msg:create', context['code'] + ' 1 \u5224\u65ad\u6b63\u786e'))
    pending = store.wecom_pending_feedback.docs[0]

    # Mark pending as applied (no operation created)
    code = pending['confirmationCode']
    status, claimed = service.pending.claim(code, 'wangwu')
    assert status == 'claimed'
    service.pending.mark_applied(claimed, claimed['applyToken'])

    activation = service.pending.activate_operation_if_pending_applied(
        pending['confirmationCode'], pending['operationId'], service.feedback.collection,
    )
    assert activation == 'not_found'
