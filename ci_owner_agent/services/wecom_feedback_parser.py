from __future__ import annotations

import re

from ci_owner_agent.services.wecom_bot_models import ParsedFeedbackIntent, WeComInboundMessage

_CODE = r"CI-[A-HJ-KM-NP-Z2-9]{6}"
_CONFIRM = re.compile(r"^(确认|取消)\s*([A-HJ-KM-NP-Z2-9]{4})$", re.I)
_LIST_A = re.compile(rf"^查看\s*({_CODE})$", re.I)
_LIST_B = re.compile(rf"^({_CODE})\s*查看反馈$", re.I)
_CREATE = re.compile(rf"^({_CODE})\s+(\d+)\s+(.+)$", re.I)


def parse_feedback_intent(message: WeComInboundMessage) -> ParsedFeedbackIntent:
    text = _clean_text(message.content, message)
    if text.lower() in {"帮助", "help", "?"}:
        return ParsedFeedbackIntent(intent_type="help")
    match = _CONFIRM.fullmatch(text)
    if match:
        return ParsedFeedbackIntent(
            intent_type="confirm_pending" if match.group(1) == "确认" else "cancel_pending",
            confirmation_code=match.group(2).upper(),
        )
    match = _LIST_A.fullmatch(text) or _LIST_B.fullmatch(text)
    if match:
        return ParsedFeedbackIntent(intent_type="list_feedback", feedback_code=match.group(1).upper())
    match = _CREATE.fullmatch(text)
    if not match:
        return ParsedFeedbackIntent(intent_type="unknown", error="未识别命令，请发送“帮助”查看用法。")
    code, index_raw, action_text = match.groups()
    action_text = action_text.strip()
    action = _action(action_text)
    if action is None:
        return ParsedFeedbackIntent(intent_type="unknown", error="未识别反馈动作，请发送“帮助”查看用法。")
    target_userid = None
    target_name = None
    if action == "correct_owner":
        candidates = [item for item in message.mentioned_users if item.userid != message.bot_userid]
        if len(candidates) != 1:
            return ParsedFeedbackIntent(
                intent_type="unknown",
                error="修正责任人时请只 @一个新责任人。",
            )
        target_userid = candidates[0].userid
        target_name = candidates[0].display_name or _display_name(action_text) or target_userid
    return ParsedFeedbackIntent(
        intent_type="create_feedback",
        action=action,
        feedback_code=code.upper(),
        item_index=int(index_raw),
        target_userid=target_userid,
        target_display_name=target_name,
    )


def _clean_text(content: str, message: WeComInboundMessage) -> str:
    text = content.replace("\u3000", " ")
    if message.bot_userid:
        text = text.replace(f"<@{message.bot_userid}>", " ")
    text = re.sub(r"^\s*@[^\s]+\s+", "", text)
    return " ".join(text.split())


def _action(text: str) -> str | None:
    lowered = text.lower()
    if lowered in {"判断正确", "正确", "责任人正确"}:
        return "confirm_owner"
    if lowered in {"标记偶发", "偶发", "flaky", "环境问题"}:
        return "mark_flaky"
    if lowered in {"无法定责", "无责任人", "无法确定责任人"}:
        return "mark_no_owner"
    if re.match(r"^(责任人改为|改为|应该是)\s*(@\S+|<@[^>]+>)$", text, re.I):
        return "correct_owner"
    return None


def _display_name(text: str) -> str | None:
    match = re.search(r"@([^\s>]+)$", text)
    return match.group(1) if match else None

