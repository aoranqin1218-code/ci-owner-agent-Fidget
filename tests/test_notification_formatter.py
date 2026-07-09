from __future__ import annotations

import json
from dataclasses import replace

from ci_owner_agent.schemas import CiResponsibilityNotice
from ci_owner_agent.config import load_settings
from ci_owner_agent.main import _maybe_notify_notice, _notify_notice, main
from ci_owner_agent.services.history_store import notice_hash
from ci_owner_agent.services.notification_formatter import collect_responsible_display_names, format_wecom_markdown_notice, result_icon
from ci_owner_agent.services.wecom_user_mapping import WeComUserMapper
from tests.test_history_store import make_store


def notice_payload(items):
    return {
        "job": "services/fx-code-unittest",
        "buildNumber": 5099,
        "buildUrl": "https://jenkins.example/job/5099/",
        "result": "FAILURE",
        "branch": "dev",
        "headCommit": "h",
        "baseCommit": "b",
        "owner": {"type": "no_high_confidence_owner", "name": "无高可信责任人", "email": None, "commit": None, "confidence": 0},
        "failureReason": "本 build 包含多个失败，责任项见下方。",
        "evidence": [
            {"id": "E1", "type": "log", "summary": "日志显示 Webhook触发 Timeout", "detail": "detail", "source": "log"},
            {"id": "E2", "type": "diff", "summary": "", "detail": "diff 显示 webhook 相关逻辑变更" * 20, "source": "diff"},
        ],
        "responsibilityItems": items,
        "suggestions": [],
        "hasHighConfidenceOwner": False,
    }


def item(owner_name="Tang.Tangerine-唐嘉伟", owner_type="inherited_failure_owner", responsibility_type="inherited_failure_owner", reason="历史持续失败。", owner_email="x@example.com"):
    return {
        "failureId": "failure-secret",
        "failureTitle": "EtlUtils - getInputEntryInfo",
        "failureSignature": "sig-secret",
        "failureSummary": "summary",
        "owner": {"type": owner_type, "name": owner_name, "email": owner_email, "commit": "secretcommit", "confidence": 0.9},
        "responsibilityType": responsibility_type,
        "sourceBuildNumber": 5094 if responsibility_type == "inherited_failure_owner" else 5099,
        "sourceBuildUrl": None,
        "sourceCommit": "secret-source",
        "matchType": "signature_exact",
        "relationship": "very_likely_same_failure",
        "confidence": 0.9,
        "reason": reason,
        "evidenceIds": ["E1", "E2"],
    }


def test_inherited_item_owner_is_mentioned_without_top_level_conclusion():
    notice = CiResponsibilityNotice.model_validate(notice_payload([item()]))
    markdown = format_wecom_markdown_notice(notice, feedback_base_url="http://ci-agent.test/feedback")
    assert "### ❌ CI 单测失败 | services/fx-code-unittest #5099" in markdown
    assert "👤 **责任人**：@Tang.Tangerine-唐嘉伟" in markdown
    assert "🧭 **原因**：" in markdown
    assert "#### 🧩 责任项" in markdown
    assert "顶层结论" not in markdown
    assert "顶层结论：无高可信责任人" not in markdown


def test_multiple_responsible_names_are_deduplicated_in_order():
    notice = CiResponsibilityNotice.model_validate(
        notice_payload([item("Tang"), item("Tang"), item("Mars", "high_confidence", "current_build_owner", "新失败。", "mars@example.com")])
    )
    assert collect_responsible_display_names(notice) == ["Tang", "Mars"]
    assert "👤 **责任人**：@Tang、@Mars" in format_wecom_markdown_notice(notice)


def test_no_item_owner_displays_no_high_confidence_owner():
    no_owner = item("无高可信责任人", "no_high_confidence_owner", "no_high_confidence_owner", "证据不足。")
    notice = CiResponsibilityNotice.model_validate(notice_payload([no_owner]))
    assert "👤 **责任人**：无高可信责任人" in format_wecom_markdown_notice(notice)


