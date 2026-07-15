from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def _isolated_subprocess_env() -> dict[str, str]:
    """Return an env dict that prevents .env from leaking real config into subprocess tests."""
    env = os.environ.copy()
    env.update(
        {
            "CI_AGENT_MODEL_PROVIDER": "fake",
            "CI_AGENT_MODEL_BASE_URL": "",
            "CI_AGENT_MODEL_NAME": "",
            "CI_AGENT_API_KEY": "",
            "CI_AGENT_HISTORY_ENABLED": "false",
            "CI_AGENT_HISTORY_MONGO_URI": "",
            "CI_AGENT_HISTORY_MONGO_DB": "",
            "CI_AGENT_WECOM_NOTIFY_ENABLED": "false",
            "CI_AGENT_WECOM_WEBHOOK_URL": "",
            "CI_AGENT_WECOM_BOT_ENABLED": "false",
            "CI_AGENT_WECOM_BOT_ID": "",
            "CI_AGENT_WECOM_BOT_SECRET": "",
            "CI_AGENT_METRICS_ENABLED": "false",
            "CI_AGENT_AI_FAILURE_FACTS_ENABLED": "false",
            "CI_AGENT_AI_HISTORY_COMPARE_ENABLED": "false",
            "JENKINS_URL": "",
            "JENKINS_USER": "",
            "JENKINS_TOKEN": "",
        }
    )
    return env


def _run_module(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "ci_owner_agent", *args],
        cwd=os.getcwd(),
        env=_isolated_subprocess_env(),
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def _notice_json_text() -> str:
    return json.dumps(
        {
            "repo": "sample-ts-repo",
            "job": "services/fx-code-unittest",
            "buildNumber": 5099,
            "buildUrl": "https://jenkins.example/job/5099/",
            "result": "FAILURE",
            "branch": "dev",
            "headCommit": "h",
            "baseCommit": "b",
            "owner": {
                "type": "no_high_confidence_owner",
                "name": "\u65e0\u9ad8\u53ef\u4fe1\u8d23\u4efb\u4eba",
                "email": None,
                "commit": None,
                "confidence": 0,
            },
            "failureReason": "\u672c\u6b21\u6784\u5efa\u5305\u542b\u591a\u4e2a\u5931\u8d25\uff0c\u8d23\u4efb\u9879\u89c1\u4e0b\u65b9\u3002",
            "evidence": [],
            "responsibilityItems": [
                {
                    "failureId": "F1",
                    "failureTitle": "\u63a5\u53e3\u8d85\u65f6\u6d4b\u8bd5\u5931\u8d25",
                    "failureSignature": "sig-1",
                    "failureSummary": "\u6784\u5efa\u8fc7\u7a0b\u4e2d\u63a5\u53e3\u8d85\u65f6",
                    "owner": {
                        "type": "inherited_failure_owner",
                        "name": "\u5f20\u4e09",
                        "email": "zhangsan@example.com",
                        "commit": "abc123",
                        "confidence": 0.9,
                    },
                    "responsibilityType": "inherited_failure_owner",
                    "sourceBuildNumber": 5094,
                    "sourceBuildUrl": None,
                    "sourceCommit": "def456",
                    "matchType": "signature_exact",
                    "relationship": "very_likely_same_failure",
                    "confidence": 0.9,
                    "reason": "\u5386\u53f2\u6301\u7eed\u5931\u8d25\u3002",
                    "evidenceIds": ["E1"],
                }
            ],
            "suggestions": [],
            "hasHighConfidenceOwner": False,
        },
        ensure_ascii=False,
        indent=2,
    ) + "\n"


def test_isolated_subprocess_env_blocks_dangerous_config(monkeypatch):
    monkeypatch.setenv("CI_AGENT_HISTORY_ENABLED", "true")
    monkeypatch.setenv("CI_AGENT_HISTORY_MONGO_URI", "mongodb://real-or-invalid-host")
    monkeypatch.setenv("CI_AGENT_WECOM_NOTIFY_ENABLED", "true")
    monkeypatch.setenv("CI_AGENT_WECOM_WEBHOOK_URL", "https://example.invalid/webhook")
    monkeypatch.setenv("CI_AGENT_MODEL_PROVIDER", "openai-compatible")
    monkeypatch.setenv("CI_AGENT_API_KEY", "secret")

    env = _isolated_subprocess_env()

    assert env["CI_AGENT_HISTORY_ENABLED"] == "false"
    assert env["CI_AGENT_WECOM_NOTIFY_ENABLED"] == "false"
    assert env["CI_AGENT_MODEL_PROVIDER"] == "fake"
    assert env["CI_AGENT_METRICS_ENABLED"] == "false"
    assert env["JENKINS_URL"] == ""
    assert env["CI_AGENT_API_KEY"] == ""
    assert env["CI_AGENT_HISTORY_MONGO_URI"] == ""
    assert env["CI_AGENT_WECOM_WEBHOOK_URL"] == ""


