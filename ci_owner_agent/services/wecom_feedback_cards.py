"""Pure rendering helpers for WeCom feedback template cards.

The feedback state machine supplies already-authorized data. This module only
applies WeCom-specific card structure, copy and field-length limits.
"""
from __future__ import annotations

from typing import Any


_MAX_CONFIRM_SUBTITLE_CHARS = 112


def build_status_card(
    *,
    title: str,
    desc: str,
    task_id: str,
    action_url: str,
    status: str,
    userids: list[str] | None = None,
) -> dict[str, Any]:
    card: dict[str, Any] = {
        "card_type": "text_notice",
        "main_title": {"title": title, "desc": desc},
        "card_action": {"type": 1, "url": action_url},
        "task_id": task_id,
    }
    if userids is not None:
        card["userids"] = userids
    return card


def build_confirmation_card(
    *,
    original_owner: Any,
    target_owner: Any,
    action: str | None,
    item_index: Any,
    repo: Any,
    build_number: Any,
    job: Any,
    branch: Any,
    sender: Any,
    failure_title: Any,
    note: Any,
    confirm_ttl_seconds: int,
    task_id: str,
) -> dict[str, Any]:
    """Render the initial confirmation card without reading or writing state."""
    horizontal_content = [{"type": 0, "keyname": "原责任人", "value": truncate_card_text(original_owner, 26)}]
    if action == "correct_owner":
        horizontal_content.append(
            {"type": 0, "keyname": "新责任人", "value": truncate_card_text(target_owner or "未指定", 26)}
        )
    horizontal_content.extend(
        [
            {"type": 0, "keyname": "责任项", "value": str(item_index)},
            {"type": 0, "keyname": "构建", "value": truncate_card_text(f"{repo} #{build_number}", 26)},
            {"type": 0, "keyname": "任务", "value": truncate_card_text(job, 26)},
            {"type": 0, "keyname": "有效期", "value": f"{max(1, confirm_ttl_seconds // 60)} 分钟"},
        ]
    )
    if len(horizontal_content) > 6:
        raise ValueError("horizontal content exceeds WeCom limit")
    desc_lines = [
        "异常：" + truncate_card_text(failure_title or "-", 72),
        "分支：" + truncate_card_text(short_branch(branch), 30),
        "发起人：" + truncate_card_text(sender, 20),
    ]
    if note:
        desc_lines.append("备注：" + truncate_card_text(note, 40))
    return _build_button_card(
        title="确认 CI 反馈",
        desc="操作：" + truncate_card_text(action_label(action), 24),
        desc_lines=desc_lines,
        horizontal_content_list=horizontal_content,
        task_id=task_id,
    )


def card_status_title(status: str, default: str = "操作失败") -> str:
    return {
        "cancelled": "反馈已取消",
        "forbidden": "无权限",
        "expired": "确认已过期",
        "not_found": "反馈不存在",
        "applied": "反馈已提交",
        "applying": "处理中",
        "failed": "反馈写入失败",
        "stale": "反馈已失效",
        "completed": "反馈已提交",
        "already_processed": "已处理",
    }.get(status, default)


def card_status_desc(status: str, default: str = "请稍后重试。") -> str:
    return {
        "cancelled": "未写入正式反馈。",
        "forbidden": "只有反馈发起人可以确认或取消。",
        "expired": "请重新发起反馈。",
        "not_found": "该反馈记录不存在。",
        "applied": "结果正在同步。",
        "applying": "反馈正在提交，请勿重复操作。",
        "failed": "请重新发起反馈。",
        "stale": "该责任项已更新，请重新发起反馈。",
        "completed": "该反馈已成功写入。",
        "already_processed": "该反馈已经处理。",
    }.get(status, default)


def truncate_card_text(value: Any, max_chars: int) -> str:
    text = " ".join(str(value or "").split()).strip()
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    if max_chars == 1:
        return "…"
    return text[: max_chars - 1] + "…"


def short_branch(value: Any) -> str:
    branch = str(value or "").strip()
    for prefix in ("refs/remotes/origin/", "refs/heads/", "origin/"):
        if branch.startswith(prefix):
            return branch[len(prefix):]
    return branch


def build_confirmation_subtitle(lines: list[str]) -> str:
    result: list[str] = []
    remaining = _MAX_CONFIRM_SUBTITLE_CHARS
    for line in lines:
        normalized = " ".join(str(line or "").split()).strip()
        if not normalized:
            continue
        separator_size = 1 if result else 0
        available = remaining - separator_size
        if available <= 0:
            break
        value = truncate_card_text(normalized, available)
        result.append(value)
        remaining -= len(value) + separator_size
    return "\n".join(result)


def action_label(action: str | None) -> str:
    return {
        "confirm_owner": "判断正确",
        "correct_owner": "修正责任人",
        "mark_flaky": "标记偶发",
        "mark_no_owner": "无法定责",
    }.get(action or "", action or "未知")


def _build_button_card(
    *,
    title: str,
    desc: str,
    desc_lines: list[str],
    task_id: str,
    horizontal_content_list: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    card: dict[str, Any] = {
        "card_type": "button_interaction",
        "main_title": {"title": title, "desc": desc},
        "sub_title_text": build_confirmation_subtitle(desc_lines),
        "button_list": [
            {"text": "确认提交", "style": 1, "key": "confirm"},
            {"text": "取消", "style": 2, "key": "cancel"},
        ],
        "task_id": task_id,
    }
    if horizontal_content_list:
        if len(horizontal_content_list) > 6:
            raise ValueError("horizontal content exceeds WeCom limit")
        card["horizontal_content_list"] = horizontal_content_list
    return card
