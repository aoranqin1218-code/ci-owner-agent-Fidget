"""Feishu custom-bot webhook HTTP delivery.

Owns optional signature, the HTTP POST, response parsing and sanitized errors.
Does not own notice business, routing, dedup or MongoDB. No output ever contains
the webhook URL, secret or signature.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from typing import Any

import requests


_BUSINESS_ERROR_LABELS = {
    9499: "bad request",
    11232: "rate limited",
    19001: "invalid webhook",
    19021: "signature validation failed",
    19022: "IP not allowed",
    19024: "required keyword not found",
}


def build_feishu_signature(secret: str, timestamp: str) -> str:
    """Return the Feishu custom-bot signature for ``timestamp`` and ``secret``.

    Official rule: HMAC-SHA256 over ``"{timestamp}\\n{secret}"`` with the same
    string as key, then Base64. The timestamp is a second-level integer.
    """
    string_to_sign = f"{timestamp}\n{secret}"
    digest = hmac.new(string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def send_feishu_payload(
    webhook_url: str,
    payload: dict,
    *,
    secret: str | None = None,
    timeout: int = 10,
) -> dict[str, Any]:
    """Send a Feishu custom-bot payload without leaking URL, secret or sign."""
    body = dict(payload)
    if secret:
        timestamp = str(int(time.time()))
        body["timestamp"] = timestamp
        body["sign"] = build_feishu_signature(secret, timestamp)
    try:
        response = requests.post(webhook_url, json=body, timeout=timeout)
    except requests.RequestException as exc:
        return {
            "ok": False,
            "statusCode": None,
            "response": None,
            "error": f"Feishu webhook request failed: {type(exc).__name__}",
        }
    except Exception as exc:  # pragma: no cover - protects callers from custom transports.
        return {
            "ok": False,
            "statusCode": None,
            "response": None,
            "error": f"Feishu webhook request failed: {type(exc).__name__}",
        }

    status_code = response.status_code
    if status_code != 200:
        return {
            "ok": False,
            "statusCode": status_code,
            "businessCode": None,
            "response": None,
            "error": f"Feishu webhook returned HTTP {status_code}",
        }
    try:
        parsed = response.json()
    except (TypeError, ValueError):
        return {
            "ok": False,
            "statusCode": status_code,
            "businessCode": None,
            "response": None,
            "error": "Feishu webhook returned invalid JSON",
        }
    raw_code = parsed.get("code") if isinstance(parsed, dict) else None
    code = raw_code if isinstance(raw_code, int) and not isinstance(raw_code, bool) else None
    if code != 0:
        # 服务端响应及 msg 都是不可信输入，可能回显 webhook token、Secret
        # 或签名。只返回受控错误类别，不把任何原始响应带出 transport 边界。
        label = _BUSINESS_ERROR_LABELS.get(code, "business error")
        code_text = str(code) if code is not None else "unknown"
        return {
            "ok": False,
            "statusCode": status_code,
            "businessCode": code,
            "response": None,
            "error": f"Feishu webhook returned code {code_text}: {label}",
        }
    return {
        "ok": True,
        "statusCode": status_code,
        "businessCode": 0,
        "response": None,
        "error": None,
    }
