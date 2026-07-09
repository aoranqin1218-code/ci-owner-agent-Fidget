from __future__ import annotations

from ci_owner_agent.config import _parse_fallback_userids, load_settings


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
    monkeypatch.delenv("CI_AGENT_WECOM_FALLBACK_USERIDS", raising=False)
    settings = load_settings()
    assert settings.wecom_fallback_userids == ()

def test_wecom_fallback_userids_are_parsed_from_env(monkeypatch):
    monkeypatch.setenv("CI_AGENT_WECOM_FALLBACK_USERIDS", " ci.owner, team.leader,ci.owner,, ")

    settings = load_settings()

    assert settings.wecom_fallback_userids == ("ci.owner", "team.leader")
