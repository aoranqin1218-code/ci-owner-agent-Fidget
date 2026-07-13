from __future__ import annotations

import re


REPOSITORY_ROOT_SEGMENTS = {"app", "lib", "modules", "packages", "server", "src", "test", "tests", "__tests__"}


def normalize_repository_path(value: str | None) -> str | None:
    raw = str(value or "").strip().strip("'\"`[]()")
    if not raw:
        return None
    raw = re.sub(r":\d+(?::\d+)?$", "", raw)
    path = re.sub(r"/+", "/", raw.replace("\\", "/"))
    path = re.sub(r"^[A-Za-z]:", "", path)
    path = re.sub(r"^\./+", "", path)
    if path.startswith("/var/app/"):
        path = path[len("/var/app/") :]
    was_absolute = path.startswith("/")
    path = path.lstrip("/")
    parts = [part for part in path.split("/") if part not in {"", "."}]
    if ".." in parts:
        return None
    if was_absolute:
        root_index = next((idx for idx, part in enumerate(parts) if part in REPOSITORY_ROOT_SEGMENTS), None)
        if root_index is not None:
            parts = parts[root_index:]
    return "/".join(parts) or None
