from __future__ import annotations

from ci_owner_agent.schemas import CiResponsibilityNotice
from ci_owner_agent.services.notification_formatter import format_wecom_markdown_notice


def test_success_notification_uses_the_same_overview_hierarchy_as_feishu():
    notice = CiResponsibilityNotice.model_validate(
        {
            "repo": "fx-code",
            "job": "services/fx-code-unittest",
            "buildNumber": 5205,
            "buildUrl": "https://jenkins.example/job/services/job/fx-code-unittest/5205/",
            "result": "SUCCESS",
            "branch": "dev",
            "headCommit": "head",
            "baseCommit": "base",
            "owner": {
                "type": "no_high_confidence_owner",
                "name": "无高可信责任人",
                "email": None,
                "commit": None,
                "confidence": 0,
            },
            "failureReason": "构建成功，无需定责。",
            "evidence": [
                {
                    "id": "E1",
                    "type": "build_info",
                    "summary": "构建结果为 SUCCESS",
                    "detail": "build 5205 succeeded",
                    "source": "jenkins",
                }
            ],
            "suggestions": [],
            "responsibilityItems": [],
            "hasHighConfidenceOwner": False,
        }
    )

    markdown = format_wecom_markdown_notice(
        notice,
        feedback_base_url="https://ci-agent.example/feedback",
        feedback_token="token",
        feedback_code="CI-5205",
    )

    assert markdown == (
        "### ✅ CI 构建成功 | services/fx-code-unittest #5205\n\n"
        "**📋 构建概览**\n"
        "• 项目：fx-code\n"
        "• 流水线：services/fx-code-unittest\n"
        "• 分支：dev\n\n"
        "🏗️ [查看 Jenkins 构建](https://jenkins.example/job/services/job/fx-code-unittest/5205/)"
    )
    assert "责任人" not in markdown
    assert "原因" not in markdown
    assert "责任项" not in markdown
    assert "提交反馈" not in markdown
    assert "反馈码" not in markdown
