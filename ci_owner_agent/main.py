from __future__ import annotations

import argparse
import csv
import io
import json
import sys
from contextlib import nullcontext
from pathlib import Path

from ci_owner_agent.constants import NO_OWNER_NAME
from ci_owner_agent.config import load_settings
from ci_owner_agent.orchestrator import analyze_jenkins, analyze_local, failure_without_context
from ci_owner_agent.schemas import BuildInfo, CiResponsibilityNotice, TestFileFailureStat
from ci_owner_agent.services.feedback_store import FeedbackStore
from ci_owner_agent.services.git_client import GitClient
from ci_owner_agent.services.history_store import get_history_store
from ci_owner_agent.services.jenkins_client import JenkinsClient
from ci_owner_agent.services.metrics import AnalysisMetricsRecorder, current_metrics_recorder, use_metrics_recorder
from ci_owner_agent.services.notification_formatter import format_wecom_markdown_notice, notification_digest
from ci_owner_agent.services.test_maintainer_mapping import TestMaintainerResolver
from ci_owner_agent.services.wecom_mongo_user_mapping import build_wecom_notice_mapper
from ci_owner_agent.services.wecom_notifier import send_wecom_markdown
from ci_owner_agent.services.test_failure_stats import TestFailureStatsService
from ci_owner_agent.services.weekly_test_report_config import load_weekly_test_report_config, parse_aware_datetime, resolve_period
from ci_owner_agent.services.weekly_test_report_service import WeeklyTestReportService

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


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
    analyze.add_argument("--notify", action="store_true")
    analyze.add_argument("--notify-dry-run", action="store_true")
    analyze.add_argument("--force-notify", action="store_true")

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

    notify = subparsers.add_parser("notify-notice", help="Send or preview a WeCom markdown notice")
    notify.add_argument("--notice-file", required=True)
    notify.add_argument("--dry-run", action="store_true")
    notify.add_argument("--force", action="store_true")
    notify.add_argument("--feedback-base-url", default=None)

    serve = subparsers.add_parser("serve-feedback", help="Start the feedback web server")
    serve.add_argument("--host", default=None)
    serve.add_argument("--port", type=int, default=None)
    serve.add_argument("--reload", action="store_true")

    feedback = subparsers.add_parser("feedback", help="Manage manual feedback")
    feedback_sub = feedback.add_subparsers(dest="feedback_command", required=True)
    apply = feedback_sub.add_parser("apply")
    apply.add_argument("--repo", required=True)
    apply.add_argument("--job", required=True)
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
    list_cmd.add_argument("--build", type=int, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    settings = load_settings()
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0)
    if args.command == "analyze-local":
        if args.build_timestamp:
            try:
                args.build_timestamp = parse_aware_datetime(args.build_timestamp).isoformat()
            except ValueError as exc:
                print(f"ERROR: {exc}", file=sys.stderr)
                return 2
        git_client = GitClient(settings.repo_cache_dir, max_output_chars=settings.max_tool_output_chars)
        recorder = _start_metrics(settings, args.job, args.build, args.repo, "analyze-local")
        try:
            with use_metrics_recorder(recorder):
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
                    last_successful_build_number=args.last_success_build,
                    previous_build_number=args.previous_build,
                    previous_commit=args.previous_commit,
                    build_timestamp=args.build_timestamp,
                )
        except ValueError as exc:
            recorder.record_error(exc)
            _finish_metrics(recorder, settings)
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        recorder.record_notice(notice)
        _print_json(notice)
        with use_metrics_recorder(recorder):
            _maybe_notify_notice(notice, settings, args.notify, args.notify_dry_run, args.force_notify)
        _finish_metrics(recorder, settings)
        return 0
    if args.command in {"test-failure-stats", "weekly-test-report"}:
        store = get_history_store(settings)
        if store is None:
            print("ERROR: history store disabled or unavailable", file=sys.stderr)
            return 2
        try:
            config = load_weekly_test_report_config(args.config_file or settings.weekly_test_report_config_file)
            if args.top is not None and args.top <= 0:
                raise ValueError("--top must be a positive integer")
            period_start, period_end = resolve_period(
                timezone_name=config.timezone, period=args.period, period_start=args.period_start, period_end=args.period_end
            )
        except ValueError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        jobs, branches = _split_values(args.job), _split_values(args.branch)
        if args.command == "test-failure-stats":
            values = TestFailureStatsService(store, config).aggregate(args.repo, jobs, branches, period_start, period_end)[: args.top or config.topN]
            _print_stats(values, args.format)
            return 0
        resolver = TestMaintainerResolver.from_yaml(settings.test_maintainer_mapping_file)
        service = WeeklyTestReportService(store, config, resolver=resolver,
                                          fallback_userids=settings.wecom_fallback_userids,
                                          webhook_url=settings.wecom_webhook_url,
                                          mention_mode=settings.wecom_mention_mode)
        report = service.generate(repo=args.repo, jobs=jobs, branches=branches, period_start=period_start,
                                  period_end=period_end, top_n=args.top)
        print(report["markdown"])
        if args.notify or args.dry_run:
            result = service.notify(report, repo=args.repo, jobs=jobs, branches=branches, period_start=period_start,
                                    period_end=period_end, dry_run=args.dry_run, force=args.force)
            if not result.get("ok"):
                print(f"ERROR: weekly report notification failed: {result.get('error')}", file=sys.stderr)
                return 2
            if result.get("reason"):
                print(json.dumps({k: v for k, v in result.items() if k != "markdown"}, ensure_ascii=False))
        return 0
    if args.command == "analyze":
        recorder = _start_metrics(settings, args.job, args.build, args.repo, "analyze")
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
                repo=args.repo,
            )
            recorder.record_notice(notice)
            _print_json(notice)
            with use_metrics_recorder(recorder):
                _maybe_notify_notice(notice, settings, args.notify, args.notify_dry_run, args.force_notify)
            _finish_metrics(recorder, settings)
            return 0
        git_client = GitClient(settings.repo_cache_dir, max_output_chars=settings.max_tool_output_chars)
        jenkins_client = JenkinsClient(
            base_url=settings.jenkins_url or "",
            user=settings.jenkins_user,
            token=settings.jenkins_token,
            max_output_chars=settings.max_tool_output_chars,
        )
        with use_metrics_recorder(recorder):
            notice = analyze_jenkins(
                repo=args.repo,
                job=args.job,
                build=args.build,
                jenkins_client=jenkins_client,
                git_client=git_client,
                log_tail_lines=args.log_tail_lines or settings.default_log_tail_lines,
                settings=settings,
            )
        recorder.record_notice(notice)
        _print_json(notice)
        with use_metrics_recorder(recorder):
            _maybe_notify_notice(notice, settings, args.notify, args.notify_dry_run, args.force_notify)
        _finish_metrics(recorder, settings)
        return 0
    if args.command == "notify-notice":
        notice_path = Path(args.notice_file)
        if not notice_path.exists():
            print(f"ERROR: notice file not found: {notice_path}", file=sys.stderr)
            return 2
        try:
            data = json.loads(notice_path.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"ERROR: invalid notice json: {exc}", file=sys.stderr)
            return 2
        try:
            notice = CiResponsibilityNotice.model_validate(data)
        except Exception as exc:
            print(f"ERROR: invalid notice schema: {exc}", file=sys.stderr)
            return 2
        dry_run = args.dry_run or settings.wecom_notify_dry_run
        try:
            result = _notify_notice(
                notice,
                settings,
                dry_run=dry_run,
                force=args.force,
                feedback_base_url=args.feedback_base_url or settings.feedback_base_url,
            )
        except Exception as exc:
            print(f"ERROR: notify failed unexpectedly: {exc}", file=sys.stderr)
            return 2
        if dry_run:
            print(result["markdown"])
        elif not result.get("ok"):
            print(f"WARNING: notify failed: {result.get('error')}", file=sys.stderr)
        return 0
    if args.command == "feedback":
        store = get_history_store(settings)
        if store is None:
            print("ERROR: history store disabled or unavailable", file=sys.stderr)
            return 2
        feedback_store = FeedbackStore(store)
        if args.feedback_command == "apply":
            try:
                result = feedback_store.apply_feedback(
                    repo=args.repo,
                    job=args.job,
                    build_number=args.build,
                    failure_id=args.failure_id,
                    failure_signature=args.failure_signature,
                    action=args.action,
                    owner_name=args.owner_name,
                    owner_email=args.owner_email,
                    owner_type=args.owner_type,
                    owner_commit=args.owner_commit,
                    source_build_number=args.source_build_number,
                    reviewer=args.reviewer,
                    note=args.note,
                )
            except ValueError as exc:
                print(f"ERROR: {exc}", file=sys.stderr)
                return 2
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            return 0
        if args.feedback_command == "list":
            print(json.dumps(feedback_store.list_feedback(repo=args.repo, job=args.job, build_number=args.build), ensure_ascii=False, indent=2, default=str))
            return 0
    if args.command == "serve-feedback":
        try:
            import uvicorn
        except Exception as exc:
            print(f"ERROR: uvicorn is required for serve-feedback: {exc}", file=sys.stderr)
            return 2
        uvicorn.run(
            "ci_owner_agent.server:app",
            host=args.host or settings.feedback_server_host,
            port=args.port or settings.feedback_server_port,
            reload=args.reload,
        )
        return 0
    parser.print_help()
    return 2


