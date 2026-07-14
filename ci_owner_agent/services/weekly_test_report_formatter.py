from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo
import re

from ci_owner_agent.schemas import TestFileFailureStat
from ci_owner_agent.services.notification_formatter import format_test_maintainer_mentions
from ci_owner_agent.services.test_maintainer_mapping import TestMaintainer
from ci_owner_agent.services.weekly_test_report_config import WeeklyTestReportConfig


def format_weekly_test_report(
    stats: list[TestFileFailureStat], *, period_start: datetime, period_end: datetime,
    config: WeeklyTestReportConfig, maintainers: dict[tuple, tuple[TestMaintainer, ...]] | None = None,
    repo: str | None = None, jobs: list[str] | None = None, branches: list[str] | None = None,
    unidentified_item_count: int = 0, missing_timestamp_count: int = 0, top_n: int | None = None,
    failed_build_count: int = 0, mention_mode: str = "userid",
) -> str:
    maintainers = maintainers or {}
    limit = top_n or config.topN
    important = [s for s in stats if s.isImportant]
    normal = [s for s in stats if not s.isImportant and s.periodFailedBuildCount >= config.normal.minimumWeeklyFailedBuildCount]
    selected_important = important[:limit]
    remaining = max(0, limit - len(selected_important))
    selected_normal = normal[:remaining] if config.normal.includeBelowThreshold else []
    omitted = max(0, len(important) - len(selected_important)) + max(0, len(normal) - len(selected_normal))
    scopes = sorted({(s.repo, s.job, s.branch) for s in stats})
    completed = sum(max((s.periodCompletedBuildCount for s in stats if (s.repo, s.job, s.branch) == scope), default=0) for scope in scopes)
    lines = [
        "### 📊 CI 高频失败测试周报",
        f"统计周期：{period_start.astimezone(ZoneInfo(config.timezone)):%Y-%m-%d %H:%M} ～ {period_end.astimezone(ZoneInfo(config.timezone)):%Y-%m-%d %H:%M} ({config.timezone})",
        f"范围：{repo or '*'} / {','.join(jobs or ['*'])} / {','.join(branches or ['*'])}",
        "",
        f"周期有效构建：{completed}",
        f"出现测试失败的构建：{failed_build_count}",
        f"失败测试文件：{len(important) + len(normal)}",
        f"达到重点处理阈值：{len(important)}",
        f"未达到重点处理阈值：{len(normal)}",
    ]
    if selected_important:
        lines.extend(["", "#### 🔴 重点处理", ""])
        for index, stat in enumerate(selected_important, 1):
            route = maintainers.get((stat.repo, stat.job, stat.branch, stat.testFilePath), ())
            mention = format_test_maintainer_mentions(route, mention_mode) if config.notification.mentionMaintainersForImportantItems else ""
            lines.extend(_item_lines(index, stat, mention))
    if normal:
        lines.extend(["", "#### 🟡 其他失败测试", ""])
        if config.normal.includeBelowThreshold:
            for index, stat in enumerate(selected_normal, 1):
                lines.extend(_item_lines(index, stat, ""))
        else:
            lines.append(f"共 {len(normal)} 个测试文件未达到重点处理阈值，本次不通知维护人。")
    lines.extend([
        "", "#### ⚠️ 数据质量", "",
        f"- 无法识别测试文件的失败项：{unidentified_item_count} 个",
        f"- 缺少真实构建时间的历史记录：{missing_timestamp_count} 条",
    ])
    return _fit_wecom_limit(lines, omitted)


def _item_lines(index: int, stat: TestFileFailureStat, mention: str) -> list[str]:
    lines = [
        f"{index}. {stat.testFilePath}",
        f"   - 周期失败：{stat.periodFailedBuildCount} 次 / {stat.periodCompletedBuildCount} 次构建",
        f"   - 周期失败率：{stat.periodFailureRate:.1%}",
        f"   - 当前连续失败：{stat.currentConsecutiveFailureCount} 次",
        f"   - 历史累计失败构建：{stat.totalFailedBuildCount} 次",
        f"   - 历史累计失败项：{stat.totalFailureItemCount} 个",
        f"   - 最近失败：#{stat.lastFailureBuildNumber or '-'}",
    ]
    if stat.matchedRuleNames:
        lines.append(f"   - 触发规则：{', '.join(stat.matchedRuleNames)}")
    if mention:
        lines.append(f"   - 测试维护人：{mention}")
    return lines


def _fit_wecom_limit(lines: list[str], omitted: int, max_bytes: int = 4000) -> str:
    """Drop complete trailing item blocks, never cut Markdown in the middle."""
    placeholder = "另有 9999 项因 Top N 或消息长度限制未展示。"
    while len("\n".join(lines + ([placeholder] if omitted or len("\n".join(lines).encode("utf-8")) > max_bytes else [])).encode("utf-8")) > max_bytes:
        starts = [index for index, line in enumerate(lines) if re.match(r"^\d+\. ", line)]
        if not starts:
            break
        start = starts[-1]
        end = next((index for index in range(start + 1, len(lines))
                    if lines[index].startswith("#### ")), len(lines))
        del lines[start:end]
        omitted += 1
    if omitted:
        quality = next((index for index, line in enumerate(lines) if line == "#### ⚠️ 数据质量"), len(lines))
        lines[quality:quality] = [f"另有 {omitted} 项因 Top N 或消息长度限制未展示。", ""]
    return "\n".join(lines)
