from __future__ import annotations

import pytest

from ci_owner_agent.services.wecom_bot_models import WeComInboundMessage, WeComMentionedUser
from ci_owner_agent.services.wecom_feedback_parser import (
    clean_feedback_text,
    derive_wecom_userid,
    parse_fixed_feedback_intent,
    parse_feedback_intent,
)


def message(content: str, *, mentions=()) -> WeComInboundMessage:
    users = [WeComMentionedUser(userid=userid, display_name=name) for userid, name in mentions]
    return WeComInboundMessage(
        event_key="msg:1",
        sender_userid="reviewer",
        sender_name="王五",
        content=content,
        bot_userid="bot",
        mentioned_userids=[item.userid for item in users],
        mentioned_users=users,
    )


# ---- derive_wecom_userid ----

@pytest.mark.parametrize(
    ("display_name", "expected"),
    [
        ("AoranQin-秦奥然", "aoranqin"),
        ("JAMES-李明", "james"),
    ],
)
def test_derive_wecom_userid_success(display_name, expected):
    assert derive_wecom_userid(display_name) == expected


@pytest.mark.parametrize(
    "display_name",
    [
        "aoran123-秦奥然",
        "aoran_qin-秦奥然",
        "aoran.qin-秦奥然",
        "aoran-qin-秦奥然",
        "İ-秦奥然",
        "ı-秦奥然",
        "ſ-秦奥然",
        "AoranQin-",
        "AoranQin-秦-奥然",
        "AoranQin-秦 奥然",
    ],
)
def test_derive_wecom_userid_rejects_invalid(display_name):
    assert derive_wecom_userid(display_name) is None


# ---- clean_feedback_text ----

def test_clean_feedback_text_removes_bot_mention():
    msg = message("<@bot> CI-7K3M9Q 1 判断正确")
    assert clean_feedback_text(msg) == "CI-7K3M9Q 1 判断正确"


def test_clean_feedback_text_normalizes_spaces():
    msg = message("CI-7K3M9Q\u30001\u3000判断正确")
    assert clean_feedback_text(msg) == "CI-7K3M9Q 1 判断正确"


def test_clean_feedback_text_removes_leading_at_mention():
    msg = message("@someone CI-7K3M9Q 1 判断正确", mentions=())
    assert clean_feedback_text(msg) == "CI-7K3M9Q 1 判断正确"


def test_clean_feedback_text_preserves_inline_at_for_correct_owner():
    msg = message("CI-7K3M9Q 1 责任人改为 @AoranQin-秦奥然")
    cleaned = clean_feedback_text(msg)
    assert "AoranQin-秦奥然" in cleaned
    assert "@AoranQin-秦奥然" in cleaned


# ---- parse_fixed_feedback_intent ----

@pytest.mark.parametrize(
    ("text", "action"),
    [
        ("CI-7K3M9Q 1 判断正确", "confirm_owner"),
        ("ci-7k3m9q\u30001\u3000正确", "confirm_owner"),
        ("CI-7K3M9Q 1 偶发", "mark_flaky"),
        ("CI-7K3M9Q 1 flaky", "mark_flaky"),
        ("CI-7K3M9Q 1 无法定责", "mark_no_owner"),
    ],
)
def test_parse_fixed_feedback_actions(text, action):
    intent = parse_fixed_feedback_intent(message(text))
    assert intent is not None
    assert intent.intent_type == "create_feedback"
    assert intent.action == action
    assert intent.feedback_code == "CI-7K3M9Q"


def test_fixed_correct_owner_parses_display_name():
    intent = parse_fixed_feedback_intent(message("CI-7K3M9Q 1 责任人改为 @AoranQin-秦奥然"))
    assert intent is not None
    assert intent.action == "correct_owner"
    assert intent.target_userid == "aoranqin"
    assert intent.target_display_name == "AoranQin-秦奥然"


def test_fixed_correct_owner_gaiwei_variant():
    intent = parse_fixed_feedback_intent(message("CI-7K3M9Q 1 改为 @JAMES-李明"))
    assert intent is not None
    assert intent.action == "correct_owner"
    assert intent.target_userid == "james"


def test_fixed_correct_owner_yinggai_shi_variant():
    intent = parse_fixed_feedback_intent(message("CI-7K3M9Q 1 应该是 @AoranQin-秦奥然"))
    assert intent is not None
    assert intent.action == "correct_owner"
    assert intent.target_userid == "aoranqin"


@pytest.mark.parametrize(
    "text",
    [
        "CI-7K3M9Q 1 责任人改为 @aoran123-秦奥然",
        "CI-7K3M9Q 1 责任人改为 @aoran_qin-秦奥然",
        "CI-7K3M9Q 1 责任人改为 @秦奥然",
        "CI-7K3M9Q 1 责任人改为 @AoranQin-",
        "CI-7K3M9Q 1 责任人改为 AoranQin-秦奥然",
        "CI-7K3M9Q 1 责任人改为 @AoranQin-秦奥然 @James-李明",
    ],
)
def test_fixed_correct_owner_returns_none_for_invalid(text):
    assert parse_fixed_feedback_intent(message(text)) is None


@pytest.mark.parametrize(
    ("text", "intent_type"),
    [
        ("查看 CI-7K3M9Q", "list_feedback"),
        ("CI-7K3M9Q 查看反馈", "list_feedback"),
        ("帮助", "help"),
        ("help", "help"),
        ("?", "help"),
    ],
)
def test_fixed_parses_non_create_commands(text, intent_type):
    intent = parse_fixed_feedback_intent(message(text))
    assert intent is not None
    assert intent.intent_type == intent_type


@pytest.mark.parametrize(
    "text",
    [
        "CI-7K3M9Q 第一条判断没问题",
        "把 CI-7K3M9Q 第一条责任人改成 @AoranQin-秦奥然",
        "CI-7K3M9Q 第2项像是偶发问题",
        "CI-7K3M9Q 1 责任人改为 @aoran123-秦奥然",
        "帮我看看 CI-7K3M9Q 的反馈",
        "随便聊聊",
    ],
)
def test_fixed_returns_none_for_non_fixed_commands(text):
    """These should NOT match fixed commands and should return None to trigger LLM."""
    assert parse_fixed_feedback_intent(message(text)) is None


# ---- parse_feedback_intent (compat wrapper) ----

def test_parse_feedback_intent_unknown():
    """Legacy wrapper returns unknown for non-fixed commands."""
    intent = parse_feedback_intent(message("随便聊聊"))
    assert intent.intent_type == "unknown"
    assert intent.error is not None


def test_parse_feedback_intent_uses_fixed_first():
    """Legacy wrapper returns fixed parse result when matched."""
    intent = parse_feedback_intent(message("CI-7K3M9Q 1 判断正确"))
    assert intent.intent_type == "create_feedback"
    assert intent.action == "confirm_owner"