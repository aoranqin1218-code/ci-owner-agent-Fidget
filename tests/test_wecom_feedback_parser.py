from __future__ import annotations

import pytest

from ci_owner_agent.services.wecom_bot_models import WeComInboundMessage, WeComMentionedUser
from ci_owner_agent.services.wecom_feedback_parser import parse_feedback_intent


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


@pytest.mark.parametrize(
    ("text", "action"),
    [
        ("CI-7K3M9Q 1 判断正确", "confirm_owner"),
        ("ci-7k3m9q　1　正确", "confirm_owner"),
        ("CI-7K3M9Q 1 偶发", "mark_flaky"),
        ("CI-7K3M9Q 1 flaky", "mark_flaky"),
        ("CI-7K3M9Q 1 无法定责", "mark_no_owner"),
    ],
)
def test_parses_feedback_actions(text, action):
    intent = parse_feedback_intent(message(text))
    assert intent.intent_type == "create_feedback"
    assert intent.action == action
    assert intent.feedback_code == "CI-7K3M9Q"


def test_correct_owner_parses_display_name_from_action_text():
    intent = parse_feedback_intent(message("<@bot> CI-7K3M9Q 1 责任人改为 @AoranQin-秦奥然"))
    assert intent.action == "correct_owner"
    assert intent.target_userid == "aoranqin"
    assert intent.target_display_name == "AoranQin-秦奥然"


def test_correct_owner_parses_display_name_no_structured_mentions():
    intent = parse_feedback_intent(message("<@bot> CI-7K3M9Q 1 责任人改为 @AoranQin-秦奥然", mentions=()))
    assert intent.action == "correct_owner"
    assert intent.target_userid == "aoranqin"
    assert intent.target_display_name == "AoranQin-秦奥然"


def test_correct_owner_parses_gaiwei_variant():
    intent = parse_feedback_intent(message("CI-7K3M9Q 1 改为 @JAMES-李明"))
    assert intent.action == "correct_owner"
    assert intent.target_userid == "james"
    assert intent.target_display_name == "JAMES-李明"


def test_correct_owner_parses_yinggai_shi_variant():
    intent = parse_feedback_intent(message("CI-7K3M9Q 1 应该是 @AoranQin-秦奥然"))
    assert intent.action == "correct_owner"
    assert intent.target_userid == "aoranqin"


def test_correct_owner_rejects_invalid_format_no_prefix():
    intent = parse_feedback_intent(message("CI-7K3M9Q 1 改为 @李四"))
    assert intent.intent_type == "unknown"
    assert "英文字母userid" in (intent.error or "")


@pytest.mark.parametrize(
    ("text", "intent_type"),
    [("查看 CI-7K3M9Q", "list_feedback"), ("CI-7K3M9Q 查看反馈", "list_feedback"), ("帮助", "help"), ("help", "help"), ("?", "help"), ("确认 A7K9", "confirm_pending"), ("取消 A7K9", "cancel_pending")],
)
def test_parses_non_create_commands(text, intent_type):
    assert parse_feedback_intent(message(text)).intent_type == intent_type


@pytest.mark.parametrize(
    ("action_text", "expect_error_keyword"),
    [
        ("责任人改为 @aoran123-秦奥然", "英文字母userid"),
        ("责任人改为 @aoran_qin-秦奥然", "英文字母userid"),
        ("责任人改为 @aoran.qin-秦奥然", "英文字母userid"),
        ("责任人改为 @aoran-qin-秦奥然", "英文字母userid"),
        ("责任人改为 @秦奥然", "英文字母userid"),
        ("责任人改为 @AoranQin-", "英文字母userid"),
        ("AoranQin-秦奥然", "未识别"),
        ("责任人改为 @AoranQin-秦奥然 @James-李明", "英文字母userid"),
    ],
)
def test_correct_owner_rejects_invalid_display_name_format(action_text, expect_error_keyword):
    intent = parse_feedback_intent(message(f"CI-7K3M9Q 1 {action_text}"))
    assert intent.intent_type == "unknown"
    assert expect_error_keyword in (intent.error or "")
    assert intent.action is None
    assert intent.target_userid is None


def test_correct_owner_ignores_extra_mentions():
    intent = parse_feedback_intent(
        message("CI-7K3M9Q 1 责任人改为 @AoranQin-秦奥然", mentions=(("wrong_user", "Wrong Name"),))
    )
    assert intent.action == "correct_owner"
    assert intent.target_userid == "aoranqin"


def test_unknown_does_not_become_feedback():
    assert parse_feedback_intent(message("随便聊聊")).intent_type == "unknown"

