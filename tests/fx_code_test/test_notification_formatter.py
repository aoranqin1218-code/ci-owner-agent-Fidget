from __future__ import annotations

import json
from dataclasses import replace

import pytest

from ci_owner_agent.schemas import CiResponsibilityNotice
from ci_owner_agent.config import load_settings
from ci_owner_agent.main import main
from ci_owner_agent.services.notification_formatter import (
    WECOM_SECTION_SPACERS,
    WECOM_VISUAL_SPACER,
    failure_category,
    format_wecom_markdown_notice,
    result_icon,
    responsibility_type_icon,
    source_build_label,
)
from ci_owner_agent.services.metrics import AnalysisMetricsRecorder, use_metrics_recorder
from ci_owner_agent.services.wecom_notification_routing import (
    collect_responsible_display_names,
    notification_digest,
)
from ci_owner_agent.services.wecom_notice_service import maybe_notify_notice as _maybe_notify_notice
from ci_owner_agent.services.wecom_notice_service import notify_notice as _notify_notice
from ci_owner_agent.services.test_maintainer_mapping import TestMaintainerResolver
from ci_owner_agent.services.wecom_user_mapping import WeComUserMapper
from tests.fx_code_test.test_history_store import make_store


def notice_payload(items):
    return {
        "repo": "sample-ts-repo",
        "job": "services/fx-code-unittest",
        "buildNumber": 5099,
        "buildUrl": "https://jenkins.example/job/5099/",
        "result": "FAILURE",
        "branch": "dev",
        "headCommit": "h",
        "baseCommit": "b",
        "owner": {"type": "no_high_confidence_owner", "name": "无高可信责任人", "email": None, "commit": None, "confidence": 0},
        "failureReason": "本 build 包含多个失败，责任项见下方。",
        "evidence": [
            {"id": "E1", "type": "log", "summary": "日志显示 Webhook触发 Timeout", "detail": "detail", "source": "log"},
            {"id": "E2", "type": "diff", "summary": "", "detail": "diff 显示 webhook 相关逻辑变更" * 20, "source": "diff"},
        ],
        "responsibilityItems": items,
        "suggestions": [],
        "hasHighConfidenceOwner": False,
    }


def item(owner_name="Tang.Tangerine-唐嘉伟", owner_type="inherited_failure_owner", responsibility_type="inherited_failure_owner", reason="历史持续失败。", owner_email="x@example.com", test_file_path=None, failure_file_path=None):
    return {
        "failureId": "failure-secret",
        "failureTitle": "EtlUtils - getInputEntryInfo",
        "failureSignature": "sig-secret",
        "failureSummary": "summary",
        "testFilePath": test_file_path,
        "failureFilePath": failure_file_path,
        "owner": {"type": owner_type, "name": owner_name, "email": owner_email, "commit": "secretcommit", "confidence": 0.9},
        "responsibilityType": responsibility_type,
        "sourceBuildNumber": 5094 if responsibility_type == "inherited_failure_owner" else 5099,
        "sourceBuildUrl": None,
        "sourceCommit": "secret-source",
        "matchType": "signature_exact",
        "relationship": "very_likely_same_failure",
        "confidence": 0.9,
        "reason": reason,
        "evidenceIds": ["E1", "E2"],
    }


def test_inherited_item_owner_is_mentioned_in_feishu_aligned_sections():
    notice = CiResponsibilityNotice.model_validate(notice_payload([item()]))
    markdown = format_wecom_markdown_notice(notice, feedback_base_url="http://ci-agent.test/feedback")
    assert "### ❌ CI 构建失败 | services/fx-code-unittest #5099" in markdown
    assert "**📋 构建概览**" in markdown
    assert "**🔎 失败原因**" in markdown
    assert "**👥 责任明细**" in markdown
    assert markdown.index("**📋 构建概览**") < markdown.index("**🔎 失败原因**") < markdown.index("**👥 责任明细**")
    assert WECOM_SECTION_SPACERS == (WECOM_VISUAL_SPACER,)
    section_break = "\n".join(WECOM_SECTION_SPACERS)
    assert f"\n{section_break}\n**🔎 失败原因**" in markdown
    assert f"\n{section_break}\n**👥 责任明细**" in markdown
    assert "👤 **责任人**：@Tang.Tangerine-唐嘉伟" in markdown
    assert "顶层结论" not in markdown
    assert "顶层结论：无高可信责任人" not in markdown


def test_failure_reasons_are_grouped_by_unit_and_integration_tests():
    unit = item(test_file_path="packages/fidget-core/test/CiOwnerAgentControlledFailureTest.ts")
    unit.update(
        {
            "failureTitle": "should expose a deterministic assertion failure for mixed-failure acceptance",
            "failureSummary": "AssertionError: expected 'unit-stage-actual' to equal 'unit-stage-expected'",
        }
    )
    first_integration = item(test_file_path="packages/fidget-sdk/test/integration/select-integration/F1003Test.ts")
    first_integration.update(
        {
            "failureTitle": "F-1003: Group by main field + aggregate main field",
            "failureSummary": "AssertionError: SharedPgObject expected values to deeply equal",
        }
    )
    second_integration = item(test_file_path="packages/fidget-sdk/test/integration/select-integration/O0503Test.ts")
    second_integration.update(
        {
            "failureTitle": "O-0503: CaseWhenProjection OID",
            "failureSummary": "AssertionError: actual OID does not match expected object_id",
        }
    )

    markdown = format_wecom_markdown_notice(
        CiResponsibilityNotice.model_validate(notice_payload([unit, first_integration, second_integration]))
    )
    reason_section = markdown.split("**🔎 失败原因**", maxsplit=1)[1].split("**👥 责任明细**", maxsplit=1)[0]

    assert "**单元测试：**" in reason_section
    assert "• CiOwnerAgentControlledFailureTest：AssertionError: expected 'unit-stage-actual'" in reason_section
    assert "**集成测试：**" in reason_section
    assert "• F-1003：AssertionError: SharedPgObject expected values to deeply equal" in reason_section
    assert "• O-0503：AssertionError: actual OID does not match expected object_id" in reason_section


def test_failure_reasons_separate_build_and_other_failures():
    build = item(
        "AoranQin-秦奥然",
        "high_confidence",
        "current_build_owner",
        "TypeScript 编译错误由当前改动直接引入。",
        "aoran@example.com",
        failure_file_path="packages/fidget-postgres/src/data/conversion/PgFieldValueConverter.ts",
    )
    build.update(
        {
            "failureTitle": "TS2552: Cannot find name 'value1'",
            "failureSignature": "typescript_compile_error|ts2552|packages/fidget-postgres/src/data/conversion/pgfieldvalueconverter.ts|value1",
            "failureSummary": "error TS2552: Cannot find name 'value1'",
        }
    )
    other = item(
        "无高可信责任人",
        "no_high_confidence_owner",
        "no_high_confidence_owner",
        "现有证据无法归入已知失败类型。",
        None,
    )
    other.update(
        {
            "failureTitle": "未分类的 Jenkins 失败",
            "failureSignature": "unclassified_failure|jenkins",
            "failureSummary": "无法确认具体失败阶段",
        }
    )

    markdown = format_wecom_markdown_notice(
        CiResponsibilityNotice.model_validate(notice_payload([build, other]))
    )
    reason_section = markdown.split("**🔎 失败原因**", maxsplit=1)[1].split("**👥 责任明细**", maxsplit=1)[0]

    assert "**构建失败：**" in reason_section
    assert "• TS2552: Cannot find name 'value1'：error TS2552" in reason_section
    assert "**其他失败：**" in reason_section
    assert "• 未分类的 Jenkins 失败：无法确认具体失败阶段" in reason_section
    assert "**单元测试：**" not in reason_section
    assert "**集成测试：**" not in reason_section


@pytest.mark.parametrize(
    ("signature", "title", "summary", "path"),
    [
        ("generic_wrapper|jenkins", "Jenkins agent offline", "节点已离线", None),
        ("scm_checkout_failure|git", "Git checkout failed", "无法拉取代码", None),
        ("infrastructure|disk", "No space left on device", "工作节点磁盘已满", None),
        ("permission_failure|workspace", "Permission denied", "工作区没有写权限", None),
        ("shell_wrapper|exit_2", "script returned exit code 2", "仅有外层退出码", None),
        ("environment|timeout", "Environment readiness timeout", "环境未就绪", None),
    ],
)
def test_unknown_non_test_failures_conservatively_use_other_category(
    signature: str,
    title: str,
    summary: str,
    path: str | None,
):
    candidate = item(
        "无高可信责任人",
        "no_high_confidence_owner",
        "no_high_confidence_owner",
        "现有证据不足。",
        None,
        failure_file_path=path,
    )
    candidate.update(
        {
            "failureSignature": signature,
            "failureTitle": title,
            "failureSummary": summary,
        }
    )
    parsed = CiResponsibilityNotice.model_validate(notice_payload([candidate]))

    assert failure_category(parsed.responsibilityItems[0]) == "other"
    markdown = format_wecom_markdown_notice(parsed)
    assert "**其他失败：**" in markdown
    assert "**单元测试：**" not in markdown
    assert "**集成测试：**" not in markdown


