from __future__ import annotations

import json
import re

from ci_owner_agent.schemas import CiResponsibilityNotice
from ci_owner_agent.services.feishu_notification_formatter import (
    FEISHU_POST_MAX_JSON_BYTES,
    format_feishu_notice_payload,
)


def notice_payload(items, **overrides):
    payload = {
        "repo": "fxp-fidget",
        "job": "fidget-build",
        "buildNumber": 313,
        "buildUrl": "https://jenkins.test/job/fidget-build/313/",
        "result": "FAILURE",
        "branch": "master",
        "owner": {"type": "no_high_confidence_owner", "name": "无高可信责任人", "email": None, "commit": None, "confidence": 0},
        "failureReason": "构建失败。",
        "evidence": [],
        "responsibilityItems": items,
        "suggestions": [],
        "hasHighConfidenceOwner": False,
    }
    payload.update(overrides)
    return payload


def current_item(name="张三", email="zs@example.com", title="F-100: group by fails", medium=False, reason="期望值不符"):
    return {
        "failureId": "failure-1",
        "failureTitle": title,
        "failureSignature": None,
        "failureSummary": None,
        "testFilePath": None,
        "failureFilePath": None,
        "owner": {"type": "medium_confidence" if medium else "high_confidence", "name": name, "email": email, "commit": "abc", "confidence": 0.8 if medium else 0.9},
        "responsibilityType": "current_build_owner",
        "sourceBuildNumber": 313,
        "sourceBuildUrl": None,
        "sourceCommit": "abc",
        "matchType": None,
        "relationship": None,
        "confidence": 0.8 if medium else 0.9,
        "reason": reason,
        "evidenceIds": [],
    }


def no_owner_item(title="T-1: flaky", reason="证据不足，无法确定高可信责任人。"):
    return {
        "failureId": "failure-2",
        "failureTitle": title,
        "failureSignature": None,
        "failureSummary": None,
        "testFilePath": "packages/core/test/fooTest.ts",
        "failureFilePath": None,
        "owner": {"type": "no_high_confidence_owner", "name": "无高可信责任人", "email": None, "commit": None, "confidence": 0},
        "responsibilityType": "no_high_confidence_owner",
        "sourceBuildNumber": None,
        "sourceBuildUrl": None,
        "sourceCommit": None,
        "matchType": None,
        "relationship": None,
        "confidence": 0,
        "reason": reason,
        "evidenceIds": [],
    }


def inherited_item(name="李四", email="ls@example.com"):
    return {
        "failureId": "failure-3",
        "failureTitle": "I-9: inherited failure",
        "failureSignature": None,
        "failureSummary": None,
        "testFilePath": None,
        "failureFilePath": None,
        "owner": {"type": "inherited_failure_owner", "name": name, "email": email, "commit": "old", "confidence": 0.85},
        "responsibilityType": "inherited_failure_owner",
        "sourceBuildNumber": 100,
        "sourceBuildUrl": None,
        "sourceCommit": "old",
        "matchType": None,
        "relationship": None,
        "confidence": 0.85,
        "reason": "与历史构建失败表现一致，责任继承。",
        "evidenceIds": [],
    }


def format_payload(notice, **kwargs):
    defaults = dict(
        jenkins_link=notice.buildUrl,
        owner_open_ids={},
        maintainer_open_ids={},
        item_maintainer_names=[],
        build_maintainer_names=[],
    )
    defaults.update(kwargs)
    return format_feishu_notice_payload(notice, **defaults)


def all_text(payload):
    card = payload["card"]
    out = [card["header"]["title"]["content"]]
    out.extend(el.get("content", "") for el in card["body"]["elements"] if el.get("tag") == "markdown")
    text = "\n".join(out)
    return re.sub(r"\\([\\*_~`\[\]])", r"\1", text)


def mention_ids(payload):
    return re.findall(r"<at id=([^>]+)></at>", json.dumps(payload, ensure_ascii=False))


def test_success_notice_payload_is_interactive_card_with_link():
    notice = CiResponsibilityNotice.model_validate(
        notice_payload([], result="SUCCESS", owner={"type": "no_high_confidence_owner", "name": "无高可信责任人", "email": None, "commit": None, "confidence": 0})
    )
    payload, summary = format_payload(notice)
    assert payload["msg_type"] == "interactive"
    assert summary.total_item_count == 0
    text = all_text(payload)
    assert "构建成功" in text
    assert "fidget-build" in payload["card"]["header"]["title"]["content"]
    assert "查看 Jenkins 分析" in text
    assert "https://jenkins.test/job/fidget-build/313/" in json.dumps(payload)


