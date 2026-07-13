from __future__ import annotations

import re


UNSAFE_ABSOLUTE_PATH_RE = re.compile(
    r"(?i)^(?:[a-z]:)?/(?:tmp|var/tmp|bin|usr/bin|sbin|usr/sbin)(?:/|$)"
    r"|^(?:[a-z]:)?/users/[^/]+/appdata/local/temp(?:/|$)"
    r"|^(?:[a-z]:)?/windows/temp(?:/|$)"
)


def normalize_repository_path(value: str | None) -> str | None:
    raw = str(value or "").strip().strip("'\"`[]()")
    if not raw:
        return None
    raw = re.sub(r":\d+(?::\d+)?$", "", raw)
    path = _normalize_raw_path(raw)
    if path is None:
        return None
    is_absolute = bool(re.match(r"^(?:[A-Za-z]:)?/", path))
    if is_absolute and _is_unsafe_absolute_path(path):
        return None
    drive_less = re.sub(r"^[A-Za-z]:", "", path)
    parts = [part for part in drive_less.lstrip("/").split("/") if part not in {"", "."}]
    if ".." in parts:
        return None
    if is_absolute:
        parts = _strip_trusted_workspace_prefix(parts)
        if parts is None:
            return None
    return "/".join(parts) or None


def _normalize_raw_path(value: str) -> str | None:
    path = re.sub(r"/+", "/", value.replace("\\", "/"))
    path = re.sub(r"^\./+", "", path)
    return path or None


def _is_unsafe_absolute_path(path: str) -> bool:
    return bool(UNSAFE_ABSOLUTE_PATH_RE.match(path))


def _strip_trusted_workspace_prefix(parts: list[str]) -> list[str] | None:
    lowered = [part.lower() for part in parts]
    if lowered[:2] == ["var", "app"]:
        return parts[2:] or None
    for marker in ("_work", "workspace", "workspaces"):
        if marker not in lowered:
            continue
        marker_index = lowered.index(marker)
        if marker_index + 1 >= len(parts):
            return None
        repo_index = marker_index + 1
        next_index = repo_index + 1
        if next_index < len(parts) and lowered[next_index] == lowered[repo_index]:
            next_index += 1
        return parts[next_index:] or None
    return None