def _maybe_notify_notice(notice: CiResponsibilityNotice, settings, cli_notify: bool, cli_dry_run: bool, force: bool) -> None:
    if not (cli_notify or settings.wecom_notify_enabled):
        return
    if notice.result == "SUCCESS" and not settings.wecom_notify_on_success:
        return
    if not settings.wecom_notify_on_no_owner and not _has_responsible_item_owner(notice):
        return
    try:
        result = _notify_notice(notice, settings, dry_run=cli_dry_run or settings.wecom_notify_dry_run, force=force, feedback_base_url=settings.feedback_base_url)
    except Exception as exc:
        print(f"WARNING: notify failed unexpectedly: {exc}", file=sys.stderr)
        return
    if not result.get("ok"):
        print(f"WARNING: notify failed: {result.get('error')}", file=sys.stderr)


def _has_responsible_item_owner(notice: CiResponsibilityNotice) -> bool:
    for item in notice.responsibilityItems:
        owner = item.owner
        if owner.type != "no_high_confidence_owner" and owner.name and owner.name != NO_OWNER_NAME:
            return True
    return False


def _start_metrics(settings, job: str, build: int, repo: str, command: str) -> AnalysisMetricsRecorder:
    return AnalysisMetricsRecorder.start(
        enabled=settings.metrics_enabled,
        job=job,
        buildNumber=build,
        repo=repo,
        command=command,
        model_provider=settings.model_provider,
        model_name=settings.model_name,
    )


