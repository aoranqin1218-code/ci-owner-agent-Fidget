from __future__ import annotations

import json
from dataclasses import replace

import pytest

from ci_owner_agent.schemas import CiResponsibilityNotice
from ci_owner_agent.config import load_settings
from ci_owner_agent.main import _maybe_notify_notice, _notify_notice, main
from ci_owner_agent.services.notification_formatter import (
    collect_responsible_display_names,
    format_wecom_markdown_notice,
    notification_digest,
    result_icon,
    source_build_label,
)
from ci_owner_agent.services.test_maintainer_mapping import TestMaintainerResolver
from ci_owner_agent.services.wecom_user_mapping import WeComUserMapper
from tests.test_history_store import make_store


def notice_payload(items):
    return {
        "repo": "sample-ts-repo",
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


def item(owner_name="Tang.Tangerine-唐嘉伟", owner_type="inherited_failure_owner", responsibility_type="inherited_failure_owner", reason="历史持续失败。", owner_email="x@example.com", test_file_path=None):
    return {
        "failureId": "failure-secret",
        "failureTitle": "EtlUtils - getInputEntryInfo",
        "failureSignature": "sig-secret",
        "failureSummary": "summary",
        "testFilePath": test_file_path,
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


def test_source_build_label_does_not_show_untrusted_no_owner_history():
    payload = notice_payload(
        [item("无高可信责任人", "no_high_confidence_owner", "no_high_confidence_owner", "证据不足。")]
    )
    payload["responsibilityItems"][0].update(
        {
            "sourceBuildNumber": 123,
            "sourceBuildUrl": "fake",
            "matchType": "signature_exact",
            "relationship": "very_likely_same_failure",
        }
    )
    notice = CiResponsibilityNotice.model_validate(payload)

    assert source_build_label(notice.responsibilityItems[0], notice) == "-"


def test_source_build_label_for_current_and_inherited_owner():
    current_notice = CiResponsibilityNotice.model_validate(
        notice_payload([item("Li", "high_confidence", "current_build_owner", "current", "li@example.com")])
    )
    inherited_notice = CiResponsibilityNotice.model_validate(notice_payload([item()]))

    assert source_build_label(current_notice.responsibilityItems[0], current_notice) == "#5099"
    assert source_build_label(inherited_notice.responsibilityItems[0], inherited_notice) == "#5094"


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
    assert "branch=dev" in markdown
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


def test_notify_dry_run_does_not_enqueue(monkeypatch):
    store = make_store()
    notice = CiResponsibilityNotice.model_validate(notice_payload([item()]))
    settings = replace(load_settings(), notification_dedup_enabled=True)
    monkeypatch.setattr("ci_owner_agent.main.get_history_store", lambda settings: store)

    result = _notify_notice(notice, settings, dry_run=True, force=False, feedback_base_url=None)

    assert result["status"] == "dry_run"
    assert store.wecom_notification_outbox.docs == []


def test_notify_force_bypasses_dedup(monkeypatch):
    store = make_store()
    notice = CiResponsibilityNotice.model_validate(notice_payload([item()]))
    settings = replace(load_settings(), notification_dedup_enabled=True, wecom_bot_notify_chat_id="chat")
    monkeypatch.setattr("ci_owner_agent.main.get_history_store", lambda settings: store)

    result = _notify_notice(notice, settings, dry_run=False, force=True, feedback_base_url=None)

    assert result["ok"] is True
    assert result["inserted"] is True
    assert store.wecom_notification_outbox.docs[0]["status"] == "pending"


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
    assert "**责任人**：无高可信责任人" in markdown
    assert "**待确认维护人**：<@ci.owner>、<@team.leader>" in markdown
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


def test_mixed_real_and_no_owner_keeps_real_owner_and_routes_no_owner():
    real = item("Tang", "inherited_failure_owner", "inherited_failure_owner", "历史持续失败。")
    no_owner = item("无高可信责任人", "no_high_confidence_owner", "no_high_confidence_owner", "证据不足。")
    notice = CiResponsibilityNotice.model_validate(notice_payload([real, no_owner]))
    markdown = format_wecom_markdown_notice(notice, fallback_userids=("user001",))
    assert "**责任人**：@Tang" in markdown
    assert "**待确认维护人**：<@user001>" in markdown
    assert "   - 👤 责任人：无高可信责任人" in markdown


def test_item_owner_still_shows_no_owner_with_fallback():
    no_owner = item("无高可信责任人", "no_high_confidence_owner", "no_high_confidence_owner", "证据不足。")
    notice = CiResponsibilityNotice.model_validate(notice_payload([no_owner]))
    markdown = format_wecom_markdown_notice(notice, fallback_userids=("user001",))
    assert "**待确认维护人**：<@user001>" in markdown
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


def test_legacy_notice_without_items_still_routes_fallback():
    notice = CiResponsibilityNotice.model_validate(notice_payload([]))
    markdown = format_wecom_markdown_notice(notice, fallback_userids=("legacy.fallback",))
    assert "**责任人**：无高可信责任人" in markdown
    assert "**待确认维护人**：<@legacy.fallback>" in markdown


@pytest.mark.parametrize("result", ["SUCCESS", "ABORTED"])
def test_success_and_aborted_legacy_notice_do_not_route_fallback_or_change_digest(result):
    payload = notice_payload([])
    payload["result"] = result
    notice = CiResponsibilityNotice.model_validate(payload)

    markdown = format_wecom_markdown_notice(notice, fallback_userids=("fallback",))
    without_fallback = notification_digest(notice, fallback_userids=())
    with_fallback = notification_digest(notice, fallback_userids=("fallback",))

    assert "待确认维护人" not in markdown
    assert "<@fallback>" not in markdown
    assert without_fallback == with_fallback


@pytest.mark.parametrize("result", ["FAILURE", "UNSTABLE", "UNKNOWN"])
def test_failed_legacy_notice_routes_fallback(result):
    payload = notice_payload([])
    payload["result"] = result
    notice = CiResponsibilityNotice.model_validate(payload)
    markdown = format_wecom_markdown_notice(notice, fallback_userids=("fallback",))
    assert "**待确认维护人**：<@fallback>" in markdown


def test_single_fallback_userid():
    no_owner = item("无高可信责任人", "no_high_confidence_owner", "no_high_confidence_owner", "证据不足。")
    notice = CiResponsibilityNotice.model_validate(notice_payload([no_owner]))
    markdown = format_wecom_markdown_notice(notice, fallback_userids=("onlyone",))
    assert "**待确认维护人**：<@onlyone>" in markdown


def test_notify_notice_passes_fallback_from_settings(monkeypatch):
    from ci_owner_agent.main import _notify_notice
    from ci_owner_agent.config import Settings, load_settings
    from dataclasses import replace

    no_owner = item("无高可信责任人", "no_high_confidence_owner", "no_high_confidence_owner", "证据不足。")
    notice = CiResponsibilityNotice.model_validate(notice_payload([no_owner]))
    settings = replace(load_settings(), wecom_fallback_userids=("fb1", "fb2"), wecom_bot_notify_chat_id="chat")

    store = make_store()
    monkeypatch.setattr("ci_owner_agent.main.get_history_store", lambda s: store)
    monkeypatch.setattr("ci_owner_agent.main.build_wecom_notice_mapper", lambda s, store: None)

    _notify_notice(notice, settings, dry_run=False, force=False, feedback_base_url=None)

    assert "**待确认维护人**：<@fb1>、<@fb2>" in store.wecom_notification_outbox.docs[0]["payload"]["content"]


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
    assert "**责任人**：无高可信责任人" in result["markdown"]
    assert "**待确认维护人**：<@ci.owner>" in result["markdown"]

def test_no_owner_notice_mentions_fallback_userids_at_top_and_item_route():
    no_owner = item("无高可信责任人", "no_high_confidence_owner", "no_high_confidence_owner", "证据不足。")
    notice = CiResponsibilityNotice.model_validate(notice_payload([no_owner]))

    markdown = format_wecom_markdown_notice(notice, fallback_userids=("ci.owner", "team.leader"))

    assert " **责任人**：无高可信责任人" in markdown
    assert " **待确认维护人**：<@ci.owner>、<@team.leader>" in markdown
    assert "   - 👤 责任人：无高可信责任人" in markdown
    assert "   - 📣 待确认维护人：<@ci.owner>、<@team.leader>" in markdown


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

    assert " **责任人**：无高可信责任人" in markdown
    assert " **待确认维护人**：<@ci.owner>、<@team.leader>" in markdown
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

    assert "**责任人**：无高可信责任人" in result["markdown"]
    assert "**待确认维护人**：<@ci.owner>" in result["markdown"]
    assert "   - 👤 责任人：无高可信责任人" in result["markdown"]


def _maintainer_resolver(tmp_path, maintainers=None, pattern="test/service/view/**"):
    maintainers = maintainers or [("Charlie", "charlie.guo")]
    rows = "\n".join(
        f"      - name: \"{name}\"\n        wecomUserId: \"{userid}\"" for name, userid in maintainers
    )
    path = tmp_path / "maintainers.yml"
    path.write_text(
        "version: 1\nrules:\n"
        "  - repo: fx-code\n"
        "    job: services/fx-code-unittest\n"
        f"    paths: [\"{pattern}\"]\n"
        "    maintainers:\n"
        f"{rows}\n",
        encoding="utf-8",
    )
    return TestMaintainerResolver.from_yaml(path)


def test_no_owner_path_routes_single_maintainer_without_changing_owner(tmp_path):
    no_owner = item(
        "无高可信责任人",
        "no_high_confidence_owner",
        "no_high_confidence_owner",
        "证据不足。",
        test_file_path="test/service/view/ViewDataQueryServiceTest.ts",
    )
    notice = CiResponsibilityNotice.model_validate(notice_payload([no_owner]))
    markdown = format_wecom_markdown_notice(
        notice,
        maintainer_resolver=_maintainer_resolver(tmp_path),
        repo="fx-code",
    )

    assert "**责任人**：无高可信责任人" in markdown
    assert "**待确认维护人**：<@charlie.guo>" in markdown
    assert "📁 测试文件：test/service/view/ViewDataQueryServiceTest.ts" in markdown
    assert notice.owner.type == "no_high_confidence_owner"
    assert notice.responsibilityItems[0].owner.name == "无高可信责任人"


def test_no_owner_path_routes_multiple_maintainers_in_order(tmp_path):
    no_owner = item(
        "无高可信责任人",
        "no_high_confidence_owner",
        "no_high_confidence_owner",
        "证据不足。",
        test_file_path="test/service/view/ViewDataQueryServiceTest.ts",
    )
    notice = CiResponsibilityNotice.model_validate(notice_payload([no_owner]))
    resolver = _maintainer_resolver(tmp_path, [("Charlie", "charlie.guo"), ("Henry", "henry")])
    markdown = format_wecom_markdown_notice(notice, maintainer_resolver=resolver, repo="fx-code")

    assert "**待确认维护人**：<@charlie.guo>、<@henry>" in markdown
    assert "   - 📣 待确认维护人：<@charlie.guo>、<@henry>" in markdown


def test_unmatched_and_missing_test_path_route_fallback(tmp_path):
    unmatched = item(
        "无高可信责任人",
        "no_high_confidence_owner",
        "no_high_confidence_owner",
        "证据不足。",
        test_file_path="test/service/unknown/UnknownTest.ts",
    )
    missing = item("无高可信责任人", "no_high_confidence_owner", "no_high_confidence_owner", "证据不足。")
    resolver = _maintainer_resolver(tmp_path)

    unmatched_markdown = format_wecom_markdown_notice(
        CiResponsibilityNotice.model_validate(notice_payload([unmatched])),
        maintainer_resolver=resolver,
        repo="fx-code",
        fallback_userids=("default.userid",),
    )
    missing_markdown = format_wecom_markdown_notice(
        CiResponsibilityNotice.model_validate(notice_payload([missing])),
        maintainer_resolver=resolver,
        repo="fx-code",
        fallback_userids=("default.userid",),
    )

    assert "📁 测试文件：test/service/unknown/UnknownTest.ts" in unmatched_markdown
    assert "未匹配测试文件维护规则" in unmatched_markdown
    assert "📁 测试文件：未识别" in missing_markdown
    assert "未识别失败测试文件" in missing_markdown
    assert "<@default.userid>" in unmatched_markdown
    assert "<@default.userid>" in missing_markdown

    evidence_pos = unmatched_markdown.index("   - 🔎 证据：")
    path_pos = unmatched_markdown.index("   - 📁 测试文件：")
    maintainer_pos = unmatched_markdown.index("   - 📣 待确认维护人：")
    reason_pos = unmatched_markdown.index("   - ℹ️ 路由说明：")
    assert evidence_pos < path_pos < maintainer_pos < reason_pos


def test_multiple_no_owner_routes_are_deduplicated_and_mixed_owner_is_preserved(tmp_path):
    real = item("Tang", "high_confidence", "current_build_owner", "当前引入。", "tang@example.com")
    first = item("无高可信责任人", "no_high_confidence_owner", "no_high_confidence_owner", "待确认。", test_file_path="test/service/view/A.test.ts")
    second = item("无高可信责任人", "no_high_confidence_owner", "no_high_confidence_owner", "待确认。", test_file_path="test/service/view/B.test.ts")
    notice = CiResponsibilityNotice.model_validate(notice_payload([real, first, second]))
    resolver = _maintainer_resolver(tmp_path, [("Charlie", "charlie.guo"), ("Henry", "henry")])

    markdown = format_wecom_markdown_notice(notice, maintainer_resolver=resolver, repo="fx-code")

    assert "**责任人**：@Tang" in markdown
    assert markdown.count("**待确认维护人**：<@charlie.guo>、<@henry>") == 1
    assert notice.responsibilityItems[1].owner.type == "no_high_confidence_owner"


def test_multiple_no_owner_items_route_to_different_maintainers(tmp_path):
    path = tmp_path / "maintainers.yml"
    path.write_text(
        "version: 1\nrules:\n"
        "  - paths: [\"test/view/**\"]\n"
        "    maintainers: [{name: Charlie, wecomUserId: charlie}]\n"
        "  - paths: [\"test/quota/**\"]\n"
        "    maintainers: [{name: Mars, wecomUserId: mars}]\n",
        encoding="utf-8",
    )
    view = item("无高可信责任人", "no_high_confidence_owner", "no_high_confidence_owner", "待确认。", test_file_path="test/view/ViewTest.ts")
    quota = item("无高可信责任人", "no_high_confidence_owner", "no_high_confidence_owner", "待确认。", test_file_path="test/quota/QuotaTest.ts")
    notice = CiResponsibilityNotice.model_validate(notice_payload([view, quota]))

    markdown = format_wecom_markdown_notice(
        notice,
        maintainer_resolver=TestMaintainerResolver.from_yaml(path),
    )

    assert "**待确认维护人**：<@charlie>、<@mars>" in markdown
    assert "   - 📣 待确认维护人：<@charlie>" in markdown
    assert "   - 📣 待确认维护人：<@mars>" in markdown


def test_maintainer_name_mode_and_no_no_owner_item(tmp_path):
    no_owner = item("无高可信责任人", "no_high_confidence_owner", "no_high_confidence_owner", "待确认。", test_file_path="test/service/view/A.test.ts")
    resolver = _maintainer_resolver(tmp_path, [("Charlie", "charlie.guo")])
    name_markdown = format_wecom_markdown_notice(
        CiResponsibilityNotice.model_validate(notice_payload([no_owner])),
        maintainer_resolver=resolver,
        repo="fx-code",
        mention_mode="name",
    )
    real_markdown = format_wecom_markdown_notice(
        CiResponsibilityNotice.model_validate(notice_payload([item("Tang")])),
        maintainer_resolver=resolver,
        repo="fx-code",
    )

    assert "**待确认维护人**：@Charlie" in name_markdown
    assert "待确认维护人" not in real_markdown


def test_notification_digest_changes_when_maintainer_route_changes(tmp_path):
    no_owner = item("无高可信责任人", "no_high_confidence_owner", "no_high_confidence_owner", "待确认。", test_file_path="test/service/view/A.test.ts")
    notice = CiResponsibilityNotice.model_validate(notice_payload([no_owner]))
    first = _maintainer_resolver(tmp_path, [("Charlie", "charlie.guo")])
    first_digest = notification_digest(notice, maintainer_resolver=first, repo="fx-code")
    second = _maintainer_resolver(tmp_path, [("Henry", "henry")])
    second_digest = notification_digest(notice, maintainer_resolver=second, repo="fx-code")

    assert first_digest != second_digest
    assert first_digest == notification_digest(notice, maintainer_resolver=first, repo="fx-code")


def test_notify_notice_loads_maintainer_mapping_from_settings(tmp_path, monkeypatch):
    resolver = _maintainer_resolver(tmp_path, [("Charlie", "charlie.guo")])
    mapping_file = tmp_path / "maintainers.yml"
    no_owner = item(
        "无高可信责任人",
        "no_high_confidence_owner",
        "no_high_confidence_owner",
        "待确认。",
        test_file_path="test/service/view/A.test.ts",
    )
    payload = notice_payload([no_owner])
    payload["repo"] = "fx-code"
    notice = CiResponsibilityNotice.model_validate(payload)
    settings = replace(
        load_settings(),
        notification_dedup_enabled=False,
        test_maintainer_mapping_file=mapping_file,
        wecom_fallback_userids=("fallback",),
    )
    monkeypatch.setattr("ci_owner_agent.main.get_history_store", lambda settings: None)

    result = _notify_notice(notice, settings, dry_run=True, force=False, feedback_base_url=None)

    assert resolver.warnings == ()
    assert "**待确认维护人**：<@charlie.guo>" in result["markdown"]
    assert "<@fallback>" not in result["markdown"]


def test_notify_notice_invalid_mapping_warns_and_uses_fallback(tmp_path, monkeypatch, capsys):
    mapping_file = tmp_path / "invalid.yml"
    mapping_file.write_text("rules: [", encoding="utf-8")
    no_owner = item("无高可信责任人", "no_high_confidence_owner", "no_high_confidence_owner", "待确认。")
    notice = CiResponsibilityNotice.model_validate(notice_payload([no_owner]))
    settings = replace(
        load_settings(),
        notification_dedup_enabled=False,
        test_maintainer_mapping_file=mapping_file,
        wecom_fallback_userids=("fallback",),
    )
    monkeypatch.setattr("ci_owner_agent.main.get_history_store", lambda settings: None)

    result = _notify_notice(notice, settings, dry_run=True, force=False, feedback_base_url=None)
    captured = capsys.readouterr()

    assert "invalid test maintainer mapping yaml" in captured.err
    assert "**待确认维护人**：<@fallback>" in result["markdown"]