def test_module_notify_notice_missing_file_returns_process_code_2(tmp_path):
    missing = tmp_path / "missing.json"
    completed = _run_module("notify-notice", "--notice-file", str(missing), "--dry-run")
    assert completed.returncode == 2
    assert "ERROR: notice file not found" in completed.stderr
    assert "Traceback" not in completed.stderr


def test_module_feedback_apply_bad_owner_type_returns_process_code_2():
    completed = _run_module(
        "feedback", "apply",
        "--job", "services/fx-code-unittest",
        "--build", "5099",
        "--failure-id", "failure-xxx",
        "--action", "correct_owner",
        "--owner-name", "Henry.Zeng-\u66fe\u7eaa\u9f99",
        "--owner-type", "no_high_confidence_owner",
    )
    assert completed.returncode == 2
    assert "invalid choice" in completed.stderr
    assert "Traceback" not in completed.stderr


def test_notify_notice_accepts_utf8_bom_json(tmp_path):
    bom_file = tmp_path / "notice_bom.json"
    bom_file.write_text(_notice_json_text(), encoding="utf-8-sig")
    completed = _run_module("notify-notice", "--notice-file", str(bom_file), "--dry-run", "--force")
    assert completed.returncode == 0
    assert "Unexpected UTF-8 BOM" not in completed.stderr
    assert "ERROR" not in completed.stderr
    assert "\u63a5\u53e3\u8d85\u65f6" in completed.stdout
    assert "Traceback" not in completed.stderr


def test_notify_notice_accepts_plain_utf8_json(tmp_path):
    plain_file = tmp_path / "notice_plain.json"
    plain_file.write_text(_notice_json_text(), encoding="utf-8")
    completed = _run_module("notify-notice", "--notice-file", str(plain_file), "--dry-run", "--force")
    assert completed.returncode == 0
    assert "ERROR" not in completed.stderr
    assert "\u63a5\u53e3\u8d85\u65f6" in completed.stdout
    assert "Traceback" not in completed.stderr


def test_analyze_output_file_writes_utf8_without_bom(tmp_path):
    output_file = tmp_path / "subdir" / "notice.json"
    completed = _run_module(
        "analyze",
        "--job", "services/fx-code-unittest",
        "--build", "5154",
        "--repo", "fx-code",
        "--output-file", str(output_file),
    )
    assert completed.returncode == 0
    assert output_file.exists()
    raw = output_file.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8")
    data = json.loads(text)
    assert data["result"] == "UNKNOWN"
    assert "JENKINS_URL" in data["failureReason"]
    assert "buildUrl" not in completed.stdout


def test_analyze_local_output_file_writes_utf8_without_bom(tmp_path, monkeypatch):
    from ci_owner_agent.main import main
    from ci_owner_agent.schemas import CiResponsibilityNotice
    from tests.test_notification_formatter import item, notice_payload

    output_file = tmp_path / "subdir" / "notice_local.json"
    notice = CiResponsibilityNotice.model_validate(notice_payload([item("\u5f20\u4e09")]))
    monkeypatch.setattr("ci_owner_agent.main.analyze_local", lambda **kwargs: notice)
    monkeypatch.setattr("ci_owner_agent.main.get_history_store", lambda settings: None)

    exit_code = main([
        "analyze-local",
        "--repo", "sample-ts-repo",
        "--job", "services/fx-code-unittest",
        "--build", "5099",
        "--base-commit", "abc123",
        "--head-commit", "def456",
        "--console-file", str(tmp_path / "fake.log"),
        "--build-url", "https://jenkins.example/job/5099/",
        "--output-file", str(output_file),
    ])
    assert exit_code == 0
    assert output_file.exists()
    raw = output_file.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8")
    data = json.loads(text)
    assert data["result"] == "FAILURE"
    assert "\u5f20\u4e09" in text


def test_analyze_output_file_write_failure_returns_code_2(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("block")
    output_file = blocker / "subdir" / "notice.json"
    completed = _run_module(
        "analyze",
        "--job", "services/fx-code-unittest",
        "--build", "5154",
        "--repo", "fx-code",
        "--output-file", str(output_file),
    )
    assert completed.returncode == 2
    assert "ERROR" in completed.stderr
    assert "buildUrl" not in completed.stdout
    assert "Traceback" not in completed.stderr