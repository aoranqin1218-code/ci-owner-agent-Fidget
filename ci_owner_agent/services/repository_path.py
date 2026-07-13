from __future__ import annotations

import re


_LINE_COLUMN_RE = re.compile(r":\d+(?::\d+)?$")
_WINDOWS_ABSOLUTE_RE = re.compile(r"^[A-Za-z]:/")
_URI_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")


def normalize_repository_path(value: str | None) -> str | None:
    raw = str(value or "").strip().strip("'\"`[]()")
    if not raw:
        return None
    path = _LINE_COLUMN_RE.sub("", raw.replace("\\", "/"))

    if path.startswith("file:///var/app/"):
        path = path[len("file:///var/app/") :]
    elif path.startswith("/var/app/"):
        path = path[len("/var/app/") :]
    elif path in {"/var/app", "file:///var/app"}:
        return None
    elif (
        path.startswith("/")
        or path.startswith("//")
        or _WINDOWS_ABSOLUTE_RE.match(path)
        or _URI_SCHEME_RE.match(path)
    ):
        return None

    path = re.sub(r"/+", "/", path)
    path = re.sub(r"^\./+", "", path)
    parts = [part for part in path.split("/") if part not in {"", "."}]
    if not parts or ".." in parts:
        return None
    return "/".join(parts)