def test_integration_failure_heading_exposes_raw_occurrences_after_suite_aggregation():
    first = item(
        "无高可信责任人",
        "no_high_confidence_owner",
        "no_high_confidence_owner",
        "数据库不可达。",
        None,
    )
    first.update(
        {
            "failureId": "integration-a",
            "failureSignature": "integration_connection|delete-integration",
            "failureTitle": "集成测试数据库不可达（delete-integration）",
            "failureSummary": "集成测试数据库不可达（delete-integration）",
            "evidenceIds": ["IE1"],
        }
    )
    second = dict(first)
    second.update(
        {
            "failureId": "integration-b",
            "failureSignature": "integration_connection|insert-integration",
            "failureTitle": "集成测试数据库不可达（insert-integration）",
            "failureSummary": "集成测试数据库不可达（insert-integration）",
            "evidenceIds": ["IE2"],
        }
    )
    payload = notice_payload([first, second])
    payload["evidence"].extend(
        [
            {"id": "IE1", "type": "log", "summary": "delete", "detail": "suite=delete, occurrences=500, lines 1-500", "source": "integration_classifier"},
            {"id": "IE2", "type": "log", "summary": "insert", "detail": "suite=insert, occurrences=269, lines 501-769", "source": "integration_classifier"},
        ]
    )

    markdown = format_wecom_markdown_notice(CiResponsibilityNotice.model_validate(payload))

    assert "**集成测试（共 769 项原始失败，聚合为 2 组）：**" in markdown


def test_failure_notice_uses_wecom_compatible_responsibility_icons():
    no_owner = item(
        "无高可信责任人",
        "no_high_confidence_owner",
        "no_high_confidence_owner",
        "evidence is insufficient",
    )
    notice = CiResponsibilityNotice.model_validate(
        notice_payload(
            [
                item(),
                item("Li", "high_confidence", "current_build_owner", "current build failure", "li@example.com"),
                no_owner,
            ]
        )
    )

    markdown = format_wecom_markdown_notice(notice)

    assert "🧭" not in markdown
    assert "🧩" not in markdown
    assert "🧷" not in markdown
    assert "**🔎 失败原因**" in markdown
    assert "**👥 责任明细**" in markdown
    assert "• 来源：#5094" in markdown
    assert "🔥 当前引入" in markdown
    assert "♻️ 历史持续" in markdown
    assert "❓ 待确认" in markdown


def test_failure_notice_without_items_uses_compatible_unknown_fallback():
    notice = CiResponsibilityNotice.model_validate(notice_payload([]))

    markdown = format_wecom_markdown_notice(notice)

    assert "**其他失败：**" in markdown
    assert "**❓ 待确认｜未识别到独立责任项**" in markdown
    assert "• 来源：-" in markdown
    assert "🧩" not in markdown
    assert "🧷" not in markdown


def test_build_reason_without_items_still_uses_build_failure_category():
    payload = notice_payload([])
    payload["failureReason"] = "TypeScript 编译失败：error TS2552: Cannot find name 'value1'."
    notice = CiResponsibilityNotice.model_validate(payload)

    markdown = format_wecom_markdown_notice(notice)

    assert "**构建失败：**" in markdown
    assert "**其他失败：**" not in markdown


def test_responsibility_type_icon_unknown_fallback_is_compatible():
    assert responsibility_type_icon("current_build_owner") == "🔥"
    assert responsibility_type_icon("inherited_failure_owner") == "♻️"
    assert responsibility_type_icon("no_high_confidence_owner") == "❓"
    assert responsibility_type_icon("unexpected") == "❓"


def test_source_build_label_does_not_show_untrusted_no_owner_history():
    payload = notice_payload(
        [item("无高可信责任人", "no_high_confidence_owner", "no_high_confidence_owner", "证据不足。")]
    )
    payload["responsibilityItems"][0].update(
        {
            "sourceBuildNumber": 123,
            "sourceBuildUrl": "fake",
            "matchType": "signature_exact",
            "relationship": "very_likely_same_failure",
        }
    )
    notice = CiResponsibilityNotice.model_validate(payload)

    assert source_build_label(notice.responsibilityItems[0], notice) == "-"


def test_source_build_label_for_current_and_inherited_owner():
    current_notice = CiResponsibilityNotice.model_validate(
        notice_payload([item("Li", "high_confidence", "current_build_owner", "current", "li@example.com")])
    )
    inherited_notice = CiResponsibilityNotice.model_validate(notice_payload([item()]))

    assert source_build_label(current_notice.responsibilityItems[0], current_notice) == "#5099"
    assert source_build_label(inherited_notice.responsibilityItems[0], inherited_notice) == "#5094"


def test_responsible_name_collection_is_deduplicated_but_items_keep_their_own_owner():
    notice = CiResponsibilityNotice.model_validate(
        notice_payload([item("Tang"), item("Tang"), item("Mars", "high_confidence", "current_build_owner", "新失败。", "mars@example.com")])
    )
    assert collect_responsible_display_names(notice) == ["Tang", "Mars"]
    markdown = format_wecom_markdown_notice(notice)
    assert markdown.count("👤 **责任人**：@Tang") == 2
    assert markdown.count("👤 **责任人**：@Mars") == 1


def test_no_item_owner_displays_no_high_confidence_owner():
    no_owner = item("无高可信责任人", "no_high_confidence_owner", "no_high_confidence_owner", "证据不足。")
    notice = CiResponsibilityNotice.model_validate(notice_payload([no_owner]))
    assert "👤 **责任人**：无高可信责任人" in format_wecom_markdown_notice(notice)


def test_responsibility_type_labels_are_public_readable():
    current = item("Henry", "high_confidence", "current_build_owner", "新失败。", "henry@example.com")
    inherited = item("Tang", "inherited_failure_owner", "inherited_failure_owner", "历史持续。", "tang@example.com")
    no_owner = item("无高可信责任人", "no_high_confidence_owner", "no_high_confidence_owner", "证据不足。")
    notice = CiResponsibilityNotice.model_validate(notice_payload([current, inherited, no_owner]))

    markdown = format_wecom_markdown_notice(notice)

    assert "📌 **责任项**：共 3 项，当前引入 1，历史持续 1，待确认 1" in markdown
    assert "**🔥 当前引入｜EtlUtils - getInputEntryInfo**" in markdown
    assert "**♻️ 历史持续｜EtlUtils - getInputEntryInfo**" in markdown
    assert "**❓ 待确认｜EtlUtils - getInputEntryInfo**" in markdown
    assert "current_build_owner" not in markdown
    assert "inherited_failure_owner" not in markdown
    assert "no_high_confidence_owner" not in markdown


def test_coverage_item_is_rendered_in_chinese_without_changing_its_schema_data():
    coverage = item(
        "AoranQin-秦奥然",
        "medium_confidence",
        "current_build_owner",
        "single author AoranQin-秦奥然 modified packages/fidget-sql/src/generator/SqlUtils.ts in focus range",
        "aoranqin@example.com",
    )
    coverage.update(
        {
            "failureTitle": "coverage branches,lines,statements below threshold for src/generator/SqlUtils.ts",
            "failureSignature": "coverage_threshold_failure|packages/fidget-sql/src/generator/sqlutils.ts",
            "failureSummary": "coverage branches,lines,statements below threshold for src/generator/SqlUtils.ts",
            "failureFilePath": "packages/fidget-sql/src/generator/SqlUtils.ts",
            "confidence": 0.6,
            "sourceCommit": "h",
        }
    )
    payload = notice_payload([coverage])
    payload["suggestions"] = [
        "补充 SqlUtils.ts 中未覆盖分支的测试。",
        "c8 覆盖率阈值失败由确定性 reconciler 单独处理，本通知不为其分配责任人。",
    ]
    notice = CiResponsibilityNotice.model_validate(payload)

    markdown = format_wecom_markdown_notice(notice)

    assert "覆盖率未达标：src/generator/SqlUtils.ts（分支、行、语句）" in markdown
    assert "**责任人（中等置信）**：@AoranQin-秦奥然" in markdown
    assert "在本次责任排查范围内，仅 AoranQin-秦奥然 修改了 packages/fidget-sql/src/generator/SqlUtils.ts。" in markdown
    assert "coverage branches,lines,statements below threshold" not in markdown
    assert "single author AoranQin-秦奥然 modified" not in markdown
    assert "本通知不为其分配责任人" not in markdown