def test_responsibility_type_labels_are_public_readable():
    current = item("Henry", "high_confidence", "current_build_owner", "新失败。", "henry@example.com")
    inherited = item("Tang", "inherited_failure_owner", "inherited_failure_owner", "历史持续。", "tang@example.com")
    no_owner = item("无高可信责任人", "no_high_confidence_owner", "no_high_confidence_owner", "证据不足。")
    notice = CiResponsibilityNotice.model_validate(notice_payload([current, inherited, no_owner]))

    markdown = format_wecom_markdown_notice(notice)

    assert "📌 **责任项**：共 3 项，当前引入 1，历史持续 1，待确认 1" in markdown
    assert "🔥 当前引入 | EtlUtils - getInputEntryInfo" in markdown
    assert "♻️ 历史持续 | EtlUtils - getInputEntryInfo" in markdown
    assert "❓ 待确认 | EtlUtils - getInputEntryInfo" in markdown
    assert "current_build_owner" not in markdown
    assert "inherited_failure_owner" not in markdown
    assert "no_high_confidence_owner" not in markdown


def test_top_responsible_owners_use_mapper_userid_mention(tmp_path):
    path = tmp_path / "mapping.csv"
    path.write_text(
        "mappingKey,authorName,authorEmail,normalizedEmail,wecomUserId,mappingStatus,note\n"
        "Tang <x@example.com>,Tang,x@example.com,x@example.com,tang.userid,manual,\n",
        encoding="utf-8",
    )
    mapper = WeComUserMapper.from_csv(path)
    notice = CiResponsibilityNotice.model_validate(notice_payload([item("Tang")]))
    markdown = format_wecom_markdown_notice(notice, user_mapper=mapper)
    assert "👤 **责任人**：<@tang.userid>" in markdown


def test_responsibility_item_owner_uses_mapper_userid_mention(tmp_path):
    path = tmp_path / "mapping.csv"
    path.write_text(
        "mappingKey,authorName,authorEmail,normalizedEmail,wecomUserId,mappingStatus,note\n"
        "Tang <x@example.com>,Tang,x@example.com,x@example.com,tang.userid,manual,\n",
        encoding="utf-8",
    )
    mapper = WeComUserMapper.from_csv(path)
    notice = CiResponsibilityNotice.model_validate(notice_payload([item("Tang")]))
    markdown = format_wecom_markdown_notice(notice, user_mapper=mapper)
    assert "   - 👤 责任人：<@tang.userid>" in markdown


def test_unmapped_owner_fallback_to_name_mention():
    notice = CiResponsibilityNotice.model_validate(notice_payload([item("Tang")]))
    markdown = format_wecom_markdown_notice(notice, user_mapper=WeComUserMapper([]))
    assert "👤 **责任人**：@Tang" in markdown
    assert "   - 👤 责任人：@Tang" in markdown


def test_mention_mode_name_does_not_use_userid(tmp_path):
    path = tmp_path / "mapping.csv"
    path.write_text(
        "mappingKey,authorName,authorEmail,normalizedEmail,wecomUserId,mappingStatus,note\n"
        "Tang <x@example.com>,Tang,x@example.com,x@example.com,tang.userid,manual,\n",
        encoding="utf-8",
    )
    mapper = WeComUserMapper.from_csv(path)
    notice = CiResponsibilityNotice.model_validate(notice_payload([item("Tang")]))
    markdown = format_wecom_markdown_notice(notice, user_mapper=mapper, mention_mode="name")
    assert "<@tang.userid>" not in markdown
    assert "👤 **责任人**：@Tang" in markdown


def test_system_fields_are_not_displayed():
    notice = CiResponsibilityNotice.model_validate(notice_payload([item(reason="failureId abc failureSignature def signatureHash ghi 签名")]))
    markdown = format_wecom_markdown_notice(notice)
    assert "failure-secret" not in markdown
    assert "sig-secret" not in markdown
    assert "failureId" not in markdown
    assert "failureSignature" not in markdown
    assert "signatureHash" not in markdown
    assert "签名" not in markdown
    assert "secretcommit" not in markdown
    assert "secret-source" not in markdown


