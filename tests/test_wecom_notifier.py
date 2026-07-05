from __future__ import annotations

import sys
import types

from ci_owner_agent.services.wecom_notifier import send_wecom_markdown


class FakeResponse:
    def __init__(self, status_code=200, text='{"errcode":0}', data=None):
        self.status_code = status_code
        self.text = text
        self._data = data if data is not None else {"errcode": 0}

    def json(self):
        return self._data


def test_requests_post_payload(monkeypatch):
    captured = {}

    def post(url, json, timeout):
        captured.update({"url": url, "json": json, "timeout": timeout})
        return FakeResponse()

    monkeypatch.setitem(sys.modules, "requests", types.SimpleNamespace(post=post))
    result = send_wecom_markdown("https://secret-webhook", "hello", timeout=3)
    assert result["ok"] is True
    assert captured["json"] == {"msgtype": "markdown", "markdown": {"content": "hello"}}
    assert captured["timeout"] == 3


def test_http_exception_returns_ok_false(monkeypatch):
    def post(url, json, timeout):
        raise RuntimeError("network down")

    monkeypatch.setitem(sys.modules, "requests", types.SimpleNamespace(post=post))
    result = send_wecom_markdown("https://secret-webhook", "hello")
    assert result["ok"] is False
    assert "network down" in result["error"]
    assert "secret-webhook" not in result["error"]


def test_wecom_errcode_non_zero_returns_ok_false(monkeypatch):
    def post(url, json, timeout):
        return FakeResponse(data={"errcode": 40001, "errmsg": "bad credential"})

    monkeypatch.setitem(sys.modules, "requests", types.SimpleNamespace(post=post))
    result = send_wecom_markdown("https://secret-webhook", "hello")
    assert result["ok"] is False
    assert result["error"] == "bad credential"


def test_http_status_non_200_returns_ok_false(monkeypatch):
    def post(url, json, timeout):
        return FakeResponse(status_code=500, text="boom", data={})

    monkeypatch.setitem(sys.modules, "requests", types.SimpleNamespace(post=post))
    result = send_wecom_markdown("https://secret-webhook", "hello")
    assert result["ok"] is False
    assert result["statusCode"] == 500
