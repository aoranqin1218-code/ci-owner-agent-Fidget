from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class InvestigationScope:
    mode: str
    full_base_commit: str | None
    full_head_commit: str | None
    focus_base_commit: str | None = None
    focus_head_commit: str | None = None
    previous_build_number: int | None = None
    previous_build_url: str | None = None
    previous_build_result: str | None = None
    reason: str | None = None

    @property
    def has_focus_range(self) -> bool:
        return bool(self.focus_base_commit and self.focus_head_commit)

    def range_for_scope(self, scope: str) -> tuple[str | None, str | None]:
        if scope == "focus" and self.has_focus_range:
            return self.focus_base_commit, self.focus_head_commit
        return self.full_base_commit, self.full_head_commit
