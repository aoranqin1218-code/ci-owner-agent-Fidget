from pathlib import Path

from scripts.manual_analyze import (
    DEFAULT_JOB,
    DEFAULT_REPO,
    build_ci_argv,
    build_parser,
)


def _parse(*args: str):
    return build_parser().parse_args(list(args))


def test_jenkins_shortcut_uses_fidget_defaults_without_notification():
    command = build_ci_argv(_parse("jenkins", "31"))

    assert command[:7] == [
        "analyze",
        "--job",
        DEFAULT_JOB,
        "--build",
        "31",
        "--repo",
        DEFAULT_REPO,
    ]
    assert command[command.index("--output-file") + 1] == str(
        Path("runs/manual/jenkins-build-31.notice.json")
    )
    assert "--notify" not in command
    assert "--notify-dry-run" not in command
    assert "--force-notify" not in command


def test_jenkins_notify_shortcut_only_adds_explicit_notification_flags():
    command = build_ci_argv(
        _parse("jenkins", "31", "--notify", "--force-notify", "--log-tail-lines", "12000")
    )

    assert "--notify" in command
    assert "--force-notify" in command
    assert command[command.index("--log-tail-lines") + 1] == "12000"


def test_local_shortcut_keeps_trusted_commit_window_explicit():
    command = build_ci_argv(
        _parse("local", "samples/fidget.log", "31", "base123", "head456", "--result", "FAILURE")
    )

    assert command[0] == "analyze-local"
    assert command[command.index("--base-commit") + 1] == "base123"
    assert command[command.index("--head-commit") + 1] == "head456"
    assert command[command.index("--console-file") + 1] == "samples/fidget.log"
    assert command[command.index("--build-url") + 1] == f"local://{DEFAULT_JOB}/31"
    assert command[command.index("--output-file") + 1] == str(
        Path("runs/manual/local-build-31.notice.json")
    )
    assert "--notify" not in command


def test_preview_shortcut_is_always_dry_run():
    command = build_ci_argv(_parse("preview", "runs/manual/jenkins-build-31.notice.json"))

    assert command == [
        "notify-notice",
        "--notice-file",
        "runs/manual/jenkins-build-31.notice.json",
        "--dry-run",
    ]
