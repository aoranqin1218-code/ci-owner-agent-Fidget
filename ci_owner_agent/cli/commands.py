"""Command execution and CLI output boundaries.

`ci_owner_agent.main` owns parser construction and this module owns the work
performed after argparse has produced a command namespace.
"""
from __future__ import annotations

import csv
import io
import json
import sys
from pathlib import Path
from typing import Any

from ci_owner_agent.orchestrator import (
    analyze_jenkins,
    analyze_local,
    failure_without_context,
)
from ci_owner_agent.schemas import (
    BuildInfo,
    CiResponsibilityNotice,
    TestFileFailureStat,
)
from ci_owner_agent.services import feishu_notice_service, wecom_notice_service
from ci_owner_agent.services.feedback_store import FeedbackStore
from ci_owner_agent.services.git_client import GitClient
from ci_owner_agent.services.history_store import get_history_store
from ci_owner_agent.services.jenkins_client import JenkinsClient
from ci_owner_agent.services.metrics import (
    AnalysisMetricsRecorder,
    use_metrics_recorder,
)
from ci_owner_agent.services.test_failure_stats import TestFailureStatsService
from ci_owner_agent.services.test_maintainer_mapping import TestMaintainerResolver
from ci_owner_agent.services.weekly_test_report_config import (
    load_weekly_test_report_config,
    parse_aware_datetime,
    resolve_period,
)
from ci_owner_agent.services.weekly_test_report_service import WeeklyTestReportService

_NOTIFICATION_SUMMARY_KEYS = (
    "ok",
    "status",
    "transport",
    "sent",
    "reason",
    "inserted",
    "deliveryKey",
    "importantItemCount",
    "normalItemCount",
    "ignoredItemCount",
)


def dispatch_command(args: Any, settings: Any, parser: Any) -> int:
    if args.command == "analyze-local":
        return _run_analyze_local(args, settings)
    if args.command in {"test-failure-stats", "weekly-test-report"}:
        return _run_test_failure_reporting(args, settings)
    if args.command == "analyze":
        return _run_analyze_jenkins(args, settings)
    if args.command == "notify-notice":
        return _run_notify_notice(args, settings)
    if args.command == "feedback":
        return _run_feedback(args, settings)
    if args.command == "serve-feedback":
        return _run_feedback_server(args, settings)
    if args.command == "serve-wecom-bot":
        return _run_wecom_bot(args, settings)
    parser.print_help()
    return 2


def _notification_result_summary(result: dict) -> dict:
    return {key: result.get(key) for key in _NOTIFICATION_SUMMARY_KEYS if key in result}