def _finish_metrics(recorder: AnalysisMetricsRecorder, settings) -> None:
    try:
        recorder.finish()
        recorder.append_jsonl(settings.metrics_file)
    except Exception as exc:
        recorder.record_error(exc)
        print(f"WARNING: metrics write failed: {exc}", file=sys.stderr)


def _notify_notice(notice: CiResponsibilityNotice, settings, *, dry_run: bool, force: bool, feedback_base_url: str | None) -> dict:
    recorder = current_metrics_recorder()
    stage = recorder.stage("notify") if recorder is not None else None
    with stage if stage is not None else nullcontext():
        store = get_history_store(settings)
        mapper = build_wecom_notice_mapper(settings, store)
        maintainer_resolver = TestMaintainerResolver.from_yaml(settings.test_maintainer_mapping_file)
        for warning in maintainer_resolver.warnings:
            print(f"WARNING: {warning}", file=sys.stderr)
        markdown = format_wecom_markdown_notice(
            notice,
            feedback_base_url=feedback_base_url,
            feedback_token=settings.feedback_shared_token,
            user_mapper=mapper,
            mention_mode=settings.wecom_mention_mode,
            fallback_userids=settings.wecom_fallback_userids,
            maintainer_resolver=maintainer_resolver,
            repo=notice.repo,
        )
        digest = notification_digest(
            notice,
            maintainer_resolver=maintainer_resolver,
            repo=notice.repo,
            fallback_userids=settings.wecom_fallback_userids,
            mention_mode=settings.wecom_mention_mode,
        )
        if store and settings.notification_dedup_enabled and not force and store.notification_sent(
            repo=str(notice.repo or ""), job=notice.job, branch=notice.branch, build_number=notice.buildNumber, notice_hash=digest
        ):
            return {"ok": True, "status": "skipped", "markdown": markdown}
        if dry_run:
            if store:
                store.save_notification(notice=notice, notice_hash=digest, channel="wecom", status="dry_run", message=markdown)
            return {"ok": True, "status": "dry_run", "markdown": markdown}
        if not settings.wecom_webhook_url:
            if store:
                store.save_notification(notice=notice, notice_hash=digest, channel="wecom", status="failed", message=markdown, error="CI_AGENT_WECOM_WEBHOOK_URL is not configured")
            return {"ok": False, "error": "CI_AGENT_WECOM_WEBHOOK_URL is not configured", "markdown": markdown}
        send_result = send_wecom_markdown(settings.wecom_webhook_url, markdown)
        if store:
            store.save_notification(
                notice=notice,
                notice_hash=digest,
                channel="wecom",
                status="sent" if send_result.get("ok") else "failed",
                message=markdown,
                error=send_result.get("error"),
            )
        return {**send_result, "markdown": markdown}


def _add_weekly_scope_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo", required=True)
    parser.add_argument("--job", action="append", default=[])
    parser.add_argument("--branch", action="append", default=[])
    parser.add_argument("--period", choices=["current-week", "previous-week"], default="previous-week")
    parser.add_argument("--period-start", default=None)
    parser.add_argument("--period-end", default=None)
    parser.add_argument("--top", type=int, default=None)
    parser.add_argument("--config-file", default=None)


def _split_values(values: list[str]) -> list[str] | None:
    result = [part.strip() for value in values for part in value.split(",") if part.strip()]
    return list(dict.fromkeys(result)) or None


def _print_stats(stats, output_format: str) -> None:
    docs = [item.model_dump(mode="json") for item in stats]
    if output_format == "json":
        print(json.dumps(docs, ensure_ascii=False, indent=2))
    elif output_format == "csv":
        buffer = io.StringIO()
        fields = list(docs[0]) if docs else list(TestFileFailureStat.model_fields)
        writer = csv.DictWriter(buffer, fieldnames=fields)
        writer.writeheader()
        for doc in docs:
            writer.writerow({k: json.dumps(v, ensure_ascii=False) if isinstance(v, list) else v for k, v in doc.items()})
        print(buffer.getvalue(), end="")
    else:
        print("| testFilePath | periodFailedBuildCount | consecutive | failureRate | important |")
        print("|---|---:|---:|---:|---|")
        for item in stats:
            print(f"| {item.testFilePath} | {item.periodFailedBuildCount} | {item.currentConsecutiveFailureCount} | {item.periodFailureRate:.1%} | {item.isImportant} |")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
