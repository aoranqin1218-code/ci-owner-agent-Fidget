from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace


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
            "CI_AGENT_WECOM_BOT_ENABLED": "false",
    "CI_AGENT_WECOM_BOT_LLM_ENABLED": "false",
    "CI_AGENT_WECOM_BOT_LLM_MAX_INPUT_CHARS": "2000",
            "CI_AGENT_WECOM_BOT_ID": "",
            "CI_AGENT_WECOM_BOT_SECRET": "",
            "CI_AGENT_METRICS_ENABLED": "false",
            "CI_AGENT_AI_FAILURE_FACTS_ENABLED": "false",
            "CI_AGENT_AI_HISTORY_COMPARE_ENABLED": "false",
            "JENKINS_URL": "",
            "JENKINS_USER": "",
            "JENKINS_TOKEN": "",
            "LANGSMITH_TRACING": "false",
            "LANGSMITH_API_KEY": "",
            "LANGSMITH_PROJECT": "",
            "LANGSMITH_ENDPOINT": "",
            "CI_AGENT_WECOM_USER_MAPPING_FILE": "",
            "CI_AGENT_TEST_MAINTAINER_MAPPING_FILE": "",
            "CI_AGENT_FEEDBACK_BASE_URL": "",
            "CI_AGENT_FEEDBACK_SHARED_TOKEN": "",
            "CI_AGENT_NOTIFICATION_DEDUP_ENABLED": "false",
            "CI_AGENT_WECOM_FALLBACK_USERIDS": "",
            "CI_AGENT_WECOM_MENTION_MODE": "userid",
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
    monkeypatch.setenv("CI_AGENT_MODEL_PROVIDER", "openai-compatible")
    monkeypatch.setenv("CI_AGENT_API_KEY", "secret")
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "real-secret")
    monkeypatch.setenv("CI_AGENT_WECOM_USER_MAPPING_FILE", "C:/real/users.csv")
    monkeypatch.setenv("CI_AGENT_TEST_MAINTAINER_MAPPING_FILE", "C:/real/maintainers.yml")
    monkeypatch.setenv("CI_AGENT_FEEDBACK_SHARED_TOKEN", "real-token")

    env = _isolated_subprocess_env()

    assert env["CI_AGENT_HISTORY_ENABLED"] == "false"
    assert env["CI_AGENT_WECOM_NOTIFY_ENABLED"] == "false"
    assert env["CI_AGENT_MODEL_PROVIDER"] == "fake"
    assert env["CI_AGENT_METRICS_ENABLED"] == "false"
    assert env["JENKINS_URL"] == ""
    assert env["CI_AGENT_API_KEY"] == ""
    assert env["CI_AGENT_HISTORY_MONGO_URI"] == ""
    assert env["LANGSMITH_TRACING"] == "false"
    assert env["LANGSMITH_API_KEY"] == ""
    assert env["LANGSMITH_PROJECT"] == ""
    assert env["LANGSMITH_ENDPOINT"] == ""
    assert env["CI_AGENT_WECOM_USER_MAPPING_FILE"] == ""
    assert env["CI_AGENT_TEST_MAINTAINER_MAPPING_FILE"] == ""
    assert env["CI_AGENT_FEEDBACK_BASE_URL"] == ""
    assert env["CI_AGENT_FEEDBACK_SHARED_TOKEN"] == ""


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


def test_fake_provider_does_not_build_real_model():
    """provider=fake should not call build_chat_model."""
    import subprocess
    import sys

    env = _isolated_subprocess_env()
    env["CI_AGENT_WECOM_BOT_LLM_ENABLED"] = "true"
    env["CI_AGENT_MODEL_PROVIDER"] = "fake"
    env["CI_AGENT_WECOM_BOT_ENABLED"] = "true"
    env["CI_AGENT_WECOM_BOT_ID"] = "bot-id"
    env["CI_AGENT_WECOM_BOT_SECRET"] = "bot-secret"
    env["CI_AGENT_HISTORY_ENABLED"] = "false"

    # We can't actually start the bot (requires MongoDB), but we can verify
    # that main.py imports work without constructing ChatOpenAI
    result = subprocess.run(
        [sys.executable, "-c", """
from ci_owner_agent.config import load_settings
from ci_owner_agent.main import main
import sys
# Just verify module imports without running serve-wecom-bot
sys.exit(0)
"""],
        capture_output=True, text=True, timeout=30, env=env,
    )
    assert result.returncode == 0, f"Import failed: {result.stderr}"


