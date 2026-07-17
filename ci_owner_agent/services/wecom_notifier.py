from __future__ import annotations

from typing import Any

import requests


def send_wecom_markdown(
    webhook_url: str,
    markdown: str,
    timeout: int = 10,
) -> dict[str, Any]:
    """Send markdown through a WeCom group webhook without leaking its URL."""
    payload = {"msgtype": "markdown", "markdown": {"content": markdown}}
    try:
        response = requests.post(webhook_url, json=payload, timeout=timeout)
    except requests.RequestException as exc:
        return {
            "ok": False,
            "statusCode": None,
            "response": None,
            "error": f"WeCom webhook request failed: {type(exc).__name__}",
        }
    except Exception as exc:  # pragma: no cover - protects callers from custom transports.
        return {
            "ok": False,
            "statusCode": None,
            "response": None,
            "error": f"WeCom webhook request failed: {type(exc).__name__}",
        }

    status_code = response.status_code
    response_text = (response.text or "").replace(webhook_url, "***")
    if status_code != 200:
        return {
            "ok": False,
            "statusCode": status_code,
            "response": response_text,
            "error": f"WeCom webhook returned HTTP {status_code}",
        }
    try:
        body = response.json()
    except (TypeError, ValueError):
        return {
            "ok": False,
            "statusCode": status_code,
            "response": response_text,
            "error": "WeCom webhook returned invalid JSON",
        }
    errcode = body.get("errcode") if isinstance(body, dict) else None
    if errcode != 0:
        return {
            "ok": False,
            "statusCode": status_code,
            "response": response_text,
            "error": f"WeCom webhook returned errcode {errcode}",
        }
    return {"ok": True, "statusCode": status_code, "response": response_text, "error": None}
