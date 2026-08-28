from scripts.send_weekly_failure_report import build_weekly_report_command


def test_weekly_report_command_sends_previous_week():
    command = build_weekly_report_command(
        python_executable="python",
        repo="fx-code",
        jobs=["services/fx-code-unittest"],
        branches=["dev"],
        config_file="config/weekly-test-report.yml",
        top=20,
        dry_run=False,
        force=False,
    )
    assert command[:4] == ["python", "-m", "ci_owner_agent", "weekly-test-report"]
    assert command[command.index("--period") + 1] == "previous-week"
    assert command[command.index("--job") + 1] == "services/fx-code-unittest"
    assert command[command.index("--branch") + 1] == "dev"
    assert "--notify" in command
    assert "--dry-run" not in command


def test_weekly_report_command_dry_run_does_not_send():
    command = build_weekly_report_command(
        python_executable="python",
        repo="fx-code",
        jobs=[],
        branches=[],
        config_file=None,
        top=None,
        dry_run=True,
        force=True,
    )
    assert "--dry-run" in command
    assert "--notify" not in command
    assert "--force" in command