def test_item_reason_is_used_as_evidence():
    notice = CiResponsibilityNotice.model_validate(notice_payload([item(reason="当前 failure item 与历史构建 #5094 一致。")]))
    assert "🔎 证据：当前 failure item 与历史构建 #5094 一致。" in format_wecom_markdown_notice(notice)


def test_empty_reason_falls_back_to_evidence_ids():
    notice = CiResponsibilityNotice.model_validate(
        notice_payload([item("Henry", "high_confidence", "current_build_owner", reason="")])
    )
    markdown = format_wecom_markdown_notice(notice)
    assert "日志显示 Webhook触发 Timeout" in markdown


def test_suggestions_show_first_three_items():
    payload = notice_payload([item()])
    payload["suggestions"] = ["检查测试数据准备逻辑。", "优先验证历史持续失败。", "补充单测覆盖。", "第四条不展示。"]
    notice = CiResponsibilityNotice.model_validate(payload)

    markdown = format_wecom_markdown_notice(notice)

    assert "#### 🛠️ 修复建议" in markdown
    assert "- 检查测试数据准备逻辑。" in markdown
    assert "- 优先验证历史持续失败。" in markdown
    assert "- 补充单测覆盖。" in markdown
    assert "第四条不展示" not in markdown


def test_suggestions_section_is_omitted_when_empty():
    notice = CiResponsibilityNotice.model_validate(notice_payload([item()]))

    assert "#### 🛠️ 修复建议" not in format_wecom_markdown_notice(notice)


def test_result_icons_and_labels():
    expected = {
        "SUCCESS": "### ✅ CI 单测成功",
        "FAILURE": "### ❌ CI 单测失败",
        "UNSTABLE": "### ⚠️ CI 单测不稳定",
        "ABORTED": "### ⏹️ CI 单测中止",
        "UNKNOWN": "### ❔ CI 单测未知",
        "OTHER": "### ❔ CI 单测未知",
    }
    for result, title in expected.items():
        payload = notice_payload([item()])
        payload["result"] = result
        notice = CiResponsibilityNotice.model_validate(payload)
        assert title in format_wecom_markdown_notice(notice)
    assert result_icon("failure") == "❌"


def test_build_and_feedback_links():
    notice = CiResponsibilityNotice.model_validate(notice_payload([item()]))
    markdown = format_wecom_markdown_notice(notice, feedback_base_url="http://ci-agent.xxx/feedback")
    assert "#### 🔗 相关链接" in markdown
    assert notice.buildUrl in markdown
    assert "🏗️ [查看 Jenkins 构建]" in markdown
    assert "📝 [提交反馈]" in markdown
    assert "job=services%2Ffx-code-unittest" in markdown
    assert "build=5099" in markdown
    assert "📝 反馈：未配置" in format_wecom_markdown_notice(notice, feedback_base_url=None)


def test_feedback_link_can_include_shared_token():
    notice = CiResponsibilityNotice.model_validate(notice_payload([item()]))
    markdown = format_wecom_markdown_notice(
        notice,
        feedback_base_url="http://ci-agent.xxx/feedback",
        feedback_token="dev-token",
    )
    assert "token=dev-token" in markdown


