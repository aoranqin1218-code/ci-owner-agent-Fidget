from __future__ import annotations

import re

from ci_owner_agent.services.wecom_bot_models import ParsedFeedbackIntent, WeComInboundMessage

_CODE = r"CI-[A-HJ-KM-NP-Z2-9]{6}"
_LIST_A = re.compile(rf"^查看\s*({_CODE})$", re.I)
_LIST_B = re.compile(rf"^({_CODE})\s*查看反馈$", re.I)
_CREATE = re.compile(rf"^({_CODE})\s+(\d+)\s+(.+)$", re.I)

_CORRECT_OWNER_RE = re.compile(
    r"^(责任人改为|改为|应该是)\s+@([A-Za-z]+)-([^\s-]+)$",
    re.ASCII,
)

_DISPLAY_NAME_RE = re.compile(
    r"^([A-Za-z]+)-[^\s-]+$",
    re.ASCII,
)

def derive_wecom_userid(display_name: str) -> str | None:
    if not display_name:
        return None
    match = _DISPLAY_NAME_RE.fullmatch(display_name.strip())
    if match is None:
        return None
    return match.group(1).lower()


def parse_correct_owner_target(action_text: str) -> tuple[str, str] | None:
    match = _CORRECT_OWNER_RE.fullmatch(action_text.strip())
    if match is None:
        return None
    prefix = match.group(2)
    name = match.group(3)
    display_name = f"{prefix}-{name}"
    userid = derive_wecom_userid(display_name)
    if userid is None:
        return None
    return userid, display_name


def clean_feedback_text(message: WeComInboundMessage) -> str:
    text = message.content.replace("\u3000", " ")
    if message.bot_userid:
        text = text.replace(f"<@{message.bot_userid}>", " ")
    text = re.sub(r"^\s*@[^\s]+\s+", "", text)
    return " ".join(text.split())


def parse_fixed_feedback_intent(
    message: WeComInboundMessage,
) -> ParsedFeedbackIntent | None:
    text = clean_feedback_text(message)
    if text.lower() in {"\u5e2e\u52a9", "help", "?"}:
        return ParsedFeedbackIntent(intent_type="help")
    match = _LIST_A.fullmatch(text) or _LIST_B.fullmatch(text)
    if match:
        return ParsedFeedbackIntent(intent_type="list_feedback", feedback_code=match.group(1).upper())
    match = _CREATE.fullmatch(text)
    if not match:
        return None
    code, index_raw, action_text = match.groups()
    action_text = action_text.strip()
    action = _action(action_text)
    if action is None:
        return None
    target_userid = None
    target_name = None
    if action == "correct_owner":
        result = parse_correct_owner_target(action_text)
        if result is None:
            return None
        target_userid, target_name = result
    return ParsedFeedbackIntent(
        intent_type="create_feedback",
        action=action,
        feedback_code=code.upper(),
        item_index=int(index_raw),
        target_userid=target_userid,
        target_display_name=target_name,
    )


def parse_feedback_intent(message: WeComInboundMessage) -> ParsedFeedbackIntent:
    result = parse_fixed_feedback_intent(message)
    if result is not None:
        return result
    return ParsedFeedbackIntent(
        intent_type="unknown",
        error="\u672a\u8bc6\u522b\u547d\u4ee4\uff0c\u8bf7\u53d1\u9001\u201c\u5e2e\u52a9\u201d\u67e5\u770b\u7528\u6cd5\u3002",
    )


def _action(text: str) -> str | None:
    lowered = text.lower()
    if lowered in {"\u5224\u65ad\u6b63\u786e", "\u6b63\u786e", "\u8d23\u4efb\u4eba\u6b63\u786e"}:
        return "confirm_owner"
    if lowered in {"\u6807\u8bb0\u5076\u53d1", "\u5076\u53d1", "flaky", "\u73af\u5883\u95ee\u9898"}:
        return "mark_flaky"
    if lowered in {"\u65e0\u6cd5\u5b9a\u8d23", "\u65e0\u8d23\u4efb\u4eba", "\u65e0\u6cd5\u786e\u5b9a\u8d23\u4efb\u4eba"}:
        return "mark_no_owner"
    if re.match(r"^(\u8d23\u4efb\u4eba\u6539\u4e3a|\u6539\u4e3a|\u5e94\u8be5\u662f)\s+@", text, re.ASCII):
        return "correct_owner"
    return None