def test_failure_payload_keeps_build_identity_stats_and_link():
    notice = CiResponsibilityNotice.model_validate(notice_payload([current_item()]))
    payload, summary = format_payload(notice)
    text = all_text(payload)
    assert "共 1 项" in text
    assert "当前引入 1" in text
    assert summary.total_item_count == 1 and summary.omitted_item_count == 0
    assert "查看 Jenkins 分析" in text
    title = payload["card"]["header"]["title"]["content"]
    assert title == "❌ CI 构建失败 | fidget-build #313"
    assert "• 项目：fxp-fidget" in text
    assert "• 分支：master" in text


def test_mapped_owner_at_rendered_once_and_deduped():
    item_a = current_item(name="张三", title="F-1")
    item_b = current_item(name="张三", title="F-2")
    notice = CiResponsibilityNotice.model_validate(notice_payload([item_a, item_b]))
    payload, summary = format_payload(
        notice,
        owner_open_ids={"张三": "ou_owner123"},
    )
    assert summary.at_open_ids == ("ou_owner123",)
    assert mention_ids(payload) == ["ou_owner123"]
    text = all_text(payload)
    assert text.count("张三") == 2  # 第一项带 @，第二项仅姓名


def test_unmapped_owner_degrades_to_name_text():
    notice = CiResponsibilityNotice.model_validate(notice_payload([current_item(name="王五", email="ww@example.com")]))
    payload, summary = format_payload(notice)
    assert summary.at_open_ids == ()
    assert summary.unmapped_owner_names == ("王五",)
    assert "未完成身份映射" in all_text(payload)


def test_no_owner_maintainer_is_not_written_as_causal_owner():
    notice = CiResponsibilityNotice.model_validate(notice_payload([no_owner_item()]))
    payload, _ = format_payload(
        notice,
        maintainer_open_ids={"李四": "ou_maint456"},
        item_maintainer_names=["李四"],
    )
    text = all_text(payload)
    assert "待确认维护者：李四" in text
    assert "责任人：李四" not in text
    assert mention_ids(payload) == ["ou_maint456"]


def test_many_items_keep_totals_and_omission_note():
    items = [current_item(name="张三", title=f"F-{i}") for i in range(12)]
    notice = CiResponsibilityNotice.model_validate(notice_payload(items))
    payload, summary = format_payload(notice)
    assert summary.total_item_count == 12
    assert summary.shown_item_count == 5
    assert summary.omitted_item_count == 7
    text = all_text(payload)
    assert "共 12 项" in text
    assert "另有 7 项未展开（共 12 项）" in text
    assert "查看 Jenkins 分析" in text


def test_coverage_item_rendered_as_coverage_label():
    coverage = current_item(name="秦奥然", email="aq@example.com", title="coverage branches,lines below threshold for src/generator/SqlUtils.ts", medium=True)
    coverage.update(
        {
            "failureSignature": "coverage_threshold_failure|packages/fidget-sql/src/generator/sqlutils.ts",
            "failureFilePath": "packages/fidget-sql/src/generator/SqlUtils.ts",
            "reason": "single author 秦奥然 modified packages/fidget-sql/src/generator/SqlUtils.ts in focus range",
        }
    )
    notice = CiResponsibilityNotice.model_validate(notice_payload([coverage]))
    payload, _ = format_payload(notice, owner_open_ids={"秦奥然": "ou_cov"})
    text = all_text(payload)
    assert "覆盖率未达标" in text
    assert "责任人（中等置信）：秦奥然" in text


def test_payload_is_official_interactive_card_with_bold_sections_and_spacing():
    notice = CiResponsibilityNotice.model_validate(notice_payload([current_item(), no_owner_item()]))
    payload, _ = format_payload(notice)
    raw = json.dumps(payload, ensure_ascii=False)
    assert payload["msg_type"] == "interactive"
    assert payload["card"]["schema"] == "2.0"
    assert payload["card"]["header"]["template"] == "blue"
    assert payload["card"]["config"]["width_mode"] == "fill"
    elements = payload["card"]["body"]["elements"]
    markdown = [element for element in elements if element["tag"] == "markdown"]
    assert markdown and all(element["margin"] for element in markdown)
    assert any("**📋 构建概览**" in element["content"] for element in markdown)
    assert any("**🔎 失败原因**" in element["content"] for element in markdown)
    assert any("**👥 责任明细**" in element["content"] for element in markdown)
    assert "<@userid>" not in raw and "<@" not in raw
    assert "###" not in raw