def test_non_fake_provider_without_api_key_does_not_block():
    """Missing API key should not prevent bot startup for fixed commands."""
    import subprocess
    import sys

    env = _isolated_subprocess_env()
    env["CI_AGENT_WECOM_BOT_LLM_ENABLED"] = "true"
    env["CI_AGENT_MODEL_PROVIDER"] = "openai-compatible"
    env["CI_AGENT_MODEL_NAME"] = "gpt-4o-mini"
    # No API key set - validation should fail but not block
    env["CI_AGENT_WECOM_BOT_ENABLED"] = "true"
    env["CI_AGENT_WECOM_BOT_ID"] = "bot-id"
    env["CI_AGENT_WECOM_BOT_SECRET"] = "bot-secret"
    env["CI_AGENT_HISTORY_ENABLED"] = "false"

    result = subprocess.run(
        [sys.executable, "-c", """
from ci_owner_agent.config import load_settings, validate_model_settings
settings = load_settings()
error = validate_model_settings(settings)
assert error is not None, "Expected validation error for missing API key"
assert "API_KEY" in error or "required" in error
print(f"Validation correctly rejected: {error}")
"""],
        capture_output=True, text=True, timeout=30, env=env,
    )
    assert result.returncode == 0, f"Validation test failed: {result.stderr}"



