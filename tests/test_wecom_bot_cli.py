from __future__ import annotations

import sys

from ci_owner_agent.main import main
from tests.test_history_store import make_store


def test_serve_wecom_bot_requires_mongodb(monkeypatch, capsys):
    monkeypatch.setenv("CI_AGENT_HISTORY_ENABLED", "false")
    assert main(["serve-wecom-bot", "--bot-id", "placeholder", "--secret", "placeholder"]) == 2
    assert "MongoDB history storage must be enabled" in capsys.readouterr().err


def test_serve_wecom_bot_requires_credentials(monkeypatch, capsys):
    monkeypatch.setenv("CI_AGENT_HISTORY_ENABLED", "true")
    monkeypatch.delenv("CI_AGENT_WECOM_BOT_ID", raising=False)
    monkeypatch.delenv("CI_AGENT_WECOM_BOT_SECRET", raising=False)
    assert main(["serve-wecom-bot"]) == 2
    assert "Bot ID is required" in capsys.readouterr().err


def test_serve_wecom_bot_missing_optional_sdk_is_clear(monkeypatch, capsys):
    monkeypatch.setenv("CI_AGENT_HISTORY_ENABLED", "true")
    monkeypatch.setattr("ci_owner_agent.main.get_history_store", lambda settings: make_store())
    monkeypatch.setitem(sys.modules, "aibot", None)
    result = main(["serve-wecom-bot", "--bot-id", "placeholder", "--secret", "placeholder"])
    assert result == 2
    assert "wecom-aibot-python-sdk is required" in capsys.readouterr().err