def test_section_titles_tightly_follow_content_with_no_blank_lines():
    notice = CiResponsibilityNotice.model_validate(
        notice_payload(
            [current_item(name="张三"), current_item(name="李四", title="F-2: second", reason="日志失败证据：xx")],
            failureReason="本 build 共 2 个失败：F-1 由渲染器改动引起；F-2 由包装改动引起。",
            suggestions=["先修复渲染器，再验证。"],
        )
    )
    payload, _ = format_payload(notice, owner_open_ids={"张三": "ou_owner"})
    markdown = [el for el in payload["card"]["body"]["elements"] if el["tag"] == "markdown"]

    overview = next(el for el in markdown if el["content"].startswith("**📋 构建概览**"))
    overview_lines = overview["content"].splitlines()
    assert overview_lines[0] == "**📋 构建概览**"
    assert overview_lines[1].startswith("• ")
    assert "" not in overview_lines

    reason = next(el for el in markdown if el["content"].startswith("**🔎 失败原因**"))
    reason_lines = reason["content"].splitlines()
    assert reason_lines[0] == "**🔎 失败原因**"
    assert reason_lines[1].startswith("本 build 共 2 个失败")
    assert not reason_lines[1].startswith("•")
    assert reason_lines[2].startswith("• F-1")
    assert reason_lines[3].startswith("• F-2")
    assert "" not in reason_lines

    items = [el for el in markdown if "**🔥 当前引入｜" in el["content"]]
    assert len(items) == 2
    first_item_lines = items[0]["content"].splitlines()
    assert first_item_lines[0] == "**👥 责任明细**"  # 大区块标题并入首个责任项元素，与责任项标题直接相连
    assert first_item_lines[1].startswith("**🔥 当前引入｜")
    assert first_item_lines[2].startswith("👤 责任人：张三")
    assert "" not in first_item_lines
    second_item_lines = items[1]["content"].splitlines()
    assert second_item_lines[0].startswith("**🔥 当前引入｜")
    assert "" not in second_item_lines

    suggestions = next(el for el in markdown if el["content"].startswith("**🛠️ 修复建议**"))
    suggestion_lines = suggestions["content"].splitlines()
    assert suggestion_lines[0] == "**🛠️ 修复建议**"
    assert suggestion_lines[1].startswith("• ")
    assert "" not in suggestion_lines

    # 区块间距由“上一段内容的结束”承担：大区块标题不携带大间距，区块最后一项承担区块间距。
    section_margin = "0px 0px 16px 0px"
    item_margin = "0px 0px 12px 0px"
    assert overview["margin"] == section_margin
    assert reason["margin"] == section_margin
    assert items[0]["margin"] == item_margin
    assert items[1]["margin"] == section_margin  # 责任明细区块最后一项承担区块间距
    assert suggestions["margin"] == section_margin

    # 功能不变项：卡片仍为 fill 宽度
    assert payload["card"]["config"]["width_mode"] == "fill"


def test_payload_respects_byte_budget():
    notice = CiResponsibilityNotice.model_validate(
        notice_payload([current_item(name="张三", title="long-title-" + "x" * 800, reason="long reason " + "y" * 2000) for _ in range(12)])
    )
    payload, _ = format_payload(notice)
    assert FEISHU_POST_MAX_JSON_BYTES == 20 * 1024
    assert len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) <= FEISHU_POST_MAX_JSON_BYTES


def test_failure_reason_and_suggestions_appear_in_payload():
    notice = CiResponsibilityNotice.model_validate(
        notice_payload(
            [no_owner_item()],
            failureReason="本次构建多个模块编译失败，导致单元测试无法运行。",
            suggestions=["先修复编译错误，再重跑全量单测。", "若为环境问题，检查依赖安装是否完整。"],
        )
    )
    payload, _ = format_payload(notice)
    text = all_text(payload)
    assert "**🔎 失败原因**" in text
    assert "本次构建多个模块编译失败，导致单元测试无法运行" in text
    assert "• 本次构建多个模块编译失败" not in text
    assert "**🛠️ 修复建议**" in text
    assert "• 先修复编译错误，再重跑全量单测。" in text
    assert "• 若为环境问题，检查依赖安装是否完整。" in text