def _serialize_json(model: Any) -> str:
    return json.dumps(model.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n"


def _emit_json(model: Any, output_file: str | None = None) -> None:
    text = _serialize_json(model)
    if output_file:
        path = Path(output_file)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            print(f"ERROR: unable to create output directory: {exc}", file=sys.stderr)
            raise SystemExit(2) from exc
        tmp = path.with_suffix(path.suffix + ".tmp")
        try:
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(path)
        except OSError as exc:
            print(f"ERROR: unable to write notice file: {exc}", file=sys.stderr)
            raise SystemExit(2) from exc
        return
    try:
        sys.stdout.write(text)
    except UnicodeEncodeError:
        sys.stdout.buffer.write(text.encode("utf-8", errors="replace"))
        sys.stdout.buffer.flush()


def _run_analyze_local(args: Any, settings: Any) -> int:
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
    _emit_json(notice, args.output_file)
    with use_metrics_recorder(recorder):
        wecom_notice_service.maybe_notify_notice(notice, settings, args.notify, args.notify_dry_run, args.force_notify)
        feishu_notice_service.maybe_notify_notice(notice, settings)
    _finish_metrics(recorder, settings)
    return 0


def _run_test_failure_reporting(args: Any, settings: Any) -> int:
    store = get_history_store(settings)
    if store is None:
        print("ERROR: history store disabled or unavailable", file=sys.stderr)
        return 2
    try:
        config = load_weekly_test_report_config(args.config_file or settings.weekly_test_report_config_file)
        if args.top is not None and args.top <= 0:
            raise ValueError("--top must be a positive integer")
        period_start, period_end = resolve_period(
            timezone_name=config.timezone,
            period=args.period,
            period_start=args.period_start,
            period_end=args.period_end,
        )
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    jobs, branches = _split_values(args.job), _split_values(args.branch)
    if args.command == "test-failure-stats":
        values = TestFailureStatsService(store, config).aggregate(args.repo, jobs, branches, period_start, period_end)
        _print_stats(values[: args.top or config.topN], args.format)
        return 0
    resolver = TestMaintainerResolver.from_yaml(settings.test_maintainer_mapping_file)
    service = WeeklyTestReportService(
        store,
        config,
        resolver=resolver,
        fallback_userids=settings.wecom_fallback_userids,
        notification_transport=getattr(settings, "wecom_notify_transport", "webhook"),
        webhook_url=getattr(settings, "wecom_webhook_url", None),
        notification_chat_id=settings.wecom_bot_notify_chat_id,
        notification_dedup_enabled=settings.notification_dedup_enabled,
        outbox_lease_seconds=settings.wecom_bot_notify_lease_seconds,
        outbox_max_attempts=settings.wecom_bot_notify_max_attempts,
        mention_mode=settings.wecom_mention_mode,
    )
    report = service.generate(
        repo=args.repo,
        jobs=jobs,
        branches=branches,
        period_start=period_start,
        period_end=period_end,
        top_n=args.top,
    )
    print(report["markdown"])
    if args.notify or args.dry_run:
        try:
            result = service.notify(
                report,
                repo=args.repo,
                jobs=jobs,
                branches=branches,
                period_start=period_start,
                period_end=period_end,
                dry_run=args.dry_run,
                force=args.force,
            )
        except Exception:
            print("ERROR: weekly report notification failed unexpectedly", file=sys.stderr)
            return 2
        if not result.get("ok"):
            print(f"ERROR: weekly report notification failed: {result.get('error')}", file=sys.stderr)
            return 2
        if result.get("reason") or result.get("status"):
            print(json.dumps(_notification_result_summary(result), ensure_ascii=False))
    return 0


def _run_analyze_jenkins(args: Any, settings: Any) -> int:
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
    else:
        git_client = GitClient(settings.repo_cache_dir, max_output_chars=settings.max_tool_output_chars)
        jenkins_client = JenkinsClient(
            base_url=settings.jenkins_url,
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
    _emit_json(notice, args.output_file)
    with use_metrics_recorder(recorder):
        wecom_notice_service.maybe_notify_notice(notice, settings, args.notify, args.notify_dry_run, args.force_notify)
        feishu_notice_service.maybe_notify_notice(notice, settings)
    _finish_metrics(recorder, settings)
    return 0


def _run_notify_notice(args: Any, settings: Any) -> int:
    notice_path = Path(args.notice_file)
    if not notice_path.exists():
        print(f"ERROR: notice file not found: {notice_path}", file=sys.stderr)
        return 2
    try:
        data = json.loads(notice_path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        print(f"ERROR: invalid notice json: {exc}", file=sys.stderr)
        return 2
    try:
        notice = CiResponsibilityNotice.model_validate(data)
    except Exception as exc:
        print(f"ERROR: invalid notice schema: {exc}", file=sys.stderr)
        return 2
    if args.channel == "feishu":
        return _run_notify_notice_feishu(args, notice, settings)
    dry_run = args.dry_run or settings.wecom_notify_dry_run
    try:
        result = wecom_notice_service.notify_notice(
            notice,
            settings,
            dry_run=dry_run,
            force=args.force,
            feedback_base_url=args.feedback_base_url or settings.feedback_base_url,
        )
    except Exception:
        print("ERROR: notify failed unexpectedly", file=sys.stderr)
        return 2
    if dry_run:
        print(result["markdown"])
    elif not result.get("ok"):
        print(f"WARNING: notify failed: {result.get('error')}", file=sys.stderr)
        return 2
    else:
        print(json.dumps(_notification_result_summary(result), ensure_ascii=False))
    return 0


def _run_notify_notice_feishu(args: Any, notice: CiResponsibilityNotice, settings: Any) -> int:
    dry_run = args.dry_run or settings.feishu_notify_dry_run
    try:
        result = feishu_notice_service.notify_notice(notice, settings, dry_run=dry_run, force=args.force)
    except Exception:
        print("ERROR: feishu notify failed unexpectedly", file=sys.stderr)
        return 2
    if dry_run:
        if not result.get("ok"):
            print(f"WARNING: feishu dry-run failed: {result.get('error')}", file=sys.stderr)
            return 2
        print(json.dumps(result["payload"], ensure_ascii=False))
        return 0
    if not result.get("ok"):
        print(f"WARNING: feishu notify failed: {result.get('error')}", file=sys.stderr)
        return 2
    output = _notification_result_summary(result)
    if result.get("summary"):
        output["summary"] = result["summary"]
    print(json.dumps(output, ensure_ascii=False))
    return 0


def _run_feedback(args: Any, settings: Any) -> int:
    store = get_history_store(settings)
    if store is None:
        print("ERROR: history store disabled or unavailable", file=sys.stderr)
        return 2
    feedback_store = FeedbackStore(store)
    try:
        if args.feedback_command == "apply":
            result = feedback_store.apply_feedback(
                repo=args.repo,
                job=args.job,
                branch=args.branch,
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
        else:
            result = feedback_store.list_feedback(
                repo=args.repo,
                job=args.job,
                branch=args.branch,
                build_number=args.build,
            )
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


def _run_feedback_server(args: Any, settings: Any) -> int:
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


def _run_wecom_bot(args: Any, settings: Any) -> int:
    bot_id = str(args.bot_id or settings.wecom_bot_id or "").strip()
    secret = str(args.secret or settings.wecom_bot_secret or "").strip()
    if not settings.history_enabled:
        print("ERROR: MongoDB history storage must be enabled for serve-wecom-bot", file=sys.stderr)
        return 2
    if not settings.wecom_bot_enabled:
        print("ERROR: CI_AGENT_WECOM_BOT_ENABLED must be enabled for serve-wecom-bot", file=sys.stderr)
        return 2
    if not bot_id:
        print("ERROR: Bot ID is required (--bot-id or CI_AGENT_WECOM_BOT_ID)", file=sys.stderr)
        return 2
    if not secret:
        print("ERROR: Secret is required (--secret or CI_AGENT_WECOM_BOT_SECRET)", file=sys.stderr)
        return 2
    bot_transport_enabled = settings.wecom_notify_transport == "bot"
    default_bot_notification_enabled = settings.wecom_notify_enabled and bot_transport_enabled
    if default_bot_notification_enabled and not settings.wecom_bot_notify_chat_id:
        print(
            "ERROR: CI_AGENT_WECOM_BOT_NOTIFY_CHAT_ID is required when default bot notifications are enabled",
            file=sys.stderr,
        )
        return 2
    notification_chat_id = settings.wecom_bot_notify_chat_id if bot_transport_enabled else None
    store = get_history_store(settings)
    if store is None:
        print("ERROR: MongoDB history storage is unavailable", file=sys.stderr)
        return 2
    try:
        admin = getattr(store.client, "admin", None)
        if admin is not None:
            admin.command("ping")
    except Exception as exc:
        print(f"ERROR: MongoDB history storage is unavailable: {exc}", file=sys.stderr)
        return 2
    try:
        from ci_owner_agent.services.wecom_bot_adapter import WeComSdkAdapter
        from ci_owner_agent.services.wecom_bot_worker import WeComBotWorker

        adapter = WeComSdkAdapter(bot_id, secret)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    worker = WeComBotWorker(
        adapter,
        store,
        confirm_ttl_seconds=settings.wecom_bot_confirm_ttl_seconds,
        feedback_code_ttl_days=settings.wecom_feedback_code_ttl_days,
        event_ttl_days=settings.wecom_bot_event_ttl_days,
        ai_parser=_build_wecom_feedback_ai_parser(settings),
        card_action_url=settings.feedback_base_url or "https://work.weixin.qq.com/",
        discover_chat_id=settings.wecom_bot_discover_chat_id,
        notification_chat_id=notification_chat_id,
        notification_poll_seconds=settings.wecom_bot_notify_poll_seconds,
        notification_lease_seconds=settings.wecom_bot_notify_lease_seconds,
        notification_max_attempts=settings.wecom_bot_notify_max_attempts,
    )
    try:
        worker.run()
    except Exception as exc:
        print(f"ERROR: WeCom bot stopped because of a fatal error: {exc}", file=sys.stderr)
        return 2
    return 0


def _build_wecom_feedback_ai_parser(settings: Any) -> Any:
    """Build AI parser for WeCom feedback, or None if disabled or invalid."""
    if not settings.wecom_bot_llm_enabled:
        return None
    if settings.model_provider.strip().lower() == "fake":
        from ci_owner_agent.services.wecom_feedback_ai_parser import FakeWeComFeedbackAiParser

        return FakeWeComFeedbackAiParser()
    from ci_owner_agent.config import validate_model_settings

    validation_error = validate_model_settings(settings)
    if validation_error:
        print(f"WARNING: WeCom bot AI parser disabled: {validation_error}", file=sys.stderr)
        return None
    try:
        from ci_owner_agent.services.wecom_feedback_ai_parser import WeComFeedbackAiParser

        return WeComFeedbackAiParser(settings, max_input_chars=settings.wecom_bot_llm_max_input_chars)
    except Exception as exc:
        print(f"WARNING: WeCom bot AI parser init failed: {exc}", file=sys.stderr)
        return None


def _start_metrics(settings: Any, job: str, build: int, repo: str, command: str) -> AnalysisMetricsRecorder:
    return AnalysisMetricsRecorder.start(
        enabled=settings.metrics_enabled,
        job=job,
        buildNumber=build,
        repo=repo,
        command=command,
        model_provider=settings.model_provider,
        model_name=settings.model_name,
    )


def _finish_metrics(recorder: AnalysisMetricsRecorder, settings: Any) -> None:
    try:
        recorder.finish()
        recorder.append_jsonl(settings.metrics_file)
    except Exception as exc:
        recorder.record_error(exc)
        print(f"WARNING: metrics write failed: {exc}", file=sys.stderr)


def _split_values(values: list[str]) -> list[str] | None:
    result = [part.strip() for value in values for part in value.split(",") if part.strip()]
    return list(dict.fromkeys(result)) or None


def _print_stats(stats: list[TestFileFailureStat], output_format: str) -> None:
    docs = [item.model_dump(mode="json") for item in stats]
    if output_format == "json":
        print(json.dumps(docs, ensure_ascii=False, indent=2))
    elif output_format == "csv":
        buffer = io.StringIO()
        fields = list(docs[0]) if docs else list(TestFileFailureStat.model_fields)
        writer = csv.DictWriter(buffer, fieldnames=fields)
        writer.writeheader()
        for doc in docs:
            writer.writerow({key: json.dumps(value, ensure_ascii=False) if isinstance(value, list) else value for key, value in doc.items()})
        print(buffer.getvalue(), end="")
    else:
        print("| testFilePath | periodFailedBuildCount | consecutive | failureRate | important |")
        print("|---|---:|---:|---:|---|")
        for item in stats:
            print(f"| {item.testFilePath} | {item.periodFailedBuildCount} | {item.currentConsecutiveFailureCount} | {item.periodFailureRate:.1%} | {item.isImportant} |")
