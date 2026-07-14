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


def test_correct_owner_uses_structured_mention():
    intent = parse_feedback_intent(message("<@bot> CI-7K3M9Q 1 责任人改为 @李四", mentions=(("bot", None), ("lisi", "李四"))))
    assert intent.action == "correct_owner"
    assert intent.target_userid == "lisi"
    assert intent.target_display_name == "李四"


def test_correct_owner_rejects_multiple_targets():
    intent = parse_feedback_intent(
        message("CI-7K3M9Q 1 改为 @李四", mentions=(("lisi", "李四"), ("wang", "王五")))
    )
    assert intent.intent_type == "unknown"
    assert "只 @一个" in (intent.error or "")


@pytest.mark.parametrize(
    ("text", "intent_type"),
    [("查看 CI-7K3M9Q", "list_feedback"), ("CI-7K3M9Q 查看反馈", "list_feedback"), ("帮助", "help"), ("help", "help"), ("?", "help"), ("确认 A7K9", "confirm_pending"), ("取消 A7K9", "cancel_pending")],
)
def test_parses_non_create_commands(text, intent_type):
    assert parse_feedback_intent(message(text)).intent_type == intent_type


def test_unknown_does_not_become_feedback():
    assert parse_feedback_intent(message("随便聊聊")).intent_type == "unknown"

