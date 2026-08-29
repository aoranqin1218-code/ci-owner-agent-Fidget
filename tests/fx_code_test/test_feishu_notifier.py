from __future__ import annotations

import base64
import json

import requests

from ci_owner_agent.services.feishu_notifier import build_feishu_signature, send_feishu_payload


class _Response:
    def __init__(self, status_code=200, body=None, text=None, json_error=False):
        self.status_code = status_code
        self._body = {"code": 0, "msg": "success", "data": {}} if body is None else body
        self.text = text if text is not None else str(self._body)
        self._json_error = json_error

    def json(self):
        if self._json_error:
            raise ValueError("no json")
        return self._body


def test_feishu_signature_is_deterministic_and_expected_vector():
    first = build_feishu_signature("my-secret-value", "1710000000")
    second = build_feishu_signature("my-secret-value", "1710000000")
    assert first == second == "2iM2rIX295itfpOTtE5kQShIIb5MpehitpjbkbXxhaE="
    # 可 Base64 解码，且随 secret 变化
    base64.b64decode(first)
    assert build_feishu_signature("other-secret", "1710000000") != first


def test_send_feishu_payload_success_and_signature(monkeypatch):
    captured = {}

    def post(url, *, json, timeout):
        captured.update(url=url, json=json, timeout=timeout)
        return _Response()

    monkeypatch.setattr("ci_owner_agent.services.feishu_notifier.requests.post", post)
    result = send_feishu_payload(
        "https://example.test/hook/secret-token",
        {"msg_type": "text", "content": {"text": "hello"}},
        secret="my-secret-value",
    )
    assert result["ok"] is True and result["statusCode"] == 200
    body = captured["json"]
    assert body["msg_type"] == "text"
    assert "timestamp" in body and "sign" in body
    assert body["sign"] == build_feishu_signature("my-secret-value", body["timestamp"])


def test_send_feishu_payload_without_secret_has_no_sign(monkeypatch):
    captured = {}

    def post(url, *, json, timeout):
        captured.update(json=json)
        return _Response()

    monkeypatch.setattr("ci_owner_agent.services.feishu_notifier.requests.post", post)
    send_feishu_payload("https://example.test/hook/x", {"msg_type": "text", "content": {"text": "hi"}})
    assert "timestamp" not in captured["json"]
    assert "sign" not in captured["json"]


def test_send_feishu_payload_rejects_business_code_nonzero(monkeypatch):
    monkeypatch.setattr(
        "ci_owner_agent.services.feishu_notifier.requests.post",
        lambda *a, **k: _Response(body={"code": 9499, "msg": "Bad Request", "data": {}}),
    )
    result = send_feishu_payload("https://example.test/hook/secret", {"msg_type": "text", "content": {"text": "x"}})
    assert result["ok"] is False
    assert "9499" in result["error"]
    assert "bad request" in result["error"]
    assert "https://example.test/hook/secret" not in result["error"]


def test_send_feishu_payload_classifies_official_rate_limit_code(monkeypatch):
    monkeypatch.setattr(
        "ci_owner_agent.services.feishu_notifier.requests.post",
        lambda *a, **k: _Response(body={"code": 11232, "msg": "rate limited", "data": {}}),
    )
    result = send_feishu_payload("https://example.test/hook/secret", {"msg_type": "text", "content": {"text": "x"}})
    assert result["ok"] is False
    assert result["businessCode"] == 11232
    assert "rate limited" in result["error"]


def test_send_feishu_payload_rejects_non_2xx(monkeypatch):
    monkeypatch.setattr(
        "ci_owner_agent.services.feishu_notifier.requests.post",
        lambda *a, **k: _Response(status_code=503, text="unavailable"),
    )
    result = send_feishu_payload("https://example.test/hook/secret", {"msg_type": "text", "content": {"text": "x"}})
    assert result["ok"] is False and result["statusCode"] == 503 and "503" in result["error"]


def test_send_feishu_payload_requires_exact_http_200(monkeypatch):
    monkeypatch.setattr(
        "ci_owner_agent.services.feishu_notifier.requests.post",
        lambda *a, **k: _Response(status_code=201),
    )
    result = send_feishu_payload("https://example.test/hook/secret", {"msg_type": "text", "content": {"text": "x"}})
    assert result["ok"] is False and result["statusCode"] == 201


def test_send_feishu_payload_non_json_is_failed(monkeypatch):
    monkeypatch.setattr(
        "ci_owner_agent.services.feishu_notifier.requests.post",
        lambda *a, **k: _Response(status_code=200, text="<html>not json</html>", json_error=True),
    )
    result = send_feishu_payload("https://example.test/hook/secret", {"msg_type": "text", "content": {"text": "x"}})
    assert result["ok"] is False
    assert "invalid JSON" in result["error"]


def test_send_feishu_payload_network_error_is_redacted(monkeypatch):
    url = "https://example.test/hook/very-secret-token"

    def fail(*args, **kwargs):
        raise requests.ConnectionError(f"unable to reach {url}")

    monkeypatch.setattr("ci_owner_agent.services.feishu_notifier.requests.post", fail)
    result = send_feishu_payload(url, {"msg_type": "text", "content": {"text": "secret payload"}})
    assert result["ok"] is False and result["statusCode"] is None and result["response"] is None
    assert url not in result["error"]
    assert "secret payload" not in result["error"]


def test_send_feishu_payload_timeout_is_redacted(monkeypatch):
    url = "https://example.test/hook/timeout-secret-token"

    def fail(*args, **kwargs):
        raise requests.Timeout(f"timeout while calling {url}")

    monkeypatch.setattr("ci_owner_agent.services.feishu_notifier.requests.post", fail)
    result = send_feishu_payload(url, {"msg_type": "text", "content": {"text": "private payload"}})
    raw = json.dumps(result, ensure_ascii=False)
    assert result["ok"] is False and result["statusCode"] is None
    assert "Timeout" in result["error"]
    assert "timeout-secret-token" not in raw
    assert "private payload" not in raw


def test_send_feishu_payload_never_returns_echoed_credentials_or_signature(monkeypatch):
    url = "https://example.test/hook/echoed-webhook-token"
    secret = "echoed-signing-secret"
    timestamp = "1710000000"
    sign = build_feishu_signature(secret, timestamp)
    echoed = f"bad {url} {secret} {sign}"

    monkeypatch.setattr("ci_owner_agent.services.feishu_notifier.time.time", lambda: int(timestamp))
    monkeypatch.setattr(
        "ci_owner_agent.services.feishu_notifier.requests.post",
        lambda *a, **k: _Response(body={"code": 19001, "msg": echoed}, text=echoed),
    )
    result = send_feishu_payload(
        url,
        {"msg_type": "text", "content": {"text": "x"}},
        secret=secret,
    )
    raw = json.dumps(result, ensure_ascii=False)
    assert result["ok"] is False and result["businessCode"] == 19001
    assert result["response"] is None
    assert "invalid webhook" in result["error"]
    for sensitive in ("echoed-webhook-token", secret, sign):
        assert sensitive not in raw
