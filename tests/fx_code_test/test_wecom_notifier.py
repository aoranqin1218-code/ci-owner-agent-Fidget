from __future__ import annotations

import requests

from ci_owner_agent.services.wecom_notifier import send_wecom_markdown


class _Response:
    def __init__(self, status_code=200, body=None, text=None):
        self.status_code = status_code
        self._body = {"errcode": 0, "errmsg": "ok"} if body is None else body
        self.text = text if text is not None else str(self._body)

    def json(self):
        return self._body


def test_send_wecom_markdown_payload_and_userid_mention(monkeypatch):
    captured = {}

    def post(url, *, json, timeout):
        captured.update(url=url, json=json, timeout=timeout)
        return _Response()

    monkeypatch.setattr("ci_owner_agent.services.wecom_notifier.requests.post", post)
    result = send_wecom_markdown("https://example.test/secret", "owner: <@alice>")
    assert result["ok"] is True and result["statusCode"] == 200
    assert captured == {
        "url": "https://example.test/secret",
        "json": {"msgtype": "markdown", "markdown": {"content": "owner: <@alice>"}},
        "timeout": 10,
    }


def test_send_wecom_markdown_rejects_nonzero_errcode(monkeypatch):
    monkeypatch.setattr("ci_owner_agent.services.wecom_notifier.requests.post",
                        lambda *a, **k: _Response(body={"errcode": 40001, "errmsg": "invalid"}))
    result = send_wecom_markdown("https://example.test/secret", "private markdown")
    assert result["ok"] is False and "40001" in result["error"]
    assert "private markdown" not in result["error"] and "https://example.test/secret" not in result["error"]


def test_send_wecom_markdown_rejects_non_2xx(monkeypatch):
    monkeypatch.setattr("ci_owner_agent.services.wecom_notifier.requests.post",
                        lambda *a, **k: _Response(status_code=503, text="unavailable"))
    result = send_wecom_markdown("https://example.test/secret", "private markdown")
    assert result["ok"] is False and result["statusCode"] == 503 and "503" in result["error"]
    assert "private markdown" not in result["error"] and "https://example.test/secret" not in result["error"]


def test_send_wecom_markdown_requires_exact_http_200(monkeypatch):
    monkeypatch.setattr("ci_owner_agent.services.wecom_notifier.requests.post",
                        lambda *a, **k: _Response(status_code=201))
    result = send_wecom_markdown("https://example.test/secret", "private markdown")
    assert result["ok"] is False and result["statusCode"] == 201


def test_send_wecom_markdown_network_error_is_structured_and_redacted(monkeypatch):
    url = "https://example.test/secret-key"
    calls = []
    def fail(*args, **kwargs):
        calls.append(1)
        raise requests.ConnectionError(f"unable to reach {url}")
    monkeypatch.setattr("ci_owner_agent.services.wecom_notifier.requests.post", fail)
    monkeypatch.setattr("ci_owner_agent.services.wecom_notifier.time.sleep", lambda _: None)
    result = send_wecom_markdown(url, "private markdown")
    assert result["ok"] is False and result["statusCode"] is None and result["response"] is None
    assert result["attemptCount"] == 3 and len(calls) == 3
    assert url not in result["error"] and "private markdown" not in result["error"]


def test_send_wecom_markdown_retries_connection_error_then_succeeds(monkeypatch):
    outcomes = [requests.ConnectionError("temporary DNS failure"), _Response()]
    sleeps = []

    def post(*args, **kwargs):
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr("ci_owner_agent.services.wecom_notifier.requests.post", post)
    monkeypatch.setattr("ci_owner_agent.services.wecom_notifier.time.sleep", sleeps.append)

    result = send_wecom_markdown("https://example.test/secret", "private markdown")

    assert result["ok"] is True and result["attemptCount"] == 2
    assert sleeps == [0.25]


def test_send_wecom_markdown_does_not_retry_read_timeout(monkeypatch):
    calls = []

    def post(*args, **kwargs):
        calls.append(1)
        raise requests.ReadTimeout("response was not received")

    monkeypatch.setattr("ci_owner_agent.services.wecom_notifier.requests.post", post)
    result = send_wecom_markdown("https://example.test/secret", "private markdown")

    assert result["ok"] is False and result["attemptCount"] == 1
    assert len(calls) == 1
