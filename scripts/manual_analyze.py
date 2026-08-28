"""Short, project-specific commands for manual Fidget analysis.

This module only translates convenient positional arguments into the stable
``python -m ci_owner_agent`` CLI contract. Analysis, persistence and
notification behavior remain in the normal application entrypoint.
"""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Sequence

from ci_owner_agent.main import main as ci_owner_agent_main


DEFAULT_REPO = "fidget-xiaoqin"
DEFAULT_JOB = "npm/fxp-fidget/fidget-xiaoqin-pipeline"
DEFAULT_BRANCH = "main"
DEFAULT_OUTPUT_DIR = Path("runs/manual")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fidget 手工测试快捷入口；底层仍调用 python -m ci_owner_agent。"
    )
    subparsers = parser.add_subparsers(dest="shortcut", required=True)

    jenkins = subparsers.add_parser("jenkins", help="分析真实 Jenkins 构建")
    jenkins.add_argument("build", type=int, help="Jenkins 构建号")
    jenkins.add_argument("--job", default=DEFAULT_JOB)
    jenkins.add_argument("--repo", default=DEFAULT_REPO)
    jenkins.add_argument("--log-tail-lines", type=int, default=None)
    jenkins.add_argument("--output-file", default=None)
    jenkins.add_argument("--notify", action="store_true", help="真实触发当前配置的通知链路")
    jenkins.add_argument("--notify-dry-run", action="store_true", help="只执行通知格式化，不发送")
    jenkins.add_argument("--force-notify", action="store_true", help="忽略通知去重；仅限明确需要时")

    local = subparsers.add_parser("local", help="分析本地 Jenkins console 日志")
    local.add_argument("console_file", help="本地 console log 路径")
    local.add_argument("build", type=int, help="该日志对应的构建号")
    local.add_argument("base_commit", help="可信责任窗口起点 commit")
    local.add_argument("head_commit", help="日志中实际 checkout 的 commit")
    local.add_argument("--job", default=DEFAULT_JOB)
    local.add_argument("--repo", default=DEFAULT_REPO)
    local.add_argument("--branch", default=DEFAULT_BRANCH)
    local.add_argument(
        "--result",
        choices=["SUCCESS", "FAILURE", "UNSTABLE", "ABORTED", "UNKNOWN"],
        default=None,
    )
    local.add_argument("--log-tail-lines", type=int, default=None)
    local.add_argument("--output-file", default=None)
    local.add_argument("--notify-dry-run", action="store_true", help="只执行通知格式化，不发送")

    preview = subparsers.add_parser("preview", help="在终端预览已有 notice，不发送")
    preview.add_argument("notice_file", help="notice JSON 路径")

    return parser


def _default_output_file(kind: str, build: int) -> str:
    return str(DEFAULT_OUTPUT_DIR / f"{kind}-build-{build}.notice.json")


def build_ci_argv(args: argparse.Namespace) -> list[str]:
    if args.shortcut == "jenkins":
        command = [
            "analyze",
            "--job",
            args.job,
            "--build",
            str(args.build),
            "--repo",
            args.repo,
            "--output-file",
            args.output_file or _default_output_file("jenkins", args.build),
        ]
        if args.log_tail_lines is not None:
            command += ["--log-tail-lines", str(args.log_tail_lines)]
        if args.notify:
            command.append("--notify")
        if args.notify_dry_run:
            command.append("--notify-dry-run")
        if args.force_notify:
            command.append("--force-notify")
        return command

    if args.shortcut == "local":
        command = [
            "analyze-local",
            "--repo",
            args.repo,
            "--job",
            args.job,
            "--build",
            str(args.build),
            "--branch",
            args.branch,
            "--base-commit",
            args.base_commit,
            "--head-commit",
            args.head_commit,
            "--console-file",
            args.console_file,
            "--build-url",
            f"local://{args.job}/{args.build}",
            "--output-file",
            args.output_file or _default_output_file("local", args.build),
        ]
        if args.result is not None:
            command += ["--result", args.result]
        if args.log_tail_lines is not None:
            command += ["--log-tail-lines", str(args.log_tail_lines)]
        if args.notify_dry_run:
            command.append("--notify-dry-run")
        return command

    if args.shortcut == "preview":
        return ["notify-notice", "--notice-file", args.notice_file, "--dry-run"]

    raise ValueError(f"unsupported shortcut: {args.shortcut}")


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return ci_owner_agent_main(build_ci_argv(args))


if __name__ == "__main__":
    raise SystemExit(main())
