"""Stable CLI entrypoint and argument parser."""
from __future__ import annotations

import argparse
import sys

from ci_owner_agent.cli.commands import dispatch_command
from ci_owner_agent.config import load_settings

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ci-owner-agent")
    subparsers = parser.add_subparsers(dest="command", required=True)

    analyze = subparsers.add_parser("analyze", help="Analyze a Jenkins build")
    analyze.add_argument("--job", required=True)
    analyze.add_argument("--build", type=int, required=True)
    analyze.add_argument("--repo", required=True)
    analyze.add_argument("--log-tail-lines", type=int, default=None)
    analyze.add_argument("--notify", action="store_true")
    analyze.add_argument("--notify-dry-run", action="store_true")
    analyze.add_argument("--force-notify", action="store_true")
    analyze.add_argument("--output-file", default=None)

    local = subparsers.add_parser("analyze-local", help="Analyze a local console log and local Git cache")
    local.add_argument("--repo", required=True)
    local.add_argument("--job", required=True)
    local.add_argument("--build", type=int, required=True)
    local.add_argument("--branch", default=None)
    local.add_argument("--base-commit", required=True)
    local.add_argument("--head-commit", required=True)
    local.add_argument("--console-file", required=True)
    local.add_argument("--build-url", required=True)
    local.add_argument("--log-tail-lines", type=int, default=None)
    local.add_argument("--result", choices=["SUCCESS", "FAILURE", "UNSTABLE", "ABORTED", "UNKNOWN"], default=None)
    local.add_argument("--ignore-checkout-commit-mismatch", action="store_true")
    local.add_argument("--output-file", default=None)
    local.add_argument("--last-success-build", type=int, default=None)
    local.add_argument("--previous-build", type=int, default=None)
    local.add_argument("--previous-commit", default=None)
    local.add_argument("--notify", action="store_true")
    local.add_argument("--notify-dry-run", action="store_true")
    local.add_argument("--force-notify", action="store_true")
    local.add_argument("--build-timestamp", default=None, help="Build time as timezone-aware ISO 8601")

    stats = subparsers.add_parser("test-failure-stats", help="Query deterministic test-file failure statistics")
    _add_weekly_scope_arguments(stats)
    stats.add_argument("--format", choices=["json", "csv", "markdown"], default="json")

    weekly = subparsers.add_parser("weekly-test-report", help="Generate or send the weekly test failure report")
    _add_weekly_scope_arguments(weekly)
    weekly.add_argument("--notify", action="store_true")
    weekly.add_argument("--dry-run", action="store_true")
    weekly.add_argument("--force", action="store_true")

    notify = subparsers.add_parser("notify-notice", help="Send or preview a notice to WeCom (default) or Feishu")
    notify.add_argument("--notice-file", required=True)
    notify.add_argument("--channel", choices=["wecom", "feishu"], default="wecom", help="Notification channel (default: wecom)")
    notify.add_argument("--dry-run", action="store_true")
    notify.add_argument("--force", action="store_true")
    notify.add_argument("--feedback-base-url", default=None)

    serve = subparsers.add_parser("serve-feedback", help="Start the feedback web server")
    serve.add_argument("--host", default=None)
    serve.add_argument("--port", type=int, default=None)
    serve.add_argument("--reload", action="store_true")

    wecom_bot = subparsers.add_parser("serve-wecom-bot", help="Start the WeCom AI bot long-running worker")
    wecom_bot.add_argument("--bot-id", default=None)
    wecom_bot.add_argument("--secret", default=None)

    feedback = subparsers.add_parser("feedback", help="Manage manual feedback")
    feedback_sub = feedback.add_subparsers(dest="feedback_command", required=True)
    apply = feedback_sub.add_parser("apply")
    apply.add_argument("--repo", required=True)
    apply.add_argument("--job", required=True)
    apply.add_argument("--branch", required=True)
    apply.add_argument("--build", type=int, required=True)
    apply.add_argument("--failure-id", default=None)
    apply.add_argument("--failure-signature", default=None)
    apply.add_argument("--action", required=True)
    apply.add_argument("--owner-name", default=None)
    apply.add_argument("--owner-email", default=None)
    apply.add_argument("--owner-type", choices=["high_confidence", "medium_confidence"], default="high_confidence")
    apply.add_argument("--owner-commit", default=None)
    apply.add_argument("--source-build-number", type=int, default=None)
    apply.add_argument("--reviewer", default=None)
    apply.add_argument("--note", default=None)
    list_cmd = feedback_sub.add_parser("list")
    list_cmd.add_argument("--repo", required=True)
    list_cmd.add_argument("--job", required=True)
    list_cmd.add_argument("--branch", required=True)
    list_cmd.add_argument("--build", type=int, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        settings = load_settings()
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0)
    return dispatch_command(args, settings, parser)


def _add_weekly_scope_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo", required=True)
    parser.add_argument("--job", action="append", default=[])
    parser.add_argument("--branch", action="append", default=[])
    parser.add_argument("--period", choices=["current-week", "previous-week"], default="previous-week")
    parser.add_argument("--period-start", default=None)
    parser.add_argument("--period-end", default=None)
    parser.add_argument("--top", type=int, default=None)
    parser.add_argument("--config-file", default=None)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
