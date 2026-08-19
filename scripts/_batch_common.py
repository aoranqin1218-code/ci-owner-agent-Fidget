"""Shared, side-effect-free helpers for batch and rerun scripts."""
from __future__ import annotations

import datetime as dt
import json
import os
import re
from pathlib import Path
from typing import Any


def load_env_file(env_file: Path, override: bool = False) -> None:
    if not env_file.exists():
        print(f"env file not found, skip: {env_file}")
        return
    try:
        from dotenv import load_dotenv

        load_dotenv(env_file, override=override)
        print(f"loaded env file: {env_file}")
        return
    except Exception as exc:
        print(f"python-dotenv unavailable, using simple .env parser: {exc}")

    for raw_line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = (part.strip() for part in line.split("=", 1))
        if value.startswith(("'", '"')) and value.endswith(("'", '"')) and len(value) >= 2:
            value = value[1:-1]
        if override or key not in os.environ:
            os.environ[key] = value
    print(f"loaded env file with fallback parser: {env_file}")


def slug(value: str) -> str:
    value = value.replace("/", "_").replace("\\", "_")
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_")


def cleanup_previous_outputs(paths: list[Path]) -> list[str]:
    warnings: list[str] = []
    for path in paths:
        try:
            if path.exists():
                path.unlink()
            if path.exists():
                warnings.append(f"failed to remove {path}: path still exists")
        except Exception as exc:
            warnings.append(f"failed to remove {path}: {exc}")
    return warnings


def extract_first_json_object(text: str) -> dict[str, Any] | None:
    decoder = json.JSONDecoder()
    for idx, ch in enumerate(text):
        if ch != "{":
            continue
        try:
            obj, _end = decoder.raw_decode(text[idx:])
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


def extract_responsibility_stats_from_notice(notice: dict[str, Any]) -> dict[str, Any]:
    items = notice.get("responsibilityItems")
    if not isinstance(items, list):
        items = []
    responsible_owners: list[str] = []
    inherited_owners: list[str] = []
    current_build_owners: list[str] = []
    unresolved = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        owner = item.get("owner") if isinstance(item.get("owner"), dict) else {}
        owner_name = str(owner.get("name") or "")
        owner_type = str(owner.get("type") or "")
        responsibility_type = str(item.get("responsibilityType") or "")
        if (
            responsibility_type in {"no_high_confidence_owner", "unknown"}
            or owner_type == "no_high_confidence_owner"
            or not owner_name
            or owner_name == "无高可信责任人"
        ):
            unresolved += 1
            continue
        if responsibility_type == "inherited_failure_owner":
            inherited_owners.append(owner_name)
            source_build = item.get("sourceBuildNumber")
            suffix = f"inherited from #{source_build}" if source_build is not None else "inherited"
            responsible_owners.append(f"{owner_name}({suffix})")
        elif responsibility_type == "current_build_owner":
            current_build_owners.append(owner_name)
            responsible_owners.append(f"{owner_name}({owner_type})")
        else:
            responsible_owners.append(f"{owner_name}({owner_type or responsibility_type})")
    return {
        "responsibilityItemCount": len(items),
        "responsibleOwners": "; ".join(_unique_in_order(responsible_owners)),
        "inheritedOwners": "; ".join(_unique_in_order(inherited_owners)),
        "currentBuildOwners": "; ".join(_unique_in_order(current_build_owners)),
        "unresolvedFailureCount": unresolved,
    }


def to_jsonable(obj: Any) -> Any:
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, (dt.datetime, dt.date)):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {str(k): to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [to_jsonable(v) for v in obj]
    if hasattr(obj, "model_dump"):
        try:
            return obj.model_dump(mode="json")
        except Exception:
            try:
                return obj.model_dump()
            except Exception:
                pass
    if hasattr(obj, "dict"):
        try:
            return obj.dict()
        except Exception:
            pass
    return str(obj)


def _unique_in_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result
