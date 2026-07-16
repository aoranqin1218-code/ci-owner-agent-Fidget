from __future__ import annotations

from ci_owner_agent.config import _parse_fallback_userids, load_settings, public_settings


def test_parse_fallback_userids_empty():
    assert _parse_fallback_userids(None) == ()
    assert _parse_fallback_userids("") == ()
    assert _parse_fallback_userids("  , , ") == ()


def test_parse_fallback_userids_single():
    assert _parse_fallback_userids("ci.owner") == ("ci.owner",)


def test_parse_fallback_userids_multiple():
    assert _parse_fallback_userids("ci.owner,team.leader") == ("ci.owner", "team.leader")


def test_parse_fallback_userids_dedup():
    result = _parse_fallback_userids("ci.owner, team.leader, ci.owner")
    assert result == ("ci.owner", "team.leader")


def test_parse_fallback_userids_trims_spaces():
    result = _parse_fallback_userids(" ci.owner , team.leader,ci.owner,, ")
    assert result == ("ci.owner", "team.leader")


def test_parse_fallback_userids_via_env(monkeypatch):
    monkeypatch.setenv("CI_AGENT_WECOM_FALLBACK_USERIDS", " ci.owner, team.leader,ci.owner,, ")
    settings = load_settings()
    assert settings.wecom_fallback_userids == ("ci.owner", "team.leader")


def test_parse_fallback_userids_default_empty(monkeypatch):
    monkeypatch.setenv("CI_AGENT_WECOM_FALLBACK_USERIDS", "")
    settings = load_settings()
    assert settings.wecom_fallback_userids == ()


def test_test_maintainer_mapping_file_is_public(monkeypatch, tmp_path):
    path = tmp_path / "test-maintainers.yml"
    monkeypatch.setenv("CI_AGENT_TEST_MAINTAINER_MAPPING_FILE", str(path))

    settings = load_settings()

    assert settings.test_maintainer_mapping_file == path
    assert public_settings(settings)["test_maintainer_mapping_file"] == str(path)

def test_wecom_fallback_userids_are_parsed_from_env(monkeypatch):
    monkeypatch.setenv("CI_AGENT_WECOM_FALLBACK_USERIDS", " ci.owner, team.leader,ci.owner,, ")

    settings = load_settings()

    assert settings.wecom_fallback_userids == ("ci.owner", "team.leader")


def test_wecom_bot_defaults_disabled(monkeypatch):
    for name in ("CI_AGENT_WECOM_BOT_ENABLED", "CI_AGENT_WECOM_BOT_ID", "CI_AGENT_WECOM_BOT_SECRET"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CI_AGENT_WECOM_BOT_ENABLED", "false")
    monkeypatch.setenv("CI_AGENT_WECOM_BOT_ID", "")
    monkeypatch.setenv("CI_AGENT_WECOM_BOT_SECRET", "")
    settings = load_settings()
    assert settings.wecom_bot_enabled is False
    assert settings.wecom_bot_id is None
    assert settings.wecom_bot_secret is None


def test_wecom_bot_settings_and_secret_redaction(monkeypatch):
    monkeypatch.setenv("CI_AGENT_WECOM_BOT_ENABLED", "true")
    monkeypatch.setenv("CI_AGENT_WECOM_BOT_ID", "bot-placeholder")
    monkeypatch.setenv("CI_AGENT_WECOM_BOT_SECRET", "secret-placeholder")
    monkeypatch.setenv("CI_AGENT_WECOM_BOT_CONFIRM_TTL_SECONDS", "600")
    monkeypatch.setenv("CI_AGENT_WECOM_FEEDBACK_CODE_TTL_DAYS", "40")
    monkeypatch.setenv("CI_AGENT_WECOM_BOT_EVENT_TTL_DAYS", "9")
    settings = load_settings()
    assert settings.wecom_bot_enabled is True
    assert settings.wecom_bot_confirm_ttl_seconds == 600
    assert settings.wecom_feedback_code_ttl_days == 40
    assert settings.wecom_bot_event_ttl_days == 9
    assert public_settings(settings)["wecom_bot_secret"] == "***"


def test_wecom_bot_chat_discovery_defaults_disabled(monkeypatch):
    monkeypatch.setenv("CI_AGENT_WECOM_BOT_DISCOVER_CHAT_ID", "false")
    assert load_settings().wecom_bot_discover_chat_id is False


def test_wecom_bot_chat_discovery_enabled_from_env(monkeypatch):
    monkeypatch.setenv("CI_AGENT_WECOM_BOT_DISCOVER_CHAT_ID", "true")
    assert load_settings().wecom_bot_discover_chat_id is True
