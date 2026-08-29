from __future__ import annotations

import json

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
    out = []
    for para in payload["content"]["post"]["zh_cn"]["content"]:
        for el in para:
            if el.get("tag") in {"text", "a"}:
                out.append(el.get("text", ""))
    return "\n".join(out)


def test_success_notice_payload_is_post_with_link():
    notice = CiResponsibilityNotice.model_validate(
        notice_payload([], result="SUCCESS", owner={"type": "no_high_confidence_owner", "name": "无高可信责任人", "email": None, "commit": None, "confidence": 0})
    )
    payload, summary = format_payload(notice)
    assert payload["msg_type"] == "post"
    assert summary.total_item_count == 0
    text = all_text(payload)
    assert "构建成功" in text
    assert "fidget-build" in payload["content"]["post"]["zh_cn"]["title"]
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
    assert payload["content"]["post"]["zh_cn"]["title"].startswith("❌")


def test_mapped_owner_at_rendered_once_and_deduped():
    item_a = current_item(name="张三", title="F-1")
    item_b = current_item(name="张三", title="F-2")
    notice = CiResponsibilityNotice.model_validate(notice_payload([item_a, item_b]))
    payload, summary = format_payload(
        notice,
        owner_open_ids={"张三": "ou_owner123"},
    )
    assert summary.at_open_ids == ("ou_owner123",)
    at_tags = [el for para in payload["content"]["post"]["zh_cn"]["content"] for el in para if el.get("tag") == "at"]
    assert len(at_tags) == 1
    assert at_tags[0]["user_id"] == "ou_owner123"
    assert at_tags[0]["user_name"] == "张三"
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
    at_tags = [el for para in payload["content"]["post"]["zh_cn"]["content"] for el in para if el.get("tag") == "at"]
    assert at_tags and at_tags[0]["user_id"] == "ou_maint456"


def test_many_items_keep_totals_and_omission_note():
    items = [current_item(name="张三", title=f"F-{i}") for i in range(12)]
    notice = CiResponsibilityNotice.model_validate(notice_payload(items))
    payload, summary = format_payload(notice)
    assert summary.total_item_count == 12
    assert summary.shown_item_count == 5
    assert summary.omitted_item_count == 7
    text = all_text(payload)
    assert "共 12 项" in text
    assert "已省略 7 项，共 12 项" in text
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


def test_payload_is_official_post_without_wecom_markdown():
    notice = CiResponsibilityNotice.model_validate(notice_payload([current_item(), no_owner_item()]))
    payload, _ = format_payload(notice)
    raw = json.dumps(payload, ensure_ascii=False)
    assert payload["msg_type"] == "post"
    assert "zh_cn" in payload["content"]["post"]
    assert "<@userid>" not in raw and "<@" not in raw
    assert "markdown" not in raw
    assert "###" not in raw


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
    assert "原因：本次构建多个模块编译失败，导致单元测试无法运行。" in text
    assert "🛠️ 建议：" in text
    assert "先修复编译错误，再重跑全量单测。" in text
    assert "若为环境问题，检查依赖安装是否完整。" in text


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
