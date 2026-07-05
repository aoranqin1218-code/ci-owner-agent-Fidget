from __future__ import annotations

from typing import Any


def send_wecom_markdown(webhook_url: str, markdown: str, timeout: int = 10) -> dict[str, Any]:
    try:
        import requests
    except Exception as exc:
        return {"ok": False, "statusCode": None, "response": None, "error": f"requests import failed: {exc}"}
    payload = {"msgtype": "markdown", "markdown": {"content": markdown}}
    try:
        response = requests.post(webhook_url, json=payload, timeout=timeout)
        text = response.text
        ok = response.status_code == 200
        error = None if ok else f"HTTP {response.status_code}"
        try:
            data = response.json()
        except Exception:
            data = None
        if isinstance(data, dict) and data.get("errcode", 0) != 0:
            ok = False
            error = str(data.get("errmsg") or f"wecom errcode {data.get('errcode')}")
        return {"ok": ok, "statusCode": response.status_code, "response": text, "error": error}
    except Exception as exc:
        return {"ok": False, "statusCode": None, "response": None, "error": str(exc)}
