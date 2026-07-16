from __future__ import annotations

from ci_owner_agent.services.branch_normalization import normalize_branch_name


def normalize_scope_values(values: list[str] | tuple[str, ...] | None) -> list[str] | None:
    if not values:
        return None
    normalized = sorted({str(value).strip() for value in values if str(value).strip()})
    return normalized or None


def normalize_branch_scope_values(values: list[str] | tuple[str, ...] | None) -> list[str] | None:
    if values is None:
        return None
    normalized = sorted({branch for branch in (normalize_branch_name(str(value)) for value in values) if branch})
    return normalized
