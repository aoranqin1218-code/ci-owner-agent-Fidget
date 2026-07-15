from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from ci_owner_agent.services.wecom_bot_models import WeComBotReply
from tests.test_history_store import make_store


def _isolated_subprocess_env(extra_env: dict[str, str] | None = None) -> dict[str, str]:
    """Minimal env for subprocess tests, isolating from user .env and real config."""
    env = {
        "CI_AGENT_MODEL_PROVIDER": "fake",
        "CI_AGENT_WECOM_BOT_ENABLED": "false",
        "CI_AGENT_WECOM_BOT_LLM_ENABLED": "false",
        "CI_AGENT_WECOM_BOT_LLM_MAX_INPUT_CHARS": "2000",
    }
    if extra_env:
        env.update(extra_env)
    return env


def test_serve_wecom_bot_missing_mongo():
    """serve-wecom-bot should fail when history is disabled."""
    result = subprocess.run(
        [sys.executable, "-m", "ci_owner_agent", "serve-wecom-bot", "--bot-id", "test", "--secret", "test"],
        capture_output=True,
        text=True,
        timeout=15,
        env={**_isolated_subprocess_env(), "CI_AGENT_HISTORY_ENABLED": "false"},
    )
    assert result.returncode == 2
    assert "MongoDB history storage must be enabled" in result.stderr


def test_serve_wecom_bot_disabled():
    result = subprocess.run(
        [sys.executable, "-m", "ci_owner_agent", "serve-wecom-bot", "--bot-id", "test", "--secret", "test"],
        capture_output=True,
        text=True,
        timeout=15,
        env={**_isolated_subprocess_env(), "CI_AGENT_HISTORY_ENABLED": "true"},
    )
    assert result.returncode == 2
    assert "must be enabled" in result.stderr


def test_serve_wecom_bot_missing_bot_id():
    result = subprocess.run(
        [sys.executable, "-m", "ci_owner_agent", "serve-wecom-bot", "--secret", "test"],
        capture_output=True,
        text=True,
        timeout=15,
        env={
            **_isolated_subprocess_env(),
            "CI_AGENT_HISTORY_ENABLED": "true",
            "CI_AGENT_WECOM_BOT_ENABLED": "true",
            "CI_AGENT_WECOM_BOT_ID": "",
        },
    )
    assert result.returncode == 2
    assert "Bot ID is required" in result.stderr


def test_wecom_bot_reply_model():
    """Verify WeComBotReply model."""
    text_reply = WeComBotReply(reply_type="text", text="hello")
    assert text_reply.reply_type == "text"
    assert text_reply.text == "hello"

    card_reply = WeComBotReply(
        reply_type="template_card",
        template_card={"card_type": "button_interaction", "task_id": "test"},
    )
    assert card_reply.reply_type == "template_card"
    assert card_reply.template_card["card_type"] == "button_interaction"