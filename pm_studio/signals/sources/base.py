"""The adapter contract."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from ..model import Signal


@dataclass
class CollectResult:
    signals: list[Signal] = field(default_factory=list)
    # Free-form facts a source learned that downstream consumers want: e.g. the git
    # source's per-repo branch summaries, the tracker sources' per-ticket timelines.
    facts: dict = field(default_factory=dict)
    # Human-readable notes (partial fetch, skipped repos) surfaced on the Sources panel.
    notes: list[str] = field(default_factory=list)
    truncated: bool = False


class SignalSource(Protocol):
    """One kind of activity record.

    `id` is stable and unique per deployment ("git", "jira", "ado", "ado-prs", "pm",
    "calendar"...). `configured` says whether the source can run at all here (the
    calendar adapter without a drop folder is declared but not configured - the
    Sources panel shows it as such rather than hiding it, so the door is visibly
    open). `collect` returns everything since `since` (epoch); it may use `previous`
    facts from the last run to fetch incrementally, and must never raise on a single
    bad record - note it and continue."""

    id: str
    label: str
    category: str  # "code" | "tracker" | "agent" | "collaboration"

    @property
    def configured(self) -> bool: ...

    def describe(self) -> dict: ...

    def collect(self, since: float, previous: dict | None = None) -> CollectResult: ...