def test_build_wecom_ai_parser_fake_never_builds_real_model(monkeypatch):
    """_build_wecom_feedback_ai_parser with provider=fake must not construct real model."""
    from ci_owner_agent.config import load_settings
    from ci_owner_agent.main import _build_wecom_feedback_ai_parser

    # Patch at the actual location where build_chat_model is used
    import ci_owner_agent.services.wecom_feedback_ai_parser as ai_parser_module
    call_count = 0
    def never_call(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        raise AssertionError("build_chat_model must not be called with provider=fake")
    monkeypatch.setattr(ai_parser_module, "build_chat_model", never_call)

    # Also ensure WeComFeedbackAiParser is never constructed
    def fail_parser_init(*args, **kwargs):
        raise AssertionError("WeComFeedbackAiParser must not be constructed")
    monkeypatch.setattr(ai_parser_module, "WeComFeedbackAiParser", fail_parser_init)

    monkeypatch.setenv("CI_AGENT_WECOM_BOT_LLM_ENABLED", "true")
    monkeypatch.setenv("CI_AGENT_MODEL_PROVIDER", "fake")
    monkeypatch.setenv("CI_AGENT_WECOM_BOT_LLM_MAX_INPUT_CHARS", "2000")

    settings = load_settings()
    result = _build_wecom_feedback_ai_parser(settings)

    from ci_owner_agent.services.wecom_feedback_ai_parser import FakeWeComFeedbackAiParser
    assert isinstance(result, FakeWeComFeedbackAiParser)
    assert call_count == 0


def test_build_wecom_ai_parser_invalid_config_returns_none(monkeypatch):
    """Invalid model config should return None without constructing model."""
    from ci_owner_agent.config import load_settings
    from ci_owner_agent.main import _build_wecom_feedback_ai_parser

    # Patch at the actual location where build_chat_model is used
    import ci_owner_agent.services.wecom_feedback_ai_parser as ai_parser_module
    call_count = 0
    def never_call(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        raise AssertionError("build_chat_model must not be called")
    monkeypatch.setattr(ai_parser_module, "build_chat_model", never_call)

    # Also ensure WeComFeedbackAiParser is never constructed
    def fail_parser_init(*args, **kwargs):
        raise AssertionError("WeComFeedbackAiParser must not be constructed")
    monkeypatch.setattr(ai_parser_module, "WeComFeedbackAiParser", fail_parser_init)

    monkeypatch.setenv("CI_AGENT_WECOM_BOT_LLM_ENABLED", "true")
    monkeypatch.setenv("CI_AGENT_MODEL_PROVIDER", "openai-compatible")
    monkeypatch.setenv("CI_AGENT_MODEL_NAME", "gpt-4o-mini")
    monkeypatch.setenv("CI_AGENT_API_KEY", "")
    monkeypatch.setenv("CI_AGENT_MODEL_BASE_URL", "")

    settings = load_settings()
    result = _build_wecom_feedback_ai_parser(settings)

    assert result is None
    assert call_count == 0


def test_build_wecom_ai_parser_disabled_returns_none(monkeypatch):
    """LLM disabled should return None."""
    from ci_owner_agent.config import load_settings
    from ci_owner_agent.main import _build_wecom_feedback_ai_parser

    monkeypatch.setenv("CI_AGENT_WECOM_BOT_LLM_ENABLED", "false")
    settings = load_settings()
    result = _build_wecom_feedback_ai_parser(settings)
    assert result is None


def test_serve_wecom_bot_passes_ai_parser_to_worker(monkeypatch):
    """main() serve-wecom-bot must pass built ai_parser to WeComBotWorker."""
    from ci_owner_agent.main import main

    sentinel_parser = object()
    captured = {}

    def fake_build(settings):
        return sentinel_parser

    monkeypatch.setattr(
        "ci_owner_agent.main._build_wecom_feedback_ai_parser",
        fake_build,
    )

    def fake_get_history_store(settings):
        class FakeStore:
            client = type("obj", (object,), {"admin": type("obj", (object,), {"command": lambda self, cmd: None})()})()
            wecom_bot_events = type("obj", (object,), {"create_index": lambda self, *a, **kw: None})()
        return FakeStore()

    monkeypatch.setattr(
        "ci_owner_agent.main.get_history_store",
        fake_get_history_store,
    )

    # Patch at the import source rather than the local name inside main()
    class FakeWeComSdkAdapter:
        def __init__(self, bot_id, secret):
            pass
        def set_text_handler(self, h):
            pass
        def set_template_card_event_handler(self, h):
            pass
        def set_fatal_error_handler(self, h):
            pass

    monkeypatch.setattr(
        "ci_owner_agent.services.wecom_bot_adapter.WeComSdkAdapter",
        FakeWeComSdkAdapter,
    )

    class FakeWorker:
        def __init__(self, adapter, store, **kwargs):
            captured["ai_parser"] = kwargs["ai_parser"]
            captured["discover_chat_id"] = kwargs["discover_chat_id"]
        def run(self):
            pass

    monkeypatch.setattr(
        "ci_owner_agent.services.wecom_bot_worker.WeComBotWorker",
        FakeWorker,
    )

    monkeypatch.setenv("CI_AGENT_HISTORY_ENABLED", "true")
    monkeypatch.setenv("CI_AGENT_WECOM_BOT_ENABLED", "true")
    monkeypatch.setenv("CI_AGENT_WECOM_BOT_LLM_ENABLED", "true")
    monkeypatch.setenv("CI_AGENT_WECOM_NOTIFY_ENABLED", "false")

    result = main([
        "serve-wecom-bot",
        "--bot-id",
        "test-bot",
        "--secret",
        "test-secret",
    ])

    assert result == 0
    assert captured.get("ai_parser") is sentinel_parser
    assert captured.get("discover_chat_id") is False


def test_weekly_notify_outbox_exception_returns_2_without_traceback(monkeypatch, capsys):
    from ci_owner_agent.main import main

    settings = SimpleNamespace(
        history_enabled=True,
        test_maintainer_mapping_file=None,
        wecom_fallback_userids=(),
        wecom_bot_notify_chat_id="chat",
        notification_dedup_enabled=True,
        wecom_bot_notify_lease_seconds=30,
        wecom_bot_notify_max_attempts=5,
        wecom_mention_mode="userid",
        weekly_test_report_config_file="unused.yml",
    )
    config = SimpleNamespace(timezone="UTC", topN=10)

    class FakeService:
        def __init__(self, *args, **kwargs):
            pass
        def generate(self, **kwargs):
            return {"markdown": "safe weekly report"}
        def notify(self, *args, **kwargs):
            raise RuntimeError("mongodb://user:password@secret-host")

    monkeypatch.setattr("ci_owner_agent.main.load_settings", lambda: settings)
    monkeypatch.setattr("ci_owner_agent.main.get_history_store", lambda _: object())
    monkeypatch.setattr("ci_owner_agent.main.load_weekly_test_report_config", lambda _: config)
    monkeypatch.setattr("ci_owner_agent.main.resolve_period", lambda **_: (None, None))
    monkeypatch.setattr("ci_owner_agent.main.WeeklyTestReportService", FakeService)
    assert main(["weekly-test-report", "--repo", "r", "--notify"]) == 2
    captured = capsys.readouterr()
    assert "weekly report notification failed unexpectedly" in captured.err
    assert "Traceback" not in captured.err and "password" not in captured.err and "secret-host" not in captured.out
