from __future__ import annotations

import argparse
import json
import sys

from ci_owner_agent.config import load_settings
from ci_owner_agent.orchestrator import analyze_jenkins, analyze_local, failure_without_context
from ci_owner_agent.schemas import BuildInfo
from ci_owner_agent.services.git_client import GitClient
from ci_owner_agent.services.jenkins_client import JenkinsClient


def _print_json(model) -> None:

    text = json.dumps(model.model_dump(), ensure_ascii=False, indent=2)

    try:
        sys.stdout.write(text + "\n")
    except UnicodeEncodeError:
        sys.stdout.buffer.write((text + "\n").encode("utf-8", errors="replace"))
        sys.stdout.buffer.flush()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ci-owner-agent")
    subparsers = parser.add_subparsers(dest="command", required=True)

    analyze = subparsers.add_parser("analyze", help="Analyze a Jenkins build")
    analyze.add_argument("--job", required=True)
    analyze.add_argument("--build", type=int, required=True)
    analyze.add_argument("--repo", required=True)
    analyze.add_argument("--log-tail-lines", type=int, default=None)

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
    return parser


def main(argv: list[str] | None = None) -> int:
    settings = load_settings()
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "analyze-local":
        git_client = GitClient(settings.repo_cache_dir, max_output_chars=settings.max_tool_output_chars)
        try:
            notice = analyze_local(
                repo=args.repo,
                job=args.job,
                build=args.build,
                branch=args.branch,
                base_commit=args.base_commit,
                head_commit=args.head_commit,
                console_file=args.console_file,
                build_url=args.build_url,
                git_client=git_client,
                log_tail_lines=args.log_tail_lines or settings.default_log_tail_lines,
                result=args.result,
                max_output_chars=settings.max_tool_output_chars,
                settings=settings,
                ignore_checkout_commit_mismatch=args.ignore_checkout_commit_mismatch,
            )
        except ValueError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        _print_json(notice)
        return 0
    if args.command == "analyze":
        if not settings.jenkins_url:
            notice = failure_without_context(
                BuildInfo(
                    job=args.job,
                    buildNumber=args.build,
                    result="UNKNOWN",
                    buildUrl=f"jenkins://{args.job}/{args.build}",
                    branch=None,
                    commit=None,
                    logTail=None,
                    warnings=["JENKINS_URL is not configured"],
                ),
                None,
                "JENKINS_URL 未配置，无法访问 Jenkins 获取构建信息。",
            )
            _print_json(notice)
            return 0
        git_client = GitClient(settings.repo_cache_dir, max_output_chars=settings.max_tool_output_chars)
        jenkins_client = JenkinsClient(
            base_url=settings.jenkins_url or "",
            user=settings.jenkins_user,
            token=settings.jenkins_token,
            max_output_chars=settings.max_tool_output_chars,
        )
        notice = analyze_jenkins(
            repo=args.repo,
            job=args.job,
            build=args.build,
            jenkins_client=jenkins_client,
            git_client=git_client,
            log_tail_lines=args.log_tail_lines or settings.default_log_tail_lines,
            settings=settings,
        )
        _print_json(notice)
        return 0
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