def test_mixed_failure_notice_distinguishes_continuing_coverage_and_includes_its_fix():
    japa = item("AoranQin-秦奥然", "high_confidence", "current_build_owner", "当前构建新增测试失败。", "aoranqin@example.com")
    japa.update({"sourceBuildNumber": 10, "sourceCommit": "build-10"})
    coverage = item(
        "AoranQin-秦奥然",
        "medium_confidence",
        "current_build_owner",
        "single author AoranQin-秦奥然 modified packages/fidget-sql/src/generator/SqlUtils.ts in focus range",
        "aoranqin@example.com",
    )
    coverage.update(
        {
            "failureTitle": "coverage branches,lines,statements below threshold for src/generator/SqlUtils.ts",
            "failureSignature": "coverage_threshold_failure|packages/fidget-sql/src/generator/sqlutils.ts",
            "failureSummary": "coverage branches,lines,statements below threshold for src/generator/SqlUtils.ts",
            "failureFilePath": "packages/fidget-sql/src/generator/SqlUtils.ts",
            "confidence": 0.6,
            "sourceBuildNumber": 10,
            "sourceCommit": "build-9",
        }
    )
    payload = notice_payload([japa, coverage])
    payload.update({"buildNumber": 10, "headCommit": "build-10"})
    notice = CiResponsibilityNotice.model_validate(payload)

    markdown = format_wecom_markdown_notice(notice)

    assert "📌 **责任项**：共 2 项，当前引入 1，覆盖率持续 1" in markdown
    assert "其他 1" not in markdown
    assert "**🔥 当前引入｜EtlUtils - getInputEntryInfo**" in markdown
    assert "**♻️ 覆盖率持续｜覆盖率未达标：src/generator/SqlUtils.ts（分支、行、语句）**" in markdown
    assert "• 🛠️ 建议：" not in markdown
    assert "**🛠️ 修复建议**" in markdown
    assert "• 补充 src/generator/SqlUtils.ts 中未覆盖分支的测试，使 分支、行、语句 达到配置阈值。" in markdown
    assert markdown.index("**🛠️ 修复建议**") > markdown.index("**♻️ 覆盖率持续｜")
    assert len(markdown.encode("utf-8")) <= 4096


def test_top_responsible_owners_use_mapper_userid_mention(tmp_path):
    path = tmp_path / "mapping.csv"
    path.write_text(
        "mappingKey,authorName,authorEmail,normalizedEmail,wecomUserId,mappingStatus,note\n"
        "Tang <x@example.com>,Tang,x@example.com,x@example.com,tang.userid,manual,\n",
        encoding="utf-8",
    )
    mapper = WeComUserMapper.from_csv(path)
    notice = CiResponsibilityNotice.model_validate(notice_payload([item("Tang")]))
    markdown = format_wecom_markdown_notice(notice, user_mapper=mapper)
    assert "👤 **责任人**：<@tang.userid>" in markdown


def test_responsibility_item_owner_uses_mapper_userid_mention(tmp_path):
    path = tmp_path / "mapping.csv"
    path.write_text(
        "mappingKey,authorName,authorEmail,normalizedEmail,wecomUserId,mappingStatus,note\n"
        "Tang <x@example.com>,Tang,x@example.com,x@example.com,tang.userid,manual,\n",
        encoding="utf-8",
    )
    mapper = WeComUserMapper.from_csv(path)
    notice = CiResponsibilityNotice.model_validate(notice_payload([item("Tang")]))
    markdown = format_wecom_markdown_notice(notice, user_mapper=mapper)
    assert "👤 **责任人**：<@tang.userid>" in markdown


def test_unmapped_owner_fallback_to_name_mention():
    notice = CiResponsibilityNotice.model_validate(notice_payload([item("Tang")]))
    markdown = format_wecom_markdown_notice(notice, user_mapper=WeComUserMapper([]))
    assert "👤 **责任人**：@Tang" in markdown
    assert "👤 **责任人**：@Tang" in markdown


def test_mention_mode_name_does_not_use_userid(tmp_path):
    path = tmp_path / "mapping.csv"
    path.write_text(
        "mappingKey,authorName,authorEmail,normalizedEmail,wecomUserId,mappingStatus,note\n"
        "Tang <x@example.com>,Tang,x@example.com,x@example.com,tang.userid,manual,\n",
        encoding="utf-8",
    )
    mapper = WeComUserMapper.from_csv(path)
    notice = CiResponsibilityNotice.model_validate(notice_payload([item("Tang")]))
    markdown = format_wecom_markdown_notice(notice, user_mapper=mapper, mention_mode="name")
    assert "<@tang.userid>" not in markdown
    assert "👤 **责任人**：@Tang" in markdown


def test_system_fields_are_not_displayed():
    notice = CiResponsibilityNotice.model_validate(notice_payload([item(reason="failureId abc failureSignature def signatureHash ghi 签名")]))
    markdown = format_wecom_markdown_notice(notice)
    assert "failure-secret" not in markdown
    assert "sig-secret" not in markdown
    assert "failureId" not in markdown
    assert "failureSignature" not in markdown
    assert "signatureHash" not in markdown
    assert "签名" not in markdown
    assert "secretcommit" not in markdown
    assert "secret-source" not in markdown


def test_item_reason_is_used_as_evidence():
    notice = CiResponsibilityNotice.model_validate(notice_payload([item(reason="当前 failure item 与历史构建 #5094 一致。")]))
    assert "• 依据：当前 failure item 与历史构建 #5094 一致。" in format_wecom_markdown_notice(notice)


def test_empty_reason_falls_back_to_evidence_ids():
    notice = CiResponsibilityNotice.model_validate(
        notice_payload([item("Henry", "high_confidence", "current_build_owner", reason="")])
    )
    markdown = format_wecom_markdown_notice(notice)
    assert "日志显示 Webhook触发 Timeout" in markdown


def test_suggestions_show_first_three_items():
    payload = notice_payload([item()])
    payload["suggestions"] = ["检查测试数据准备逻辑。", "优先验证历史持续失败。", "补充单测覆盖。", "第四条不展示。"]
    notice = CiResponsibilityNotice.model_validate(payload)

    markdown = format_wecom_markdown_notice(notice)

    assert "**🛠️ 修复建议**" in markdown
    assert "• 检查测试数据准备逻辑。" in markdown
    assert "• 优先验证历史持续失败。" in markdown
    assert "• 补充单测覆盖。" in markdown
    assert "第四条不展示" not in markdown


def test_suggestions_section_is_omitted_when_empty():
    notice = CiResponsibilityNotice.model_validate(notice_payload([item()]))

    assert "**🛠️ 修复建议**" not in format_wecom_markdown_notice(notice)


def test_result_icons_and_labels():
    expected = {
        "SUCCESS": "### ✅ CI 构建成功",
        "FAILURE": "### ❌ CI 构建失败",
        "UNSTABLE": "### ⚠️ CI 构建不稳定",
        "ABORTED": "### ⏹️ CI 构建中止",
        "UNKNOWN": "### ❔ CI 构建未知",
        "OTHER": "### ❔ CI 构建未知",
    }
    for result, title in expected.items():
        payload = notice_payload([item()])
        payload["result"] = result
        notice = CiResponsibilityNotice.model_validate(payload)
        assert title in format_wecom_markdown_notice(notice)
    assert result_icon("failure") == "❌"


def test_build_and_feedback_links():
    notice = CiResponsibilityNotice.model_validate(notice_payload([item()]))
    markdown = format_wecom_markdown_notice(notice, feedback_base_url="http://ci-agent.xxx/feedback")
    assert "**🔗 相关链接**" in markdown
    assert notice.buildUrl in markdown
    assert "🏗️ [查看 Jenkins 构建]" in markdown
    assert "📝 [提交反馈]" in markdown
    assert "job=services%2Ffx-code-unittest" in markdown
    assert "branch=dev" in markdown
    assert "build=5099" in markdown
    assert "📝 反馈：未配置" in format_wecom_markdown_notice(notice, feedback_base_url=None)


def test_feedback_link_can_include_shared_token():
    notice = CiResponsibilityNotice.model_validate(notice_payload([item()]))
    markdown = format_wecom_markdown_notice(
        notice,
        feedback_base_url="http://ci-agent.xxx/feedback",
        feedback_token="dev-token",
    )
    assert "token=dev-token" in markdown


