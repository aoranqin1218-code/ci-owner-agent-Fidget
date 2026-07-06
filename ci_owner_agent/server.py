from __future__ import annotations

import html
from urllib.parse import parse_qs, urlencode

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ci_owner_agent.config import Settings, load_settings
from ci_owner_agent.services.feedback_store import FeedbackStore
from ci_owner_agent.services.history_store import MongoHistoryStore, get_history_store


def create_app(settings: Settings | None = None, history_store: MongoHistoryStore | None = None) -> FastAPI:
    app_settings = settings or load_settings()
    app = FastAPI(title="ci-owner-agent feedback")
    app.state.settings = app_settings
    app.state.history_store = history_store

    @app.get("/health")
    def health() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/feedback", response_class=HTMLResponse)
    def feedback_page(job: str = Query(...), build: int = Query(...), token: str | None = Query(default=None)) -> HTMLResponse:
        forbidden = _forbidden_response(app_settings, token)
        if forbidden is not None:
            return forbidden
        store = _store_for_request(app)
        if store is None:
            return _html_error("history store unavailable", 500)
        notice_doc = store.notices.find_one({"job": job, "buildNumber": build})
        if not notice_doc:
            return _html_error("notice not found", 404)
        feedback_docs = list(store.feedback.find({"job": job, "buildNumber": build, "isActive": True}))
        return HTMLResponse(_render_feedback_page(notice_doc, feedback_docs, token))

    @app.post("/feedback", response_class=HTMLResponse)
    async def submit_feedback(request: Request):
        form = _parse_form(await request.body())
        job = form.get("job") or ""
        token = form.get("token")
        forbidden = _forbidden_response(app_settings, token)
        if forbidden is not None:
            return forbidden
        try:
            build = int(form.get("build") or "")
        except ValueError:
            return _html_error("invalid build", 400)
        store = _store_for_request(app)
        if store is None:
            return _html_error("history store unavailable", 500)
        if not store.notices.find_one({"job": job, "buildNumber": build}):
            return _html_error("notice not found", 404)
        try:
            FeedbackStore(store).apply_feedback(
                job=job,
                build_number=build,
                failure_id=form.get("failureId") or None,
                failure_signature=None,
                action=form.get("action") or "",
                owner_name=form.get("ownerName") or None,
                owner_email=form.get("ownerEmail") or None,
                owner_type=form.get("ownerType") or "high_confidence",
                reviewer=form.get("reviewer") or None,
                note=form.get("note") or None,
            )
        except ValueError as exc:
            return _html_error(str(exc), 400)
        query = {"job": job, "build": build}
        if token:
            query["token"] = token
        return RedirectResponse(f"/feedback?{urlencode(query)}", status_code=303)

    return app


def _store_for_request(app: FastAPI) -> MongoHistoryStore | None:
    return app.state.history_store or get_history_store(app.state.settings)


def _forbidden_response(settings: Settings, token: str | None) -> HTMLResponse | None:
    if settings.feedback_shared_token and token != settings.feedback_shared_token:
        return _html_error("forbidden", 403)
    return None


def _parse_form(body: bytes) -> dict[str, str]:
    parsed = parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)
    return {key: values[-1] if values else "" for key, values in parsed.items()}


