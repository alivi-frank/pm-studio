"""Weekly commitment refs: what the Now lane held when the week's plan settled.

"Of what we planned, what shipped?" is unanswerable from current state alone, and
reconstructing the plan from a boundary timestamp is the fragile road (it is exactly
the mechanism behind Jira's backdated-sprint scope bug). So the plan is an explicit,
tiny artifact: one JSON file per ISO week, written once, capturing the open Now-lane
changes. A 24-hour grace window after the week boundary lets planning-day churn settle
before the plan pins - additions inside the grace still count as planned, the
convention the planning-accuracy tools converged on.

Capture is lazy - the first read or write that passes through the server after the
grace window writes the file - so no scheduler exists and an idle deployment simply
captures when it wakes. `captured_at` is recorded, never guessed, so a late capture is
visible rather than lying about when the plan was pinned. Files live under
`<workspace>/plans/`, server-owned like every other store, and deliberately NOT in the
gitignore: a plan is the one runtime artifact whose whole value is surviving history.
"""

import json
import threading
import time
from pathlib import Path

from .costing import week_bounds, week_key

GRACE_SECONDS = 24 * 3600


class PlanStore:
    def __init__(self, directory: Path) -> None:
        self._dir = directory
        self._lock = threading.Lock()

    def _path(self, week: str) -> Path:
        return self._dir / f"{week}.json"

    def ensure_current(self, changes: list[dict], now: float | None = None) -> None:
        """Writes this week's plan if the grace window has passed and none exists.

        Never raises: a plan that fails to write costs one week of plan-vs-actual,
        which must not take a read endpoint down with it.
        """
        now = time.time() if now is None else now
        week = week_key(now)
        start, _ = week_bounds(week)
        if now < start + GRACE_SECONDS:
            return
        path = self._path(week)
        with self._lock:
            if path.exists():
                return
            planned = [
                {
                    "id": change["id"],
                    "title": change.get("title", ""),
                    "product": change.get("product"),
                    "project_id": change.get("project_id"),
                }
                for change in changes
                if change.get("bucket") == "now" and change.get("status") != "done"
            ]
            try:
                self._dir.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(".tmp")
                tmp.write_text(json.dumps(
                    {"week": week, "captured_at": now, "planned": planned}, indent=2
                ))
                tmp.replace(path)
            except OSError as exc:  # pragma: no cover - disk trouble
                print(f"[pm_studio] plan capture failed for {week}: {exc}")

    def plan_for(self, week: str) -> dict | None:
        path = self._path(week)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            return None


def plan_vs_actual(plan: dict, changes: list[dict]) -> dict:
    """Classifies a week's plan against the CURRENT board - pure, store-free.

    Every planned item lands in exactly one bucket: shipped, still open in Now,
    moved (open but no longer Now), or gone (deleted). `added` counts today's
    Now-open work that was not in the plan - reported beside the fraction, never
    folded into it, the same separation the planning-accuracy tools keep.
    """
    by_id = {change["id"]: change for change in changes}
    shipped, still_now, moved, gone = [], [], [], []
    for item in plan.get("planned", []):
        change = by_id.get(item["id"])
        if change is None:
            gone.append(item)
        elif change.get("status") == "done":
            shipped.append(item)
        elif change.get("bucket") == "now":
            still_now.append(item)
        else:
            moved.append(item)
    planned_ids = {item["id"] for item in plan.get("planned", [])}
    added = [
        {"id": c["id"], "title": c.get("title", "")}
        for c in changes
        if c.get("bucket") == "now" and c.get("status") != "done"
        and c["id"] not in planned_ids
    ]
    total = len(plan.get("planned", []))
    return {
        "week": plan.get("week"),
        "captured_at": plan.get("captured_at"),
        "planned_total": total,
        "shipped": len(shipped),
        "still_now": len(still_now),
        "moved": len(moved),
        "gone": len(gone),
        "added": len(added),
        "shipped_fraction": round(len(shipped) / total, 4) if total else None,
    }
