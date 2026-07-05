from __future__ import annotations

import json
from dataclasses import replace

from ci_owner_agent.schemas import CiResponsibilityNotice
from ci_owner_agent.config import load_settings
from ci_owner_agent.main import _maybe_notify_notice, _notify_notice, main
from ci_owner_agent.services.history_store import notice_hash
from ci_owner_agent.services.notification_formatter import collect_responsible_display_names, format_wecom_markdown_notice
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


def item(owner_name="Tang.Tangerine-唐嘉伟", owner_type="inherited_failure_owner", responsibility_type="inherited_failure_owner", reason="历史持续失败。"):
    return {
        "failureId": "failure-secret",
        "failureTitle": "EtlUtils - getInputEntryInfo",
        "failureSignature": "sig-secret",
        "failureSummary": "summary",
        "owner": {"type": owner_type, "name": owner_name, "email": "x@example.com", "commit": "secretcommit", "confidence": 0.9},
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
    assert "**责任人**：@Tang.Tangerine-唐嘉伟" in markdown
    assert "顶层结论" not in markdown
    assert "顶层结论：无高可信责任人" not in markdown


def test_multiple_responsible_names_are_deduplicated_in_order():
    notice = CiResponsibilityNotice.model_validate(
        notice_payload([item("Tang"), item("Tang"), item("Mars", "high_confidence", "current_build_owner", "新失败。")])
    )
    assert collect_responsible_display_names(notice) == ["Tang", "Mars"]
    assert "**责任人**：@Tang、@Mars" in format_wecom_markdown_notice(notice)


def test_no_item_owner_displays_no_high_confidence_owner():
    no_owner = item("无高可信责任人", "no_high_confidence_owner", "no_high_confidence_owner", "证据不足。")
    notice = CiResponsibilityNotice.model_validate(notice_payload([no_owner]))
    assert "**责任人**：无高可信责任人" in format_wecom_markdown_notice(notice)


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
    assert "证据：当前 failure item 与历史构建 #5094 一致。" in format_wecom_markdown_notice(notice)


def test_empty_reason_falls_back_to_evidence_ids():
    notice = CiResponsibilityNotice.model_validate(
        notice_payload([item("Henry", "high_confidence", "current_build_owner", reason="")])
    )
    markdown = format_wecom_markdown_notice(notice)
    assert "日志显示 Webhook触发 Timeout" in markdown


def test_build_and_feedback_links():
    notice = CiResponsibilityNotice.model_validate(notice_payload([item()]))
    markdown = format_wecom_markdown_notice(notice, feedback_base_url="http://ci-agent.xxx/feedback")
    assert "#### 构建链接" in markdown
    assert notice.buildUrl in markdown
    assert "#### 反馈链接" in markdown
    assert "job=services%2Ffx-code-unittest" in markdown
    assert "build=5099" in markdown
    assert "未配置" in format_wecom_markdown_notice(notice, feedback_base_url=None)


def test_notify_notice_dry_run_outputs_markdown(tmp_path, capsys, monkeypatch):
    notice = CiResponsibilityNotice.model_validate(notice_payload([item()]))
    notice_file = tmp_path / "notice.json"
    notice_file.write_text(notice.model_dump_json(), encoding="utf-8")
    monkeypatch.setenv("CI_AGENT_MODEL_PROVIDER", "fake")
    rc = main(["notify-notice", "--notice-file", str(notice_file), "--dry-run", "--feedback-base-url", "http://ci-agent.test/feedback"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "### 单测失败 | services/fx-code-unittest #5099" in out
    assert "#### 反馈链接" in out


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
    assert "### 单测失败" not in out


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
    assert "### 单测失败" not in captured.out
    assert "WARNING: notify failed unexpectedly: mongo down" in captured.err