def _render_feedback_page(notice_doc: dict, feedback_docs: list[dict], token: str | None) -> str:
    notice = notice_doc.get("notice") or {}
    job = str(notice_doc.get("job") or notice.get("job") or "")
    build = int(notice_doc.get("buildNumber") or notice.get("buildNumber") or 0)
    items = notice.get("responsibilityItems") or []
    feedback_by_failure = _feedback_by_failure(feedback_docs)
    item_blocks = []
    for idx, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            continue
        failure_id = str(item.get("failureId") or "")
        active_feedback = feedback_by_failure.get(failure_id) or feedback_by_failure.get(str(item.get("failureSignature") or ""))
        item_blocks.append(_render_item(idx, job, build, item, active_feedback, token))
    if not item_blocks:
        item_blocks.append("<p class=\"muted\">未识别到独立责任项。</p>")
    build_url = str(notice.get("buildUrl") or "")
    build_link = f"<a href=\"{_e(build_url)}\">{_e(build_url)}</a>" if build_url else "无"
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>CI 反馈 | {_e(job)} #{build}</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 32px; color: #1f2937; }}
    main {{ max-width: 980px; margin: 0 auto; }}
    .notice, .item {{ border: 1px solid #d1d5db; border-radius: 6px; padding: 16px; margin: 16px 0; }}
    .muted {{ color: #6b7280; }}
    label {{ display: block; margin-top: 10px; font-weight: 600; }}
    input, select, textarea {{ box-sizing: border-box; width: 100%; max-width: 640px; padding: 8px; margin-top: 4px; }}
    textarea {{ min-height: 80px; }}
    button {{ margin-top: 12px; padding: 8px 14px; }}
    dt {{ font-weight: 700; }}
    dd {{ margin: 0 0 8px 0; }}
  </style>
</head>
<body>
<main>
  <h1>CI 反馈 | {_e(job)} #{build}</h1>
  <section class="notice">
    <p><strong>构建链接：</strong>{build_link}</p>
    <p><strong>原始原因：</strong>{_e(notice.get("failureReason"))}</p>
  </section>
  <h2>责任项</h2>
  {''.join(item_blocks)}
</main>
</body>
</html>"""


def _render_item(idx: int, job: str, build: int, item: dict, feedback: dict | None, token: str | None) -> str:
    owner = item.get("owner") if isinstance(item.get("owner"), dict) else {}
    feedback_html = _render_feedback_status(feedback)
    hidden_token = f'<input type="hidden" name="token" value="{_e(token)}">' if token else ""
    return f"""
<section class="item">
  <h3>{idx}. {_e(item.get("failureTitle"))}</h3>
  <dl>
    <dt>责任类型</dt><dd>{_e(item.get("responsibilityType"))}</dd>
    <dt>当前责任人</dt><dd>{_e(owner.get("name") or "无高可信责任人")} / {_e(owner.get("email") or "-")}</dd>
    <dt>来源构建</dt><dd>{_e(item.get("sourceBuildNumber") or "-")}</dd>
    <dt>原因</dt><dd>{_e(item.get("reason") or "-")}</dd>
  </dl>
  {feedback_html}
  <form method="post" action="/feedback">
    <input type="hidden" name="job" value="{_e(job)}">
    <input type="hidden" name="build" value="{build}">
    <input type="hidden" name="failureId" value="{_e(item.get("failureId"))}">
    {hidden_token}
    <label>反馈动作
      <select name="action">
        <option value="confirm_owner">判断正确</option>
        <option value="correct_owner">责任人不对，修正责任人</option>
        <option value="mark_flaky">这是偶发/环境问题</option>
        <option value="mark_no_owner">无高可信责任人</option>
      </select>
    </label>
    <label>修正责任人姓名 <input name="ownerName" autocomplete="off"></label>
    <label>修正责任人邮箱 <input name="ownerEmail" autocomplete="off"></label>
    <label>责任人类型
      <select name="ownerType">
        <option value="high_confidence">high_confidence</option>
        <option value="medium_confidence">medium_confidence</option>
      </select>
    </label>
    <label>反馈人 <input name="reviewer" autocomplete="off"></label>
    <label>备注 <textarea name="note"></textarea></label>
    <button type="submit">提交反馈</button>
  </form>
</section>
"""


def _render_feedback_status(feedback: dict | None) -> str:
    if not feedback:
        return '<p class="muted">暂无 active feedback。</p>'
    owner = feedback.get("correctedOwner") if isinstance(feedback.get("correctedOwner"), dict) else {}
    corrected = owner.get("name") or "-"
    return (
        "<div class=\"notice\">"
        "<strong>已有 active feedback</strong>"
        f"<p>action: {_e(feedback.get('action'))}</p>"
        f"<p>correctedOwner: {_e(corrected)}</p>"
        f"<p>reviewer: {_e(feedback.get('reviewer') or '-')}</p>"
        f"<p>note: {_e(feedback.get('note') or '-')}</p>"
        "</div>"
    )


def _feedback_by_failure(feedback_docs: list[dict]) -> dict[str, dict]:
    result: dict[str, dict] = {}
    for doc in feedback_docs:
        failure_id = str(doc.get("failureId") or "")
        failure_signature = str(doc.get("failureSignature") or "")
        if failure_id:
            result[failure_id] = doc
        if failure_signature:
            result[failure_signature] = doc
    return result


def _html_error(message: str, status_code: int) -> HTMLResponse:
    return HTMLResponse(
        f"<!doctype html><html lang=\"zh-CN\"><meta charset=\"utf-8\"><title>{status_code}</title><body><h1>{status_code}</h1><p>{_e(message)}</p></body></html>",
        status_code=status_code,
    )


def _e(value: object) -> str:
    return html.escape(str(value or ""), quote=True)


app = create_app()
