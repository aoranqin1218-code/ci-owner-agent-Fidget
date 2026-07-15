with open("tests/test_wecom_feedback_service.py", "r", encoding="utf-8") as f:
    c = f.read()

# 1. Delete test_user_directory_exception_marks_pending_failed
old_test = '''def test_user_directory_exception_marks_pending_failed(monkeypatch):
    store, _, context = _setup()
    service = WeComFeedbackService(store)
    service.handle(_message("msg:create", f"{context['code']} 1 责任人改为 @Lisi-李四", mentions=(("lisi", "李四"),)))
    code = store.wecom_pending_feedback.docs[0]["confirmationCode"]
    monkeypatch.setattr(service.users, "search_users", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("directory failed")))
    assert "反馈写入失败" in service.handle(_message("msg:confirm", f"确认 {code}"))
    assert store.wecom_pending_feedback.docs[0]["status"] == "failed"


'''

if old_test in c:
    c = c.replace(old_test, "")
    print("Deleted test_user_directory_exception_marks_pending_failed")
else:
    print("FAIL: old test not found")

# 2. Add new test: correct_owner works without directory
new_test = '''def test_correct_owner_submits_without_directory_lookup():
    """correct_owner should use target_display_name and target_userid directly, no directory query."""
    store, _, context = _setup()
    service = WeComFeedbackService(store)
    service.handle(_message("msg:create", f"{context['code']} 1 责任人改为 @AoranQin-秦奥然"))
    code = store.wecom_pending_feedback.docs[0]["confirmationCode"]
    reply = service.handle(_message("msg:confirm", f"确认 {code}"))
    assert "反馈已提交" in reply
    assert store.feedback.docs[0]["correctedOwnerWeComUserId"] == "aoranqin"
    assert store.feedback.docs[0]["correctedOwner"]["name"] == "AoranQin-秦奥然"
    assert store.feedback.docs[0]["correctedOwner"]["email"] is None


'''

# Insert before test_correct_owner_keeps_real_userid_without_directory_entry
idx = c.find("def test_correct_owner_keeps_real_userid_without_directory_entry():")
if idx > 0:
    c = c[:idx] + new_test + c[idx:]
    print("Added test_correct_owner_submits_without_directory_lookup")
else:
    print("FAIL: insertion point not found")

with open("tests/test_wecom_feedback_service.py", "w", encoding="utf-8", newline="\n") as f:
    f.write(c)
print("Done")