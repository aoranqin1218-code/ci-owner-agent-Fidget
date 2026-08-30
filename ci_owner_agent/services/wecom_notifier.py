from __future__ import annotations

import time
from typing import Any

import requests


def send_wecom_markdown(
    webhook_url: str,
    markdown: str,
    timeout: int = 10,
    *,
    max_attempts: int = 3,
    retry_backoff_seconds: float = 0.25,
) -> dict[str, Any]:
    """Send markdown through a WeCom group webhook without leaking its URL.

    Only connection failures are retried. HTTP responses and WeCom business
    errors are definitive and return immediately; read timeouts are not retried
    because the server may already have accepted the message.
    """
    payload = {"msgtype": "markdown", "markdown": {"content": markdown}}
    attempts = max(1, int(max_attempts))
    response = None
    for attempt in range(1, attempts + 1):
        try:
            response = requests.post(webhook_url, json=payload, timeout=timeout)
            break
        except requests.ConnectionError as exc:
            if attempt < attempts:
                time.sleep(max(0.0, retry_backoff_seconds) * attempt)
                continue
            return {
                "ok": False,
                "statusCode": None,
                "response": None,
                "attemptCount": attempt,
                "error": f"WeCom webhook request failed: {type(exc).__name__}",
            }
        except requests.RequestException as exc:
            return {
                "ok": False,
                "statusCode": None,
                "response": None,
                "attemptCount": attempt,
                "error": f"WeCom webhook request failed: {type(exc).__name__}",
            }
        except Exception as exc:  # pragma: no cover - protects callers from custom transports.
            return {
                "ok": False,
                "statusCode": None,
                "response": None,
                "attemptCount": attempt,
                "error": f"WeCom webhook request failed: {type(exc).__name__}",
            }

    assert response is not None  # loop always returns or assigns a response
    status_code = response.status_code
    response_text = (response.text or "").replace(webhook_url, "***")
    if status_code != 200:
        return {
            "ok": False,
            "statusCode": status_code,
            "response": response_text,
            "attemptCount": attempt,
            "error": f"WeCom webhook returned HTTP {status_code}",
        }
    try:
        body = response.json()
    except (TypeError, ValueError):
        return {
            "ok": False,
            "statusCode": status_code,
            "response": response_text,
            "attemptCount": attempt,
            "error": "WeCom webhook returned invalid JSON",
        }
    errcode = body.get("errcode") if isinstance(body, dict) else None
    if errcode != 0:
        return {
            "ok": False,
            "statusCode": status_code,
            "response": response_text,
            "attemptCount": attempt,
            "error": f"WeCom webhook returned errcode {errcode}",
        }
    return {
        "ok": True,
        "statusCode": status_code,
        "response": response_text,
        "attemptCount": attempt,
        "error": None,
    }
