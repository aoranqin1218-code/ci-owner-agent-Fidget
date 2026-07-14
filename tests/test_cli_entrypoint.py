from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def _run_module(*args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["CI_AGENT_MODEL_PROVIDER"] = "fake"
    return subprocess.run(
        [sys.executable, "-m", "ci_owner_agent", *args],
        cwd=os.getcwd(),
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )




def _notice_json_text() -> str:
    return json.dumps(
        {
            "repo": "sample-ts-repo",
            "job": "services/fx-code-unittest",
            "buildNumber": 5099,
            "buildUrl": "https://jenkins.example/job/5099/",
            "result": "FAILURE",
            "branch": "dev",
            "headCommit": "h",
            "baseCommit": "b",
            "owner": {
                "type": "no_high_confidence_owner",
                "name": "无高可信责任人",
                "email": None,
                "commit": None,
                "confidence": 0,
            },
            "failureReason": "本次构建包含多个失败，责任项见下方。",
            "evidence": [],
            "responsibilityItems": [
                {
                    "failureId": "F1",
                    "failureTitle": "接口超时测试失败",
                    "failureSignature": "sig-1",
                    "failureSummary": "构建过程中接口超时",
                    "owner": {
                        "type": "inherited_failure_owner",
                        "name": "张三",
                        "email": "zhangsan@example.com",
                        "commit": "abc123",
                        "confidence": 0.9,
                    },
                    "responsibilityType": "inherited_failure_owner",
                    "sourceBuildNumber": 5094,
                    "sourceBuildUrl": None,
                    "sourceCommit": "def456",
                    "matchType": "signature_exact",
                    "relationship": "very_likely_same_failure",
                    "confidence": 0.9,
                    "reason": "历史持续失败。",
                    "evidenceIds": ["E1"],
                }
            ],
            "suggestions": [],
            "hasHighConfidenceOwner": False,
        },
        ensure_ascii=False,
        indent=2,
    ) + "\n"
def test_module_notify_notice_missing_file_returns_process_code_2(tmp_path):
    missing = tmp_path / "missing.json"

    completed = _run_module("notify-notice", "--notice-file", str(missing), "--dry-run")

    assert completed.returncode == 2
    assert "ERROR: notice file not found" in completed.stderr
    assert "Traceback" not in completed.stderr


def test_module_feedback_apply_bad_owner_type_returns_process_code_2():
    completed = _run_module(
        "feedback",
        "apply",
        "--job",
        "services/fx-code-unittest",
        "--build",
        "5099",
        "--failure-id",
        "failure-xxx",
        "--action",
        "correct_owner",
        "--owner-name",
        "Henry.Zeng-曾纪龙",
        "--owner-type",
        "no_high_confidence_owner",
    )

    assert completed.returncode == 2
    assert "invalid choice" in completed.stderr
    assert "Traceback" not in completed.stderr


def test_notify_notice_accepts_utf8_bom_json(tmp_path):
    bom_file = tmp_path / "notice_bom.json"
    bom_file.write_text(_notice_json_text(), encoding="utf-8-sig")

    completed = _run_module("notify-notice", "--notice-file", str(bom_file), "--dry-run", "--force")

    assert completed.returncode == 0
    assert "Unexpected UTF-8 BOM" not in completed.stderr
    assert "ERROR" not in completed.stderr
    assert "接口超时" in completed.stdout
    assert "Traceback" not in completed.stderr


def test_notify_notice_accepts_plain_utf8_json(tmp_path):
    plain_file = tmp_path / "notice_plain.json"
    plain_file.write_text(_notice_json_text(), encoding="utf-8")

    completed = _run_module("notify-notice", "--notice-file", str(plain_file), "--dry-run", "--force")

    assert completed.returncode == 0
    assert "ERROR" not in completed.stderr
    assert "接口超时" in completed.stdout
    assert "Traceback" not in completed.stderr


def test_analyze_output_file_writes_utf8_without_bom(tmp_path):
    output_file = tmp_path / "subdir" / "notice.json"
    env = os.environ.copy()
    env["CI_AGENT_MODEL_PROVIDER"] = "fake"
    env["JENKINS_URL"] = ""

    completed = subprocess.run(
        [sys.executable, "-m", "ci_owner_agent", "analyze",
         "--job", "services/fx-code-unittest", "--build", "5154", "--repo", "fx-code",
         "--output-file", str(output_file)],
        cwd=os.getcwd(),
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert completed.returncode == 0
    assert output_file.exists()
    raw = output_file.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8")
    data = json.loads(text)
    assert data["result"] == "UNKNOWN"
    assert "JENKINS_URL" in data["failureReason"]
    assert "buildUrl" not in completed.stdout



def test_analyze_local_output_file_writes_utf8_without_bom(tmp_path, monkeypatch):
    from ci_owner_agent.main import main
    from ci_owner_agent.schemas import CiResponsibilityNotice
    from tests.test_notification_formatter import item, notice_payload

    output_file = tmp_path / "subdir" / "notice_local.json"

    # Build a notice with Chinese content
    notice = CiResponsibilityNotice.model_validate(notice_payload([item("张三")]))

    monkeypatch.setattr(
        "ci_owner_agent.main.analyze_local",
        lambda **kwargs: notice,
    )
    monkeypatch.setattr(
        "ci_owner_agent.main.get_history_store",
        lambda settings: None,
    )

    exit_code = main([
        "analyze-local",
        "--repo", "sample-ts-repo",
        "--job", "services/fx-code-unittest",
        "--build", "5099",
        "--base-commit", "abc123",
        "--head-commit", "def456",
        "--console-file", str(tmp_path / "fake.log"),
        "--build-url", "https://jenkins.example/job/5099/",
        "--output-file", str(output_file),
    ])

    assert exit_code == 0
    assert output_file.exists()
    raw = output_file.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8")
    data = json.loads(text)
    assert data["result"] == "FAILURE"
    assert "张三" in text

def test_analyze_output_file_write_failure_returns_code_2(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("block")
    output_file = blocker / "subdir" / "notice.json"

    env = os.environ.copy()
    env["CI_AGENT_MODEL_PROVIDER"] = "fake"
    env["JENKINS_URL"] = ""

    completed = subprocess.run(
        [sys.executable, "-m", "ci_owner_agent", "analyze",
         "--job", "services/fx-code-unittest", "--build", "5154", "--repo", "fx-code",
         "--output-file", str(output_file)],
        cwd=os.getcwd(),
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert completed.returncode == 2
    assert "ERROR" in completed.stderr
    assert "buildUrl" not in completed.stdout
    assert "Traceback" not in completed.stderr