def test_notify_notice_dry_run_outputs_markdown(tmp_path, capsys, monkeypatch):
    notice = CiResponsibilityNotice.model_validate(notice_payload([item()]))
    notice_file = tmp_path / "notice.json"
    notice_file.write_text(notice.model_dump_json(), encoding="utf-8")
    monkeypatch.setenv("CI_AGENT_MODEL_PROVIDER", "fake")
    rc = main(["notify-notice", "--notice-file", str(notice_file), "--dry-run", "--feedback-base-url", "http://ci-agent.test/feedback"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "### ❌ CI 单测失败 | services/fx-code-unittest #5099" in out
    assert "#### 🔗 相关链接" in out


def test_notify_notice_dry_run_uses_mapping_file(tmp_path, capsys, monkeypatch):
    notice = CiResponsibilityNotice.model_validate(notice_payload([item("Tang")]))
    notice_file = tmp_path / "notice.json"
    notice_file.write_text(notice.model_dump_json(), encoding="utf-8")
    mapping_file = tmp_path / "mapping.csv"
    mapping_file.write_text(
        "mappingKey,authorName,authorEmail,normalizedEmail,wecomUserId,mappingStatus,note\n"
        "Tang <x@example.com>,Tang,x@example.com,x@example.com,tang.userid,manual,\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CI_AGENT_MODEL_PROVIDER", "fake")
    monkeypatch.setenv("CI_AGENT_WECOM_USER_MAPPING_FILE", str(mapping_file))
    monkeypatch.setenv("CI_AGENT_WECOM_MENTION_MODE", "userid")
    rc = main(["notify-notice", "--notice-file", str(notice_file), "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "<@tang.userid>" in out


def test_notify_notice_prefers_mongo_wecom_userid(monkeypatch):
    store = make_store()
    store.wecom_users.update_one(
        {"normalizedEmail": "x@example.com"},
        {"$set": {"authorName": "Tang", "normalizedEmail": "x@example.com", "wecomUserId": "mongo.userid", "commitCount": 1}},
        upsert=True,
    )
    notice = CiResponsibilityNotice.model_validate(notice_payload([item("Tang")]))
    settings = replace(load_settings(), notification_dedup_enabled=False, wecom_user_mapping_file=None)
    monkeypatch.setattr("ci_owner_agent.main.get_history_store", lambda settings: store)

    result = _notify_notice(notice, settings, dry_run=True, force=False, feedback_base_url=None)

    assert "<@mongo.userid>" in result["markdown"]


def test_notify_notice_falls_back_to_csv_when_mongo_missing(tmp_path, monkeypatch):
    store = make_store()
    mapping_file = tmp_path / "mapping.csv"
    mapping_file.write_text(
        "mappingKey,authorName,authorEmail,normalizedEmail,wecomUserId,mappingStatus,note\n"
        "Tang <x@example.com>,Tang,x@example.com,x@example.com,csv.userid,manual,\n",
        encoding="utf-8",
    )
    notice = CiResponsibilityNotice.model_validate(notice_payload([item("Tang")]))
    settings = replace(load_settings(), notification_dedup_enabled=False, wecom_user_mapping_file=mapping_file)
    monkeypatch.setattr("ci_owner_agent.main.get_history_store", lambda settings: store)

    result = _notify_notice(notice, settings, dry_run=True, force=False, feedback_base_url=None)

    assert "<@csv.userid>" in result["markdown"]


def test_notify_notice_falls_back_to_csv_when_history_store_unavailable(tmp_path, monkeypatch):
    mapping_file = tmp_path / "mapping.csv"
    mapping_file.write_text(
        "mappingKey,authorName,authorEmail,normalizedEmail,wecomUserId,mappingStatus,note\n"
        "Tang <x@example.com>,Tang,x@example.com,x@example.com,csv.userid,manual,\n",
        encoding="utf-8",
    )
    notice = CiResponsibilityNotice.model_validate(notice_payload([item("Tang")]))
    settings = replace(load_settings(), notification_dedup_enabled=False, wecom_user_mapping_file=mapping_file)
    monkeypatch.setattr("ci_owner_agent.main.get_history_store", lambda settings: None)

    result = _notify_notice(notice, settings, dry_run=True, force=False, feedback_base_url=None)

    assert "<@csv.userid>" in result["markdown"]


def test_notify_notice_falls_back_to_name_when_no_mapping(monkeypatch):
    store = make_store()
    notice = CiResponsibilityNotice.model_validate(notice_payload([item("Tang")]))
    settings = replace(load_settings(), notification_dedup_enabled=False, wecom_user_mapping_file=None)
    monkeypatch.setattr("ci_owner_agent.main.get_history_store", lambda settings: store)

    result = _notify_notice(notice, settings, dry_run=True, force=False, feedback_base_url=None)

    assert "👤 **责任人**：@Tang" in result["markdown"]


def test_notify_notice_mention_mode_name_ignores_mongo_userid(monkeypatch):
    store = make_store()
    store.wecom_users.update_one(
        {"normalizedEmail": "x@example.com"},
        {"$set": {"authorName": "Tang", "normalizedEmail": "x@example.com", "wecomUserId": "mongo.userid", "commitCount": 1}},
        upsert=True,
    )
    notice = CiResponsibilityNotice.model_validate(notice_payload([item("Tang")]))
    settings = replace(load_settings(), notification_dedup_enabled=False, wecom_user_mapping_file=None, wecom_mention_mode="name")
    monkeypatch.setattr("ci_owner_agent.main.get_history_store", lambda settings: store)

    result = _notify_notice(notice, settings, dry_run=True, force=False, feedback_base_url=None)

    assert "<@mongo.userid>" not in result["markdown"]
    assert "👤 **责任人**：@Tang" in result["markdown"]


def test_notify_notice_missing_file_returns_error_without_traceback(tmp_path, capsys, monkeypatch):
    missing = tmp_path / "missing.json"
    monkeypatch.setenv("CI_AGENT_MODEL_PROVIDER", "fake")
    rc = main(["notify-notice", "--notice-file", str(missing), "--dry-run"])
    captured = capsys.readouterr()
    assert rc == 2
    assert "ERROR: notice file not found:" in captured.err
    assert str(missing) in captured.err
    assert "Traceback" not in captured.err
    assert captured.out == ""


def test_notify_notice_unexpected_error_returns_2_without_traceback(tmp_path, capsys, monkeypatch):
    notice = CiResponsibilityNotice.model_validate(notice_payload([item()]))
    notice_file = tmp_path / "notice.json"
    notice_file.write_text(notice.model_dump_json(), encoding="utf-8")
    monkeypatch.setenv("CI_AGENT_MODEL_PROVIDER", "fake")

    def raise_notify(*args, **kwargs):
        raise RuntimeError("mongo down")

    monkeypatch.setattr("ci_owner_agent.main._notify_notice", raise_notify)

    rc = main(["notify-notice", "--notice-file", str(notice_file), "--dry-run"])
    captured = capsys.readouterr()

    assert rc == 2
    assert "ERROR: notify failed unexpectedly: mongo down" in captured.err
    assert "Traceback" not in captured.err
    assert captured.out == ""


def test_analyze_notify_dry_run_stdout_stays_json(monkeypatch, capsys):
    notice = CiResponsibilityNotice.model_validate(notice_payload([item()]))
    monkeypatch.setenv("CI_AGENT_MODEL_PROVIDER", "fake")
    monkeypatch.setattr("ci_owner_agent.main.analyze_local", lambda **kwargs: notice)
    rc = main(
        [
            "analyze-local",
            "--repo",
            "repo",
            "--job",
            "services/fx-code-unittest",
            "--build",
            "5099",
            "--base-commit",
            "b",
            "--head-commit",
            "h",
            "--console-file",
            "console.log",
            "--build-url",
            "local://job/5099",
            "--notify-dry-run",
            "--notify",
        ]
    )
    out = capsys.readouterr().out
    assert rc == 0
    parsed = __import__("json").loads(out)
    assert parsed["buildNumber"] == 5099
    assert "CI 单测失败" not in out


def test_maybe_notify_filters_no_owner_by_responsibility_items(monkeypatch):
    calls = []
    no_owner = item("无高可信责任人", "no_high_confidence_owner", "no_high_confidence_owner", "证据不足。")
    notice = CiResponsibilityNotice.model_validate(notice_payload([no_owner]))
    settings = replace(load_settings(), wecom_notify_on_no_owner=False)
    monkeypatch.setattr("ci_owner_agent.main._notify_notice", lambda *args, **kwargs: calls.append(kwargs) or {"ok": True})

    _maybe_notify_notice(notice, settings, cli_notify=True, cli_dry_run=True, force=False)

    assert calls == []


def test_maybe_notify_allows_item_owner_when_no_owner_notify_disabled(monkeypatch):
    calls = []
    notice = CiResponsibilityNotice.model_validate(notice_payload([item()]))
    settings = replace(load_settings(), wecom_notify_on_no_owner=False)
    monkeypatch.setattr("ci_owner_agent.main._notify_notice", lambda *args, **kwargs: calls.append(kwargs) or {"ok": True})

    _maybe_notify_notice(notice, settings, cli_notify=True, cli_dry_run=True, force=False)

    assert len(calls) == 1


def test_env_enabled_notify_skips_success_by_default(monkeypatch):
    calls = []
    payload = notice_payload([item()])
    payload["result"] = "SUCCESS"
    notice = CiResponsibilityNotice.model_validate(payload)
    monkeypatch.setenv("CI_AGENT_WECOM_NOTIFY_ENABLED", "true")
    monkeypatch.setenv("CI_AGENT_WECOM_NOTIFY_ON_SUCCESS", "false")
    settings = load_settings()
    monkeypatch.setattr("ci_owner_agent.main._notify_notice", lambda *args, **kwargs: calls.append(kwargs) or {"ok": True})

    _maybe_notify_notice(notice, settings, cli_notify=False, cli_dry_run=False, force=False)

    assert calls == []


def test_notify_dedup_does_not_overwrite_sent(monkeypatch):
    store = make_store()
    notice = CiResponsibilityNotice.model_validate(notice_payload([item()]))
    digest = notice_hash(notice)
    store.save_notification(notice=notice, notice_hash=digest, channel="wecom", status="sent", message="old")
    settings = replace(load_settings(), notification_dedup_enabled=True)
    monkeypatch.setattr("ci_owner_agent.main.get_history_store", lambda settings: store)

    result = _notify_notice(notice, settings, dry_run=True, force=False, feedback_base_url=None)

    assert result["status"] == "skipped"
    assert len(store.notifications.docs) == 1
    assert store.notifications.docs[0]["status"] == "sent"


def test_notify_force_bypasses_dedup(monkeypatch):
    store = make_store()
    notice = CiResponsibilityNotice.model_validate(notice_payload([item()]))
    digest = notice_hash(notice)
    store.save_notification(notice=notice, notice_hash=digest, channel="wecom", status="sent", message="old")
    settings = replace(load_settings(), notification_dedup_enabled=True, wecom_webhook_url="https://secret-webhook")
    monkeypatch.setattr("ci_owner_agent.main.get_history_store", lambda settings: store)
    monkeypatch.setattr("ci_owner_agent.main.send_wecom_markdown", lambda url, markdown: {"ok": True, "statusCode": 200, "response": "ok", "error": None})

    result = _notify_notice(notice, settings, dry_run=False, force=True, feedback_base_url=None)

    assert result["ok"] is True
    assert result.get("status") != "skipped"
    assert store.notifications.docs[0]["status"] == "sent"


def test_analyze_notify_exception_stays_json(monkeypatch, capsys):
    notice = CiResponsibilityNotice.model_validate(notice_payload([item()]))
    monkeypatch.setenv("CI_AGENT_MODEL_PROVIDER", "fake")
    monkeypatch.setattr("ci_owner_agent.main.analyze_local", lambda **kwargs: notice)

    def raise_notify(*args, **kwargs):
        raise RuntimeError("mongo down")

    monkeypatch.setattr("ci_owner_agent.main._notify_notice", raise_notify)
    rc = main(
        [
            "analyze-local",
            "--repo",
            "repo",
            "--job",
            "services/fx-code-unittest",
            "--build",
            "5099",
            "--base-commit",
            "b",
            "--head-commit",
            "h",
            "--console-file",
            "console.log",
            "--build-url",
            "local://job/5099",
            "--notify",
        ]
    )
    captured = capsys.readouterr()
    parsed = json.loads(captured.out)
    assert rc == 0
    assert parsed["buildNumber"] == 5099
    assert "WARNING" not in captured.out
    assert "CI 单测失败" not in captured.out
    assert "WARNING: notify failed unexpectedly: mongo down" in captured.err

def test_fallback_userids_appended_when_no_owner():
    no_owner = item("无高可信责任人", "no_high_confidence_owner", "no_high_confidence_owner", "证据不足。")
    notice = CiResponsibilityNotice.model_validate(notice_payload([no_owner]))
    markdown = format_wecom_markdown_notice(notice, fallback_userids=("ci.owner", "team.leader"))
    # Top line with bold markers
    assert "**责任人**：无高可信责任人，兜底通知 <@ci.owner>、<@team.leader>" in markdown
    # Item detail line still shows no-owner without fallback
    item_lines = [l for l in markdown.split("\n") if l.strip().startswith("- 👤 责任人")]
    assert len(item_lines) >= 1
    assert "无高可信责任人" in item_lines[0]
    assert "ci.owner" not in item_lines[0]


def test_fallback_not_appended_when_has_real_owner():
    real = item("Tang", "inherited_failure_owner", "inherited_failure_owner", "历史持续失败。")
    notice = CiResponsibilityNotice.model_validate(notice_payload([real]))
    markdown = format_wecom_markdown_notice(notice, fallback_userids=("ci.owner",))
    # Top line shows @Tang (real owner), not fallback
    assert "**责任人**：" in markdown
    assert "@Tang" in markdown
    # No fallback text or userid
    assert "兜底通知" not in markdown
    assert "<@ci.owner>" not in markdown


def test_fallback_not_appended_when_mixed_owners():
    real = item("Tang", "inherited_failure_owner", "inherited_failure_owner", "历史持续失败。")
    no_owner = item("无高可信责任人", "no_high_confidence_owner", "no_high_confidence_owner", "证据不足。")
    notice = CiResponsibilityNotice.model_validate(notice_payload([real, no_owner]))
    markdown = format_wecom_markdown_notice(notice, fallback_userids=("user001",))
    assert "兜底通知" not in markdown
    assert "Tang" in markdown


def test_item_owner_still_shows_no_owner_with_fallback():
    no_owner = item("无高可信责任人", "no_high_confidence_owner", "no_high_confidence_owner", "证据不足。")
    notice = CiResponsibilityNotice.model_validate(notice_payload([no_owner]))
    markdown = format_wecom_markdown_notice(notice, fallback_userids=("user001",))
    # Top shows fallback
    assert "兜底通知 <@user001>" in markdown
    # Item owner line still shows no high confidence owner (not fallback userid)
    # Item lines have "   - 👤 责任人：" prefix
    item_owner_lines = [line for line in markdown.split("\n") if line.strip().startswith("- 👤 责任人")]
    assert len(item_owner_lines) >= 1
    assert "无高可信责任人" in item_owner_lines[0]
    assert "user001" not in item_owner_lines[0]


def test_no_fallback_when_not_configured():
    no_owner = item("无高可信责任人", "no_high_confidence_owner", "no_high_confidence_owner", "证据不足。")
    notice = CiResponsibilityNotice.model_validate(notice_payload([no_owner]))
    markdown = format_wecom_markdown_notice(notice)
    assert "无高可信责任人" in markdown
    assert "兜底通知" not in markdown
    assert "<@" not in markdown


def test_single_fallback_userid():
    no_owner = item("无高可信责任人", "no_high_confidence_owner", "no_high_confidence_owner", "证据不足。")
    notice = CiResponsibilityNotice.model_validate(notice_payload([no_owner]))
    markdown = format_wecom_markdown_notice(notice, fallback_userids=("onlyone",))
    assert "兜底通知 <@onlyone>" in markdown


def test_notify_notice_passes_fallback_from_settings(monkeypatch):
    from ci_owner_agent.main import _notify_notice
    from ci_owner_agent.config import Settings, load_settings
    from dataclasses import replace

    no_owner = item("无高可信责任人", "no_high_confidence_owner", "no_high_confidence_owner", "证据不足。")
    notice = CiResponsibilityNotice.model_validate(notice_payload([no_owner]))
    settings = replace(load_settings(), wecom_fallback_userids=("fb1", "fb2"), wecom_webhook_url="https://h.example")

    markdowns = []
    monkeypatch.setattr("ci_owner_agent.main.get_history_store", lambda s: None)
    monkeypatch.setattr("ci_owner_agent.main.build_wecom_notice_mapper", lambda s, store: None)
    monkeypatch.setattr("ci_owner_agent.main.send_wecom_markdown", lambda url, md: markdowns.append(md) or {"ok": True})

    _notify_notice(notice, settings, dry_run=False, force=False, feedback_base_url=None)

    assert len(markdowns) == 1
    assert "兜底通知 <@fb1>、<@fb2>" in markdowns[0]


def test_notify_notice_dry_run_with_fallback(monkeypatch):
    from ci_owner_agent.main import _notify_notice
    from ci_owner_agent.config import load_settings
    from dataclasses import replace

    no_owner = item("无高可信责任人", "no_high_confidence_owner", "no_high_confidence_owner", "证据不足。")
    notice = CiResponsibilityNotice.model_validate(notice_payload([no_owner]))
    settings = replace(
        load_settings(),
        notification_dedup_enabled=False,
        wecom_user_mapping_file=None,
        wecom_fallback_userids=("ci.owner",),
    )

    monkeypatch.setattr("ci_owner_agent.main.get_history_store", lambda s: make_store())
    monkeypatch.setattr("ci_owner_agent.main.build_wecom_notice_mapper", lambda s, store: None)

    result = _notify_notice(notice, settings, dry_run=True, force=False, feedback_base_url=None)

    assert result["status"] == "dry_run"
    assert "无高可信责任人，兜底通知 <@ci.owner>" in result["markdown"]

def test_no_owner_notice_mentions_fallback_userids_only_at_top():
    no_owner = item("无高可信责任人", "no_high_confidence_owner", "no_high_confidence_owner", "证据不足。")
    notice = CiResponsibilityNotice.model_validate(notice_payload([no_owner]))

    markdown = format_wecom_markdown_notice(notice, fallback_userids=("ci.owner", "team.leader"))

    assert " **责任人**：无高可信责任人，兜底通知 <@ci.owner>、<@team.leader>" in markdown
    assert "   - 👤 责任人：无高可信责任人" in markdown


def test_fallback_userids_are_not_used_when_real_owner_exists():
    notice = CiResponsibilityNotice.model_validate(notice_payload([item("Tang")]))
    markdown = format_wecom_markdown_notice(notice, fallback_userids=("ci.owner",))

    assert " **责任人**：@Tang" in markdown
    assert "兜底通知" not in markdown
    assert "<@ci.owner>" not in markdown


def test_fallback_userids_are_cleaned_by_formatter():
    no_owner = item("无高可信责任人", "no_high_confidence_owner", "no_high_confidence_owner", "证据不足。")
    notice = CiResponsibilityNotice.model_validate(notice_payload([no_owner]))

    markdown = format_wecom_markdown_notice(notice, fallback_userids=(" ci.owner ", "", "ci.owner", "team.leader"))

    assert " **责任人**：无高可信责任人，兜底通知 <@ci.owner>、<@team.leader>" in markdown
    assert "<@>" not in markdown


def test_notify_notice_passes_fallback_userids_from_settings(monkeypatch):
    from ci_owner_agent.main import _notify_notice
    from ci_owner_agent.config import load_settings
    from dataclasses import replace

    store = make_store()
    no_owner = item("无高可信责任人", "no_high_confidence_owner", "no_high_confidence_owner", "证据不足。")
    notice = CiResponsibilityNotice.model_validate(notice_payload([no_owner]))
    settings = replace(
        load_settings(),
        notification_dedup_enabled=False,
        wecom_user_mapping_file=None,
        wecom_fallback_userids=("ci.owner",),
    )
    monkeypatch.setattr("ci_owner_agent.main.get_history_store", lambda settings: store)

    result = _notify_notice(notice, settings, dry_run=True, force=False, feedback_base_url=None)

    assert "无高可信责任人，兜底通知 <@ci.owner>" in result["markdown"]
    assert "   - 👤 责任人：无高可信责任人" in result["markdown"]