def test_notify_notice_dry_run_outputs_markdown(tmp_path, capsys, monkeypatch):
    notice = CiResponsibilityNotice.model_validate(notice_payload([item()]))
    notice_file = tmp_path / "notice.json"
    notice_file.write_text(notice.model_dump_json(), encoding="utf-8")
    monkeypatch.setenv("CI_AGENT_MODEL_PROVIDER", "fake")
    rc = main(["notify-notice", "--notice-file", str(notice_file), "--dry-run", "--feedback-base-url", "http://ci-agent.test/feedback"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "### ❌ CI 构建失败 | services/fx-code-unittest #5099" in out
    assert "**🔗 相关链接**" in out


def test_notify_notice_dry_run_uses_mapping_file(tmp_path, capsys, monkeypatch):
    notice = CiResponsibilityNotice.model_validate(notice_payload([item("Tang")]))
    notice_file = tmp_path / "notice.json"
    notice_file.write_text(notice.model_dump_json(), encoding="utf-8")
    mapping_file = tmp_path / "mapping.csv"
    mapping_file.write_text(
        "mappingKey,authorName,authorEmail,normalizedEmail,wecomUserId,mappingStatus,note\n"
        "Tang <x@example.com>,Tang,x@example.com,x@example.com,tang.userid,manual,\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CI_AGENT_MODEL_PROVIDER", "fake")
    monkeypatch.setenv("CI_AGENT_WECOM_USER_MAPPING_FILE", str(mapping_file))
    monkeypatch.setenv("CI_AGENT_WECOM_MENTION_MODE", "userid")
    rc = main(["notify-notice", "--notice-file", str(notice_file), "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "<@tang.userid>" in out


def test_notify_notice_prefers_mongo_wecom_userid(monkeypatch):
    store = make_store()
    store.wecom_users.update_one(
        {"normalizedEmail": "x@example.com"},
        {"$set": {"authorName": "Tang", "normalizedEmail": "x@example.com", "wecomUserId": "mongo.userid", "commitCount": 1}},
        upsert=True,
    )
    notice = CiResponsibilityNotice.model_validate(notice_payload([item("Tang")]))
    settings = replace(load_settings(), notification_dedup_enabled=False, wecom_user_mapping_file=None)
    monkeypatch.setattr("ci_owner_agent.services.wecom_notice_service.get_history_store", lambda settings: store)

    result = _notify_notice(notice, settings, dry_run=True, force=False, feedback_base_url=None)

    assert "<@mongo.userid>" in result["markdown"]


def test_notify_notice_falls_back_to_csv_when_mongo_missing(tmp_path, monkeypatch):
    store = make_store()
    mapping_file = tmp_path / "mapping.csv"
    mapping_file.write_text(
        "mappingKey,authorName,authorEmail,normalizedEmail,wecomUserId,mappingStatus,note\n"
        "Tang <x@example.com>,Tang,x@example.com,x@example.com,csv.userid,manual,\n",
        encoding="utf-8",
    )
    notice = CiResponsibilityNotice.model_validate(notice_payload([item("Tang")]))
    settings = replace(load_settings(), notification_dedup_enabled=False, wecom_user_mapping_file=mapping_file)
    monkeypatch.setattr("ci_owner_agent.services.wecom_notice_service.get_history_store", lambda settings: store)

    result = _notify_notice(notice, settings, dry_run=True, force=False, feedback_base_url=None)

    assert "<@csv.userid>" in result["markdown"]


def test_notify_notice_falls_back_to_csv_when_history_store_unavailable(tmp_path, monkeypatch):
    mapping_file = tmp_path / "mapping.csv"
    mapping_file.write_text(
        "mappingKey,authorName,authorEmail,normalizedEmail,wecomUserId,mappingStatus,note\n"
        "Tang <x@example.com>,Tang,x@example.com,x@example.com,csv.userid,manual,\n",
        encoding="utf-8",
    )
    notice = CiResponsibilityNotice.model_validate(notice_payload([item("Tang")]))
    settings = replace(load_settings(), notification_dedup_enabled=False, wecom_user_mapping_file=mapping_file)
    monkeypatch.setattr("ci_owner_agent.services.wecom_notice_service.get_history_store", lambda settings: None)

    result = _notify_notice(notice, settings, dry_run=True, force=False, feedback_base_url=None)

    assert "<@csv.userid>" in result["markdown"]


def test_notify_notice_falls_back_to_name_when_no_mapping(monkeypatch):
    store = make_store()
    notice = CiResponsibilityNotice.model_validate(notice_payload([item("Tang")]))
    settings = replace(load_settings(), notification_dedup_enabled=False, wecom_user_mapping_file=None)
    monkeypatch.setattr("ci_owner_agent.services.wecom_notice_service.get_history_store", lambda settings: store)

    result = _notify_notice(notice, settings, dry_run=True, force=False, feedback_base_url=None)

    assert "👤 **责任人**：@Tang" in result["markdown"]


def test_notify_notice_mention_mode_name_ignores_mongo_userid(monkeypatch):
    store = make_store()
    store.wecom_users.update_one(
        {"normalizedEmail": "x@example.com"},
        {"$set": {"authorName": "Tang", "normalizedEmail": "x@example.com", "wecomUserId": "mongo.userid", "commitCount": 1}},
        upsert=True,
    )
    notice = CiResponsibilityNotice.model_validate(notice_payload([item("Tang")]))
    settings = replace(load_settings(), notification_dedup_enabled=False, wecom_user_mapping_file=None, wecom_mention_mode="name")
    monkeypatch.setattr("ci_owner_agent.services.wecom_notice_service.get_history_store", lambda settings: store)

    result = _notify_notice(notice, settings, dry_run=True, force=False, feedback_base_url=None)

    assert "<@mongo.userid>" not in result["markdown"]
    assert "👤 **责任人**：@Tang" in result["markdown"]


def test_notify_notice_missing_file_returns_error_without_traceback(tmp_path, capsys, monkeypatch):
    missing = tmp_path / "missing.json"
    monkeypatch.setenv("CI_AGENT_MODEL_PROVIDER", "fake")
    rc = main(["notify-notice", "--notice-file", str(missing), "--dry-run"])
    captured = capsys.readouterr()
    assert rc == 2
    assert "ERROR: notice file not found:" in captured.err
    assert str(missing) in captured.err
    assert "Traceback" not in captured.err
    assert captured.out == ""


def test_notify_notice_unexpected_error_returns_2_without_traceback(tmp_path, capsys, monkeypatch):
    notice = CiResponsibilityNotice.model_validate(notice_payload([item()]))
    notice_file = tmp_path / "notice.json"
    notice_file.write_text(notice.model_dump_json(), encoding="utf-8")
    monkeypatch.setenv("CI_AGENT_MODEL_PROVIDER", "fake")

    def raise_notify(*args, **kwargs):
        raise RuntimeError("mongodb://user:password@secret-host/db")

    monkeypatch.setattr("ci_owner_agent.services.wecom_notice_service.notify_notice", raise_notify)

    rc = main(["notify-notice", "--notice-file", str(notice_file), "--dry-run"])
    captured = capsys.readouterr()

    assert rc == 2
    assert "ERROR: notify failed unexpectedly" in captured.err
    assert "mongodb://" not in captured.err and "password" not in captured.err and "secret-host" not in captured.err
    assert "Traceback" not in captured.err
    assert captured.out == ""


def test_analyze_notify_dry_run_stdout_stays_json(monkeypatch, capsys):
    notice = CiResponsibilityNotice.model_validate(notice_payload([item()]))
    monkeypatch.setenv("CI_AGENT_MODEL_PROVIDER", "fake")
    monkeypatch.setattr("ci_owner_agent.cli.commands.analyze_local", lambda **kwargs: notice)
    rc = main(
        [
            "analyze-local",
            "--repo",
            "repo",
            "--job",
            "services/fx-code-unittest",
            "--build",
            "5099",
            "--base-commit",
            "b",
            "--head-commit",
            "h",
            "--console-file",
            "console.log",
            "--build-url",
            "local://job/5099",
            "--notify-dry-run",
            "--notify",
        ]
    )
    out = capsys.readouterr().out
    assert rc == 0
    parsed = __import__("json").loads(out)
    assert parsed["buildNumber"] == 5099
    assert "CI 单测失败" not in out


def test_maybe_notify_filters_no_owner_by_responsibility_items(monkeypatch):
    calls = []
    no_owner = item("无高可信责任人", "no_high_confidence_owner", "no_high_confidence_owner", "证据不足。")
    notice = CiResponsibilityNotice.model_validate(notice_payload([no_owner]))
    settings = replace(load_settings(), wecom_notify_on_no_owner=False)
    monkeypatch.setattr("ci_owner_agent.services.wecom_notice_service.notify_notice", lambda *args, **kwargs: calls.append(kwargs) or {"ok": True})

    _maybe_notify_notice(notice, settings, cli_notify=True, cli_dry_run=True, force=False)

    assert calls == []


def test_maybe_notify_allows_item_owner_when_no_owner_notify_disabled(monkeypatch):
    calls = []
    notice = CiResponsibilityNotice.model_validate(notice_payload([item()]))
    settings = replace(load_settings(), wecom_notify_on_no_owner=False)
    monkeypatch.setattr("ci_owner_agent.services.wecom_notice_service.notify_notice", lambda *args, **kwargs: calls.append(kwargs) or {"ok": True})

    _maybe_notify_notice(notice, settings, cli_notify=True, cli_dry_run=True, force=False)

    assert len(calls) == 1


def test_maybe_notify_records_sanitized_failure_in_metrics(monkeypatch):
    notice = CiResponsibilityNotice.model_validate(notice_payload([item()]))
    settings = replace(load_settings(), wecom_notify_enabled=True)
    recorder = AnalysisMetricsRecorder.start(
        enabled=True,
        job=notice.job,
        buildNumber=notice.buildNumber,
        repo=notice.repo or "repo",
        command="analyze",
    )
    monkeypatch.setattr(
        "ci_owner_agent.services.wecom_notice_service.notify_notice",
        lambda *args, **kwargs: {
            "ok": False,
            "status": "failed",
            "transport": "webhook",
            "error": "WeCom webhook request failed: ConnectionError",
        },
    )

    with use_metrics_recorder(recorder):
        result = _maybe_notify_notice(notice, settings, cli_notify=False, cli_dry_run=False, force=False)

    assert result and result["status"] == "failed"
    assert recorder.warnings == ["WeCom notification failed: WeCom webhook request failed: ConnectionError"]


def test_env_enabled_notify_skips_success_by_default(monkeypatch):
    calls = []
    payload = notice_payload([item()])
    payload["result"] = "SUCCESS"
    notice = CiResponsibilityNotice.model_validate(payload)
    monkeypatch.setenv("CI_AGENT_WECOM_NOTIFY_ENABLED", "true")
    monkeypatch.setenv("CI_AGENT_WECOM_NOTIFY_ON_SUCCESS", "false")
    settings = load_settings()
    monkeypatch.setattr("ci_owner_agent.services.wecom_notice_service.notify_notice", lambda *args, **kwargs: calls.append(kwargs) or {"ok": True})

    _maybe_notify_notice(notice, settings, cli_notify=False, cli_dry_run=False, force=False)

    assert calls == []


def test_notify_dry_run_does_not_enqueue(monkeypatch):
    store = make_store()
    notice = CiResponsibilityNotice.model_validate(notice_payload([item()]))
    settings = replace(load_settings(), notification_dedup_enabled=True)
    monkeypatch.setattr("ci_owner_agent.services.wecom_notice_service.get_history_store", lambda settings: store)

    result = _notify_notice(notice, settings, dry_run=True, force=False, feedback_base_url=None)

    assert result["status"] == "dry_run"
    assert store.wecom_notification_outbox.docs == []


def test_notify_webhook_without_mongo_sends_directly(tmp_path, monkeypatch):
    mapping_file = tmp_path / "wecom-users.csv"
    mapping_file.write_text(
        "authorName,authorEmail,normalizedEmail,mappingKey,wecomUserId,mappingStatus,note\n"
        "Tang.Tangerine-唐嘉伟,x@example.com,x@example.com,"
        '"Tang.Tangerine-唐嘉伟 <x@example.com>",tang.userid,confirmed,\n',
        encoding="utf-8",
    )
    notice = CiResponsibilityNotice.model_validate(notice_payload([item()]))
    settings = replace(load_settings(), history_enabled=False, wecom_notify_transport="webhook",
                       wecom_webhook_url="https://example.test/secret", wecom_notify_dry_run=False,
                       wecom_user_mapping_file=mapping_file, test_maintainer_mapping_file=None,
                       wecom_fallback_userids=(), wecom_mention_mode="userid")
    calls = []
    monkeypatch.setattr("ci_owner_agent.services.wecom_notice_service.get_history_store", lambda _: None)
    monkeypatch.setattr("ci_owner_agent.services.wecom_notice_service.send_wecom_markdown",
                        lambda url, markdown: calls.append((url, markdown)) or
                        {"ok": True, "statusCode": 200, "response": "ok", "error": None})
    result = _notify_notice(notice, settings, dry_run=False, force=False, feedback_base_url=None)
    assert result["ok"] is True and result["status"] == "sent" and result["transport"] == "webhook"
    assert len(calls) == 1 and "<@tang.userid>" in calls[0][1]


def test_notify_webhook_without_mapping_falls_back_to_name(tmp_path, monkeypatch):
    notice = CiResponsibilityNotice.model_validate(notice_payload([item()]))
    settings = replace(load_settings(), history_enabled=False, wecom_notify_transport="webhook",
                       wecom_webhook_url="https://example.test/secret", wecom_notify_dry_run=False,
                       wecom_user_mapping_file=tmp_path / "missing-users.csv",
                       test_maintainer_mapping_file=None, wecom_fallback_userids=(),
                       wecom_mention_mode="userid")
    calls = []
    monkeypatch.setattr("ci_owner_agent.services.wecom_notice_service.get_history_store", lambda _: None)
    monkeypatch.setattr("ci_owner_agent.services.wecom_notice_service.send_wecom_markdown",
                        lambda url, markdown: calls.append((url, markdown)) or
                        {"ok": True, "statusCode": 200, "response": "ok", "error": None})

    result = _notify_notice(notice, settings, dry_run=False, force=False, feedback_base_url=None)

    assert result["ok"] is True and len(calls) == 1
    assert "@Tang.Tangerine-唐嘉伟" in calls[0][1]
    assert "<@Tang.Tangerine-唐嘉伟>" not in calls[0][1]


def test_webhook_force_failure_does_not_erase_prior_success(monkeypatch):
    store = make_store()
    notice = CiResponsibilityNotice.model_validate(notice_payload([item()]))
    settings = replace(load_settings(), wecom_notify_transport="webhook",
                       wecom_webhook_url="https://example.test/secret", notification_dedup_enabled=True)
    outcomes = [True, False]
    calls = []
    monkeypatch.setattr("ci_owner_agent.services.wecom_notice_service.get_history_store", lambda _: store)
    def send(*args):
        calls.append(args)
        ok = outcomes.pop(0)
        return {"ok": ok, "statusCode": 200, "response": "safe",
                "error": None if ok else "safe failure"}

    monkeypatch.setattr("ci_owner_agent.services.wecom_notice_service.send_wecom_markdown", send)
    first = _notify_notice(notice, settings, dry_run=False, force=False, feedback_base_url=None)
    forced = _notify_notice(notice, settings, dry_run=False, force=True, feedback_base_url=None)
    skipped = _notify_notice(notice, settings, dry_run=False, force=False, feedback_base_url=None)
    doc = store.notifications.docs[0]
    assert first["status"] == "sent" and forced["status"] == "failed" and skipped["status"] == "skipped"
    assert len(calls) == 2 and doc["status"] == "sent"
    assert doc["lastAttemptStatus"] == "failed" and doc["lastAttemptError"] == "safe failure"
    assert store.wecom_notification_outbox.docs == []


def test_webhook_initial_failure_can_retry_and_upgrade_to_sent(monkeypatch):
    store = make_store()
    notice = CiResponsibilityNotice.model_validate(notice_payload([item()]))
    settings = replace(load_settings(), wecom_notify_transport="webhook",
                       wecom_webhook_url="https://example.test/secret", notification_dedup_enabled=True)
    outcomes = [False, True]
    calls = []
    monkeypatch.setattr("ci_owner_agent.services.wecom_notice_service.get_history_store", lambda _: store)

    def send(*args):
        calls.append(args)
        ok = outcomes.pop(0)
        return {"ok": ok, "statusCode": 200, "response": "safe",
                "error": None if ok else "safe failure"}

    monkeypatch.setattr("ci_owner_agent.services.wecom_notice_service.send_wecom_markdown", send)
    failed = _notify_notice(notice, settings, dry_run=False, force=False, feedback_base_url=None)
    sent = _notify_notice(notice, settings, dry_run=False, force=False, feedback_base_url=None)
    skipped = _notify_notice(notice, settings, dry_run=False, force=False, feedback_base_url=None)
    doc = store.notifications.docs[0]
    assert failed["status"] == "failed" and sent["status"] == "sent" and skipped["status"] == "skipped"
    assert len(calls) == 2 and doc["status"] == "sent"
    assert doc["lastAttemptStatus"] == "sent" and doc["lastAttemptError"] is None


def test_notify_dry_run_never_calls_webhook_or_outbox(monkeypatch):
    notice = CiResponsibilityNotice.model_validate(notice_payload([item()]))
    store = make_store()
    monkeypatch.setattr("ci_owner_agent.services.wecom_notice_service.get_history_store", lambda _: store)
    monkeypatch.setattr("ci_owner_agent.services.wecom_notice_service.send_wecom_markdown",
                        lambda *args: (_ for _ in ()).throw(AssertionError("must not send")))
    for transport in ("webhook", "bot"):
        settings = replace(load_settings(), wecom_notify_transport=transport,
                           wecom_webhook_url="https://example.test/secret", wecom_bot_notify_chat_id="chat")
        result = _notify_notice(notice, settings, dry_run=True, force=False, feedback_base_url=None)
        assert result["status"] == "dry_run" and result["transport"] == transport
    assert store.wecom_notification_outbox.docs == [] and store.notifications.docs == []


def test_notify_force_bypasses_dedup(monkeypatch):
    store = make_store()
    notice = CiResponsibilityNotice.model_validate(notice_payload([item()]))
    settings = replace(load_settings(), notification_dedup_enabled=True, wecom_notify_transport="bot",
                       wecom_bot_notify_chat_id="chat")
    monkeypatch.setattr("ci_owner_agent.services.wecom_notice_service.get_history_store", lambda settings: store)

    result = _notify_notice(notice, settings, dry_run=False, force=True, feedback_base_url=None)

    assert result["ok"] is True
    assert result["inserted"] is True
    assert store.wecom_notification_outbox.docs[0]["status"] == "pending"


def test_notify_notice_existing_dead_returns_error_and_force_creates_new_delivery(monkeypatch):
    store = make_store()
    notice = CiResponsibilityNotice.model_validate(notice_payload([item()]))
    settings = replace(load_settings(), wecom_notify_transport="bot", wecom_bot_notify_chat_id="chat")
    monkeypatch.setattr("ci_owner_agent.services.wecom_notice_service.get_history_store", lambda _: store)
    first = _notify_notice(notice, settings, dry_run=False, force=False, feedback_base_url=None)
    store.wecom_notification_outbox.docs[0]["status"] = "dead"
    repeat = _notify_notice(notice, settings, dry_run=False, force=False, feedback_base_url=None)
    forced = _notify_notice(notice, settings, dry_run=False, force=True, feedback_base_url=None)
    assert first["ok"] is True
    assert repeat["ok"] is False and repeat["reason"] == "existing_dead_delivery" and repeat["inserted"] is False
    assert forced["ok"] is True and forced["inserted"] is True and len(store.wecom_notification_outbox.docs) == 2


def test_notify_notice_handles_outbox_exception_safely(monkeypatch, caplog):
    store = make_store()
    notice = CiResponsibilityNotice.model_validate(notice_payload([item()]))
    settings = replace(load_settings(), wecom_notify_transport="bot", wecom_bot_notify_chat_id="chat")
    monkeypatch.setattr("ci_owner_agent.services.wecom_notice_service.get_history_store", lambda _: store)
    monkeypatch.setattr(
        "ci_owner_agent.services.wecom_notification_outbox.WeComNotificationOutbox.enqueue_markdown",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("mongodb://user:password@secret-host/ci_owner_agent")),
    )
    result = _notify_notice(notice, settings, dry_run=False, force=False, feedback_base_url=None)
    rendered = str(result)
    assert result["ok"] is False and result["status"] == "enqueue_failed"
    assert result["error"] == "notification outbox is unavailable"
    assert all(value not in rendered and value not in caplog.text for value in ("mongodb://", "password", "secret-host"))


def test_analyze_notify_exception_stays_json(monkeypatch, capsys):
    notice = CiResponsibilityNotice.model_validate(notice_payload([item()]))
    monkeypatch.setenv("CI_AGENT_MODEL_PROVIDER", "fake")
    monkeypatch.setattr("ci_owner_agent.cli.commands.analyze_local", lambda **kwargs: notice)

    def raise_notify(*args, **kwargs):
        raise RuntimeError("mongodb://user:password@secret-host/db")

    monkeypatch.setattr("ci_owner_agent.services.wecom_notice_service.notify_notice", raise_notify)
    rc = main(
        [
            "analyze-local",
            "--repo",
            "repo",
            "--job",
            "services/fx-code-unittest",
            "--build",
            "5099",
            "--base-commit",
            "b",
            "--head-commit",
            "h",
            "--console-file",
            "console.log",
            "--build-url",
            "local://job/5099",
            "--notify",
        ]
    )
    captured = capsys.readouterr()
    parsed = json.loads(captured.out)
    assert rc == 0
    assert parsed["buildNumber"] == 5099
    assert "WARNING" not in captured.out
    assert "CI 单测失败" not in captured.out
    assert "WARNING: notify failed unexpectedly" in captured.err
    assert all(value not in captured.out and value not in captured.err for value in ("mongodb://", "password", "secret-host"))

def test_fallback_userids_appended_when_no_owner():
    no_owner = item("无高可信责任人", "no_high_confidence_owner", "no_high_confidence_owner", "证据不足。")
    notice = CiResponsibilityNotice.model_validate(notice_payload([no_owner]))
    markdown = format_wecom_markdown_notice(notice, fallback_userids=("ci.owner", "team.leader"))
    assert "**责任人**：无高可信责任人" in markdown
    assert "**待确认维护人**：<@ci.owner>、<@team.leader>" in markdown
    # 责任项中的 owner 仍保持 no-owner，不被兜底通知对象替代。
    item_lines = [l for l in markdown.split("\n") if l.startswith("👤 **责任人**")]
    assert len(item_lines) >= 1
    assert "无高可信责任人" in item_lines[0]
    assert "ci.owner" not in item_lines[0]


def test_fallback_not_appended_when_has_real_owner():
    real = item("Tang", "inherited_failure_owner", "inherited_failure_owner", "历史持续失败。")
    notice = CiResponsibilityNotice.model_validate(notice_payload([real]))
    markdown = format_wecom_markdown_notice(notice, fallback_userids=("ci.owner",))
    # Top line shows @Tang (real owner), not fallback
    assert "**责任人**：" in markdown
    assert "@Tang" in markdown
    # No fallback text or userid
    assert "兜底通知" not in markdown
    assert "<@ci.owner>" not in markdown


def test_mixed_real_and_no_owner_keeps_real_owner_and_routes_no_owner():
    real = item("Tang", "inherited_failure_owner", "inherited_failure_owner", "历史持续失败。")
    no_owner = item("无高可信责任人", "no_high_confidence_owner", "no_high_confidence_owner", "证据不足。")
    notice = CiResponsibilityNotice.model_validate(notice_payload([real, no_owner]))
    markdown = format_wecom_markdown_notice(notice, fallback_userids=("user001",))
    assert "**责任人**：@Tang" in markdown
    assert "**待确认维护人**：<@user001>" in markdown
    assert "👤 **责任人**：无高可信责任人" in markdown


def test_item_owner_still_shows_no_owner_with_fallback():
    no_owner = item("无高可信责任人", "no_high_confidence_owner", "no_high_confidence_owner", "证据不足。")
    notice = CiResponsibilityNotice.model_validate(notice_payload([no_owner]))
    markdown = format_wecom_markdown_notice(notice, fallback_userids=("user001",))
    assert "**待确认维护人**：<@user001>" in markdown
    # Item owner line still shows no high confidence owner (not fallback userid).
    item_owner_lines = [line for line in markdown.split("\n") if line.startswith("👤 **责任人**")]
    assert len(item_owner_lines) >= 1
    assert "无高可信责任人" in item_owner_lines[0]
    assert "user001" not in item_owner_lines[0]


def test_no_fallback_when_not_configured():
    no_owner = item("无高可信责任人", "no_high_confidence_owner", "no_high_confidence_owner", "证据不足。")
    notice = CiResponsibilityNotice.model_validate(notice_payload([no_owner]))
    markdown = format_wecom_markdown_notice(notice)
    assert "无高可信责任人" in markdown
    assert "兜底通知" not in markdown
    assert "<@" not in markdown


def test_legacy_notice_without_items_still_routes_fallback():
    notice = CiResponsibilityNotice.model_validate(notice_payload([]))
    markdown = format_wecom_markdown_notice(notice, fallback_userids=("legacy.fallback",))
    assert "**责任人**：无高可信责任人" in markdown
    assert "**待确认维护人**：<@legacy.fallback>" in markdown


@pytest.mark.parametrize("result", ["SUCCESS", "ABORTED"])
def test_success_and_aborted_legacy_notice_do_not_route_fallback_or_change_digest(result):
    payload = notice_payload([])
    payload["result"] = result
    notice = CiResponsibilityNotice.model_validate(payload)

    markdown = format_wecom_markdown_notice(notice, fallback_userids=("fallback",))
    without_fallback = notification_digest(notice, fallback_userids=())
    with_fallback = notification_digest(notice, fallback_userids=("fallback",))

    assert "待确认维护人" not in markdown
    assert "<@fallback>" not in markdown
    assert without_fallback == with_fallback


@pytest.mark.parametrize("result", ["FAILURE", "UNSTABLE", "UNKNOWN"])
def test_failed_legacy_notice_routes_fallback(result):
    payload = notice_payload([])
    payload["result"] = result
    notice = CiResponsibilityNotice.model_validate(payload)
    markdown = format_wecom_markdown_notice(notice, fallback_userids=("fallback",))
    assert "**待确认维护人**：<@fallback>" in markdown


def test_single_fallback_userid():
    no_owner = item("无高可信责任人", "no_high_confidence_owner", "no_high_confidence_owner", "证据不足。")
    notice = CiResponsibilityNotice.model_validate(notice_payload([no_owner]))
    markdown = format_wecom_markdown_notice(notice, fallback_userids=("onlyone",))
    assert "**待确认维护人**：<@onlyone>" in markdown


def test_notify_notice_passes_fallback_from_settings(monkeypatch):
    from ci_owner_agent.services.wecom_notice_service import notify_notice as _notify_notice
    from ci_owner_agent.config import Settings, load_settings
    from dataclasses import replace

    no_owner = item("无高可信责任人", "no_high_confidence_owner", "no_high_confidence_owner", "证据不足。")
    notice = CiResponsibilityNotice.model_validate(notice_payload([no_owner]))
    settings = replace(load_settings(), wecom_fallback_userids=("fb1", "fb2"),
                       wecom_notify_transport="bot", wecom_bot_notify_chat_id="chat")

    store = make_store()
    monkeypatch.setattr("ci_owner_agent.services.wecom_notice_service.get_history_store", lambda s: store)
    monkeypatch.setattr("ci_owner_agent.services.wecom_notice_service.build_wecom_notice_mapper", lambda s, store: None)

    _notify_notice(notice, settings, dry_run=False, force=False, feedback_base_url=None)

    assert "**待确认维护人**：<@fb1>、<@fb2>" in store.wecom_notification_outbox.docs[0]["payload"]["content"]


def test_notify_notice_dry_run_with_fallback(monkeypatch):
    from ci_owner_agent.services.wecom_notice_service import notify_notice as _notify_notice
    from ci_owner_agent.config import load_settings
    from dataclasses import replace

    no_owner = item("无高可信责任人", "no_high_confidence_owner", "no_high_confidence_owner", "证据不足。")
    notice = CiResponsibilityNotice.model_validate(notice_payload([no_owner]))
    settings = replace(
        load_settings(),
        notification_dedup_enabled=False,
        wecom_user_mapping_file=None,
        wecom_fallback_userids=("ci.owner",),
    )

    monkeypatch.setattr("ci_owner_agent.services.wecom_notice_service.get_history_store", lambda s: make_store())
    monkeypatch.setattr("ci_owner_agent.services.wecom_notice_service.build_wecom_notice_mapper", lambda s, store: None)

    result = _notify_notice(notice, settings, dry_run=True, force=False, feedback_base_url=None)

    assert result["status"] == "dry_run"
    assert "**责任人**：无高可信责任人" in result["markdown"]
    assert "**待确认维护人**：<@ci.owner>" in result["markdown"]

def test_no_owner_notice_mentions_fallback_userids_at_top_and_item_route():
    no_owner = item("无高可信责任人", "no_high_confidence_owner", "no_high_confidence_owner", "证据不足。")
    notice = CiResponsibilityNotice.model_validate(notice_payload([no_owner]))

    markdown = format_wecom_markdown_notice(notice, fallback_userids=("ci.owner", "team.leader"))

    assert " **责任人**：无高可信责任人" in markdown
    assert " **待确认维护人**：<@ci.owner>、<@team.leader>" in markdown
    assert "👤 **责任人**：无高可信责任人" in markdown
    assert "📣 **待确认维护人**：<@ci.owner>、<@team.leader>" in markdown


def test_fallback_userids_are_not_used_when_real_owner_exists():
    notice = CiResponsibilityNotice.model_validate(notice_payload([item("Tang")]))
    markdown = format_wecom_markdown_notice(notice, fallback_userids=("ci.owner",))

    assert " **责任人**：@Tang" in markdown
    assert "兜底通知" not in markdown
    assert "<@ci.owner>" not in markdown


def test_fallback_userids_are_cleaned_by_formatter():
    no_owner = item("无高可信责任人", "no_high_confidence_owner", "no_high_confidence_owner", "证据不足。")
    notice = CiResponsibilityNotice.model_validate(notice_payload([no_owner]))

    markdown = format_wecom_markdown_notice(notice, fallback_userids=(" ci.owner ", "", "ci.owner", "team.leader"))

    assert " **责任人**：无高可信责任人" in markdown
    assert " **待确认维护人**：<@ci.owner>、<@team.leader>" in markdown
    assert "<@>" not in markdown


def test_notify_notice_passes_fallback_userids_from_settings(monkeypatch):
    from ci_owner_agent.services.wecom_notice_service import notify_notice as _notify_notice
    from ci_owner_agent.config import load_settings
    from dataclasses import replace

    store = make_store()
    no_owner = item("无高可信责任人", "no_high_confidence_owner", "no_high_confidence_owner", "证据不足。")
    notice = CiResponsibilityNotice.model_validate(notice_payload([no_owner]))
    settings = replace(
        load_settings(),
        notification_dedup_enabled=False,
        wecom_user_mapping_file=None,
        wecom_fallback_userids=("ci.owner",),
    )
    monkeypatch.setattr("ci_owner_agent.services.wecom_notice_service.get_history_store", lambda settings: store)

    result = _notify_notice(notice, settings, dry_run=True, force=False, feedback_base_url=None)

    assert "**责任人**：无高可信责任人" in result["markdown"]
    assert "**待确认维护人**：<@ci.owner>" in result["markdown"]
    assert "👤 **责任人**：无高可信责任人" in result["markdown"]


def _maintainer_resolver(tmp_path, maintainers=None, pattern="test/service/view/**"):
    maintainers = maintainers or [("Charlie", "charlie.guo")]
    rows = "\n".join(
        f"      - name: \"{name}\"\n        wecomUserId: \"{userid}\"" for name, userid in maintainers
    )
    path = tmp_path / "maintainers.yml"
    path.write_text(
        "version: 1\nrules:\n"
        "  - repo: fx-code\n"
        "    job: services/fx-code-unittest\n"
        f"    paths: [\"{pattern}\"]\n"
        "    maintainers:\n"
        f"{rows}\n",
        encoding="utf-8",
    )
    return TestMaintainerResolver.from_yaml(path)


def test_no_owner_path_routes_single_maintainer_without_changing_owner(tmp_path):
    no_owner = item(
        "无高可信责任人",
        "no_high_confidence_owner",
        "no_high_confidence_owner",
        "证据不足。",
        test_file_path="test/service/view/ViewDataQueryServiceTest.ts",
    )
    notice = CiResponsibilityNotice.model_validate(notice_payload([no_owner]))
    markdown = format_wecom_markdown_notice(
        notice,
        maintainer_resolver=_maintainer_resolver(tmp_path),
        repo="fx-code",
    )

    assert "**责任人**：无高可信责任人" in markdown
    assert "**待确认维护人**：<@charlie.guo>" in markdown
    assert "📁 测试文件：test/service/view/ViewDataQueryServiceTest.ts" in markdown
    assert notice.owner.type == "no_high_confidence_owner"
    assert notice.responsibilityItems[0].owner.name == "无高可信责任人"


def test_coverage_failure_file_path_routes_maintainer_without_becoming_owner(tmp_path):
    no_owner = item(
        "无高可信责任人",
        "no_high_confidence_owner",
        "no_high_confidence_owner",
        "覆盖率文件没有可确认的因果责任人。",
        test_file_path=None,
        failure_file_path="packages/fidget-sql/src/parser/TokenScanner.ts",
    )
    notice = CiResponsibilityNotice.model_validate(notice_payload([no_owner]))
    resolver = _maintainer_resolver(
        tmp_path,
        [("Dust", "dust")],
        pattern="packages/fidget-sql/src/**",
    )

    markdown = format_wecom_markdown_notice(notice, maintainer_resolver=resolver, repo="fx-code")

    assert "**待确认维护人**：<@dust>" in markdown
    assert notice.owner.type == "no_high_confidence_owner"
    assert notice.responsibilityItems[0].failureFilePath == "packages/fidget-sql/src/parser/TokenScanner.ts"


def test_no_owner_path_routes_multiple_maintainers_in_order(tmp_path):
    no_owner = item(
        "无高可信责任人",
        "no_high_confidence_owner",
        "no_high_confidence_owner",
        "证据不足。",
        test_file_path="test/service/view/ViewDataQueryServiceTest.ts",
    )
    notice = CiResponsibilityNotice.model_validate(notice_payload([no_owner]))
    resolver = _maintainer_resolver(tmp_path, [("Charlie", "charlie.guo"), ("Henry", "henry")])
    markdown = format_wecom_markdown_notice(notice, maintainer_resolver=resolver, repo="fx-code")

    assert "**待确认维护人**：<@charlie.guo>、<@henry>" in markdown
    assert "📣 **待确认维护人**：<@charlie.guo>、<@henry>" in markdown


def test_unmatched_and_missing_test_path_route_fallback(tmp_path):
    unmatched = item(
        "无高可信责任人",
        "no_high_confidence_owner",
        "no_high_confidence_owner",
        "证据不足。",
        test_file_path="test/service/unknown/UnknownTest.ts",
    )
    missing = item("无高可信责任人", "no_high_confidence_owner", "no_high_confidence_owner", "证据不足。")
    resolver = _maintainer_resolver(tmp_path)

    unmatched_markdown = format_wecom_markdown_notice(
        CiResponsibilityNotice.model_validate(notice_payload([unmatched])),
        maintainer_resolver=resolver,
        repo="fx-code",
        fallback_userids=("default.userid",),
    )
    missing_markdown = format_wecom_markdown_notice(
        CiResponsibilityNotice.model_validate(notice_payload([missing])),
        maintainer_resolver=resolver,
        repo="fx-code",
        fallback_userids=("default.userid",),
    )

    assert "📁 测试文件：test/service/unknown/UnknownTest.ts" in unmatched_markdown
    assert "未匹配测试文件维护规则" in unmatched_markdown
    assert "📁 测试文件：未识别" in missing_markdown
    assert "未识别失败测试文件" in missing_markdown
    assert "<@default.userid>" in unmatched_markdown
    assert "<@default.userid>" in missing_markdown

    details = unmatched_markdown.split("**👥 责任明细**", maxsplit=1)[1]
    evidence_pos = details.index("• 依据：")
    path_pos = details.index("• 📁 测试文件：")
    maintainer_pos = details.index("📣 **待确认维护人**：")
    reason_pos = details.index("• ℹ️ 路由说明：")
    assert evidence_pos < path_pos < maintainer_pos < reason_pos


def test_multiple_no_owner_routes_are_deduplicated_and_mixed_owner_is_preserved(tmp_path):
    real = item("Tang", "high_confidence", "current_build_owner", "当前引入。", "tang@example.com")
    first = item("无高可信责任人", "no_high_confidence_owner", "no_high_confidence_owner", "待确认。", test_file_path="test/service/view/A.test.ts")
    second = item("无高可信责任人", "no_high_confidence_owner", "no_high_confidence_owner", "待确认。", test_file_path="test/service/view/B.test.ts")
    notice = CiResponsibilityNotice.model_validate(notice_payload([real, first, second]))
    resolver = _maintainer_resolver(tmp_path, [("Charlie", "charlie.guo"), ("Henry", "henry")])

    markdown = format_wecom_markdown_notice(notice, maintainer_resolver=resolver, repo="fx-code")

    assert "**责任人**：@Tang" in markdown
    details = markdown.split("**👥 责任明细**", maxsplit=1)[1]
    assert details.count("📣 **待确认维护人**：<@charlie.guo>、<@henry>") == 2
    assert notice.responsibilityItems[1].owner.type == "no_high_confidence_owner"


def test_multiple_no_owner_items_route_to_different_maintainers(tmp_path):
    path = tmp_path / "maintainers.yml"
    path.write_text(
        "version: 1\nrules:\n"
        "  - paths: [\"test/view/**\"]\n"
        "    maintainers: [{name: Charlie, wecomUserId: charlie}]\n"
        "  - paths: [\"test/quota/**\"]\n"
        "    maintainers: [{name: Mars, wecomUserId: mars}]\n",
        encoding="utf-8",
    )
    view = item("无高可信责任人", "no_high_confidence_owner", "no_high_confidence_owner", "待确认。", test_file_path="test/view/ViewTest.ts")
    quota = item("无高可信责任人", "no_high_confidence_owner", "no_high_confidence_owner", "待确认。", test_file_path="test/quota/QuotaTest.ts")
    notice = CiResponsibilityNotice.model_validate(notice_payload([view, quota]))

    markdown = format_wecom_markdown_notice(
        notice,
        maintainer_resolver=TestMaintainerResolver.from_yaml(path),
    )

    assert "📣 **待确认维护人**：<@charlie>" in markdown
    assert "📣 **待确认维护人**：<@mars>" in markdown


def test_maintainer_name_mode_and_no_no_owner_item(tmp_path):
    no_owner = item("无高可信责任人", "no_high_confidence_owner", "no_high_confidence_owner", "待确认。", test_file_path="test/service/view/A.test.ts")
    resolver = _maintainer_resolver(tmp_path, [("Charlie", "charlie.guo")])
    name_markdown = format_wecom_markdown_notice(
        CiResponsibilityNotice.model_validate(notice_payload([no_owner])),
        maintainer_resolver=resolver,
        repo="fx-code",
        mention_mode="name",
    )
    real_markdown = format_wecom_markdown_notice(
        CiResponsibilityNotice.model_validate(notice_payload([item("Tang")])),
        maintainer_resolver=resolver,
        repo="fx-code",
    )

    assert "**待确认维护人**：@Charlie" in name_markdown
    assert "待确认维护人" not in real_markdown


def test_notification_digest_changes_when_maintainer_route_changes(tmp_path):
    no_owner = item("无高可信责任人", "no_high_confidence_owner", "no_high_confidence_owner", "待确认。", test_file_path="test/service/view/A.test.ts")
    notice = CiResponsibilityNotice.model_validate(notice_payload([no_owner]))
    first = _maintainer_resolver(tmp_path, [("Charlie", "charlie.guo")])
    first_digest = notification_digest(notice, maintainer_resolver=first, repo="fx-code")
    second = _maintainer_resolver(tmp_path, [("Henry", "henry")])
    second_digest = notification_digest(notice, maintainer_resolver=second, repo="fx-code")

    assert first_digest != second_digest
    assert first_digest == notification_digest(notice, maintainer_resolver=first, repo="fx-code")


def test_notify_notice_loads_maintainer_mapping_from_settings(tmp_path, monkeypatch):
    resolver = _maintainer_resolver(tmp_path, [("Charlie", "charlie.guo")])
    mapping_file = tmp_path / "maintainers.yml"
    no_owner = item(
        "无高可信责任人",
        "no_high_confidence_owner",
        "no_high_confidence_owner",
        "待确认。",
        test_file_path="test/service/view/A.test.ts",
    )
    payload = notice_payload([no_owner])
    payload["repo"] = "fx-code"
    notice = CiResponsibilityNotice.model_validate(payload)
    settings = replace(
        load_settings(),
        notification_dedup_enabled=False,
        test_maintainer_mapping_file=mapping_file,
        wecom_fallback_userids=("fallback",),
    )
    monkeypatch.setattr("ci_owner_agent.services.wecom_notice_service.get_history_store", lambda settings: None)

    result = _notify_notice(notice, settings, dry_run=True, force=False, feedback_base_url=None)

    assert resolver.warnings == ()
    assert "**待确认维护人**：<@charlie.guo>" in result["markdown"]
    assert "<@fallback>" not in result["markdown"]


def test_notify_notice_invalid_mapping_warns_and_uses_fallback(tmp_path, monkeypatch, capsys):
    mapping_file = tmp_path / "invalid.yml"
    mapping_file.write_text("rules: [", encoding="utf-8")
    no_owner = item("无高可信责任人", "no_high_confidence_owner", "no_high_confidence_owner", "待确认。")
    notice = CiResponsibilityNotice.model_validate(notice_payload([no_owner]))
    settings = replace(
        load_settings(),
        notification_dedup_enabled=False,
        test_maintainer_mapping_file=mapping_file,
        wecom_fallback_userids=("fallback",),
    )
    monkeypatch.setattr("ci_owner_agent.services.wecom_notice_service.get_history_store", lambda settings: None)

    result = _notify_notice(notice, settings, dry_run=True, force=False, feedback_base_url=None)
    captured = capsys.readouterr()

    assert "invalid test maintainer mapping yaml" in captured.err
    assert "**待确认维护人**：<@fallback>" in result["markdown"]


def test_many_items_render_within_4096_utf8_bytes():
    """数百个责任项渲染后仍 ≤4096 UTF-8 字节，且显式标注省略数量。"""
    items = [item(reason=f"历史持续失败，责任项 {i} 的较长中文描述以撑大消息体。") for i in range(300)]
    notice = CiResponsibilityNotice.model_validate(notice_payload(items))
    markdown = format_wecom_markdown_notice(notice, feedback_base_url="http://ci-agent.test/feedback")
    n = len(markdown.encode("utf-8"))
    assert n <= 4096, f"渲染结果 {n} 字节超预算"
    assert "请查看分析结果" in markdown
    assert "Jenkins" in markdown
    assert "反馈" in markdown


def test_many_connection_items_aggregated_render_within_budget():
    """聚合后的环境 no-owner 责任项（每 suite 一条）渲染 ≤4096。"""
    items = [
        item(owner_type="no_high_confidence_owner", owner_name="无高可信责任人", responsibility_type="no_high_confidence_owner", reason="环境失败")
        for _ in range(50)
    ]
    notice = CiResponsibilityNotice.model_validate(notice_payload(items))
    markdown = format_wecom_markdown_notice(notice)
    assert len(markdown.encode("utf-8")) <= 4096


def test_long_job_and_success_always_within_4096_bytes():
    """超长 job 的两种消息都必须守住预算，同时保留 Jenkins 链接和省略提示。"""
    payload = notice_payload([item()])
    payload["job"] = "services/fx-code-unittest/" * 300
    failure = CiResponsibilityNotice.model_validate(payload)
    md_failure = format_wecom_markdown_notice(failure)
    assert len(md_failure.encode("utf-8")) <= 4096
    assert "查看 Jenkins 构建" in md_failure
    assert "Job 名称过长已截断" in md_failure
    assert "**责任人**" in md_failure

    success = CiResponsibilityNotice.model_validate({**payload, "result": "SUCCESS"})
    md_success = format_wecom_markdown_notice(success)
    assert len(md_success.encode("utf-8")) <= 4096
    assert "查看 Jenkins 构建" in md_success
    assert "Job 名称过长已截断" in md_success


def test_omission_note_preserved_when_items_over_budget():
    """省略提示必须保留，即使责任项极多。"""
    items = [item(reason=f"历史持续失败，责任项 {i} 超长中文描述" + "很长" * 20) for i in range(500)]
    notice = CiResponsibilityNotice.model_validate(notice_payload(items))
    md = format_wecom_markdown_notice(notice)
    assert len(md.encode("utf-8")) <= 4096
    assert "请查看分析结果" in md
    assert "Jenkins" in md
    assert "本消息展示" in md
    assert "其余" in md