def test_reason_and_evidence_are_split_into_key_points():
    item = current_item(
        title="F-1003: Group by main field(select name, sum(score), sum(nested.score)) (<duration>)",
        reason=(
            "日志失败证据：期望结果包含 sum_score，实际结果缺失。"
            "diff 证据：渲染器过滤了主字段聚合。"
            "测试断言：原有测试要求两个聚合结果并存。"
        ),
    )
    notice = CiResponsibilityNotice.model_validate(
        notice_payload(
            [item],
            failureReason=(
                "本 build 共 2 个失败，均由渲染器改动导致："
                "F-1003 由聚合路由改动引起；O-0503 由 OID 包装改动引起。"
            ),
            suggestions=["修复聚合路由。", "修复 OID 包装。"],
        )
    )
    payload, _ = format_payload(notice)
    text = all_text(payload)
    reason_section = next(
        element["content"]
        for element in payload["card"]["body"]["elements"]
        if element.get("content", "").startswith("**🔎 失败原因**")
    )
    reason_lines = reason_section.splitlines()
    assert reason_lines[0] == "**🔎 失败原因**"
    assert reason_lines[1].startswith("本 build 共 2 个失败")
    assert not reason_lines[1].startswith("•")
    assert reason_lines[2].startswith("• F-1003 由聚合路由改动引起")
    assert reason_lines[3].startswith("• O-0503 由 OID 包装改动引起")
    assert "• F-1003 由聚合路由改动引起" in text
    assert "• O-0503 由 OID 包装改动引起" in text
    assert "🔥 当前引入｜F-1003：Group by main field" in text
    assert "• 日志：期望结果包含 sum_score，实际结果缺失" in text
    assert "• 代码：渲染器过滤了主字段聚合" in text
    assert "测试：原有测试要求" not in text  # 每项只保留最关键的日志与代码两点


def test_long_point_uses_explicit_jenkins_hint_instead_of_truncation_ellipsis():
    notice = CiResponsibilityNotice.model_validate(
        notice_payload(
            [current_item(reason="日志失败证据：" + "关键证据" * 100)],
            failureReason="失败原因" * 100,
            suggestions=["处理建议" * 100],
        )
    )
    payload, _ = format_payload(notice)
    text = all_text(payload)
    assert "内容较长，详见 Jenkins" in text
    assert not any(line.endswith("...") or line.endswith("…") for line in text.splitlines())


def test_fallback_not_used_when_owner_reachable():
    notice = CiResponsibilityNotice.model_validate(notice_payload([current_item(name="张三")]))
    payload, summary = format_payload(
        notice,
        owner_open_ids={"张三": "ou_owner"},
        fallback_open_ids=("ou_fb",),
    )
    assert summary.at_open_ids == ("ou_owner",)
    assert "兜底通知" not in all_text(payload)


def test_fallback_used_only_when_no_one_reachable():
    notice = CiResponsibilityNotice.model_validate(notice_payload([no_owner_item()]))
    payload, summary = format_payload(
        notice,
        item_maintainer_names=["李四"],
        fallback_open_ids=("ou_fb",),
    )
    assert summary.at_open_ids == ("ou_fb",)
    assert "兜底通知" in all_text(payload)


def test_inherited_failure_rendered_as_historical():
    notice = CiResponsibilityNotice.model_validate(notice_payload([inherited_item()]))
    payload, _ = format_payload(notice)
    text = all_text(payload)
    assert "历史持续" in text
    assert "责任人：李四" in text


def test_integration_env_failure_not_rewritten_as_code_owner():
    env_item = no_owner_item(title="integration: connection to mongo failed", reason="ECONNREFUSED 连接外部数据库失败，属环境/基础设施问题，无法归责代码提交者。")
    notice = CiResponsibilityNotice.model_validate(notice_payload([env_item]))
    payload, _ = format_payload(notice)
    text = all_text(payload)
    assert "待确认" in text
    assert "责任人：" not in text


def test_mixed_failure_distinguishes_categories_in_stats():
    notice = CiResponsibilityNotice.model_validate(
        notice_payload([current_item(name="张三", title="F-1"), inherited_item(name="李四"), no_owner_item()])
    )
    payload, _ = format_payload(notice)
    text = all_text(payload)
    assert "共 3 项" in text
    assert "当前引入 1" in text
    assert "历史持续 1" in text
    assert "待确认 1" in text
