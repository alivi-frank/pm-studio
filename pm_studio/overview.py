"""The overview: one read-only screen answering "what are we working on?".

Everything here is derived at read time from data the stores already hold - nothing is
persisted, so the screen can never disagree with the board it summarizes. The builder is
pure with respect to the stores (changes and groups are passed in, mirroring
`PortfolioStore.group_changes_by_initiative`), which is what makes the rollups testable
without a server.

Three questions, three sections, one payload:
- "what is in flight" - one row per initiative with progress, overdue and people;
- "what shipped recently" - a flat feed ordered newest first, because the answer a
  stakeholder wants is a list, not something reconstructed from per-project accordions;
- "who is on what" - the same load rows the board derives, trimmed for a summary.
"""

import time
from datetime import date

SHIPPED_WINDOW_DAYS = 30
SHIPPED_FEED_CAP = 25
OVERDUE_FEED_CAP = 25
WORKING_FEED_CAP = 25
NEXT_FEED_CAP = 25


def _days_between(earlier: float, later: float) -> int:
    return max(0, int((later - earlier) // 86400))


def _days_late(target_at: str, today: date) -> int:
    try:
        y, m, d = (int(part) for part in target_at.split("-"))
        return max(0, (today - date(y, m, d)).days)
    except (ValueError, AttributeError):
        return 0


def _assignee_name(change: dict) -> str | None:
    assigned = change.get("assigned")
    if isinstance(assigned, dict):
        return assigned.get("name") or None
    return None


def build_overview(
    groups: list[dict],
    workload_rows: list[dict],
    *,
    now: float | None = None,
    shipped_window_days: int = SHIPPED_WINDOW_DAYS,
) -> dict:
    """`groups` is `group_changes_by_initiative` output over the whole board (every
    change appears exactly once - the invariant that pivot already guarantees), so the
    totals here are conserved by construction. `workload_rows` is `people.workload`
    over the same changes."""
    now = time.time() if now is None else now
    today = date.fromtimestamp(now)
    window_start = now - shipped_window_days * 86400

    initiatives = []
    shipped_feed = []
    overdue_feed = []
    # The two halves of "what is planned next vs. being worked now", as actual items
    # rather than counts - the question a rollup alone keeps failing to answer.
    working_feed = []
    next_feed = []

    for group in groups:
        initiative = group.get("initiative")
        changes = [c for entry in group["projects"] for c in entry["changes"]]
        counts = {"total": len(changes), "pending": 0, "in_progress": 0, "done": 0}
        overdue = 0
        products: dict[str, int] = {}
        people: dict[str, int] = {}
        next_target: str | None = None
        shipped_in_window = 0
        last_shipped_at: float | None = None

        initiative_title = initiative["title"] if initiative else None
        for change in changes:
            status = change.get("status") or "pending"
            if status not in counts:
                status = "pending"
            counts[status] += 1
            products[change["product"]] = products.get(change["product"], 0) + 1
            name = _assignee_name(change)
            shipped_at = change.get("shipped_at")
            if status == "done":
                if shipped_at:
                    last_shipped_at = max(last_shipped_at or 0, shipped_at)
                    if shipped_at >= window_start:
                        shipped_in_window += 1
                        shipped_feed.append({
                            "id": change["id"],
                            "title": change["title"],
                            "product": change["product"],
                            "initiative": initiative_title,
                            "shipped_at": shipped_at,
                            "days_ago": _days_between(shipped_at, now),
                            "assignee": name,
                        })
                continue
            # open work from here down
            if name:
                people[name] = people.get(name, 0) + 1
            entry = {
                "id": change["id"],
                "title": change["title"],
                "product": change["product"],
                "initiative": initiative_title,
                "target_at": change.get("target_at"),
                "assignee": name,
            }
            if status == "in_progress":
                working_feed.append(entry)
            elif change.get("bucket") == "next":
                next_feed.append(entry)
            target = change.get("target_at")
            if change.get("is_overdue") and target:
                overdue += 1
                overdue_feed.append({
                    "id": change["id"],
                    "title": change["title"],
                    "product": change["product"],
                    "initiative": initiative_title,
                    "target_at": target,
                    "days_late": _days_late(target, today),
                    "assignee": name,
                })
            elif target and (next_target is None or target < next_target):
                next_target = target

        # Health is derived, never stored, from the same facts the row already shows -
        # a chip that can't be computed from visible numbers would just be a rumor.
        # "at_risk" = overdue work exists; "quiet" = open work but no motion (nothing in
        # progress, nothing shipped in the window) - staleness rendered as a state, not
        # hidden; "done" = nothing open. Ideation and closed override: expected silence
        # is not risk.
        status = (initiative or {}).get("status", "open")
        in_ideation = bool((initiative or {}).get("in_ideation"))
        open_count = counts["pending"] + counts["in_progress"]
        if status == "closed":
            health = "closed"
        elif in_ideation:
            health = "ideation"
        elif overdue:
            health = "at_risk"
        elif open_count == 0:
            health = "done" if counts["total"] else "empty"
        elif counts["in_progress"] == 0 and shipped_in_window == 0:
            health = "quiet"
        else:
            health = "on_track"

        row = {
            "id": initiative["id"] if initiative else None,
            "title": initiative_title,
            "status": status,
            "is_maintenance": bool((initiative or {}).get("is_maintenance")),
            "in_ideation": in_ideation,
            "health": health,
            "counts": counts,
            "open": counts["pending"] + counts["in_progress"],
            "pct_done": round(100 * counts["done"] / counts["total"]) if counts["total"] else 0,
            "overdue": overdue,
            "products": sorted(products, key=products.get, reverse=True),
            "people": [
                {"name": name, "open": count}
                for name, count in sorted(people.items(), key=lambda kv: -kv[1])
            ],
            "next_target": next_target,
            "shipped_recent": shipped_in_window,
            "last_shipped_at": last_shipped_at,
        }
        initiatives.append(row)

    # Declared initiatives first (most open work leading), then idle-but-open, ideation,
    # closed. Maintenance and the unaligned group (id None) trail the open rows whatever
    # their counts say: on a real board unaligned work is routinely the biggest bucket,
    # and letting bulk outrank intent would bury the actual initiatives under the
    # catch-all. Closed initiatives with nothing shipped in the window are dropped -
    # they are history, and the portfolio page still has them.
    def sort_key(row: dict) -> tuple:
        closed = row["status"] == "closed"
        return (
            closed,
            row["id"] is None and not closed,  # unaligned trails everything open
            row["is_maintenance"] and not closed,  # maintenance is intent, but not news
            row["in_ideation"],
            -row["counts"]["in_progress"],
            -row["open"],
            row["title"] or "~unaligned",
        )

    initiatives.sort(key=sort_key)
    initiatives = [
        row for row in initiatives
        if not (row["status"] == "closed" and row["shipped_recent"] == 0)
        and not (row["counts"]["total"] == 0 and row["id"] is None)
    ]

    shipped_feed.sort(key=lambda entry: -entry["shipped_at"])
    overdue_feed.sort(key=lambda entry: -entry["days_late"])
    # Dated commitments lead; the undated trail alphabetically so the order is stable.
    def plan_key(entry: dict) -> tuple:
        return (entry["target_at"] is None, entry["target_at"] or "", entry["title"])
    working_feed.sort(key=plan_key)
    next_feed.sort(key=plan_key)

    load = [
        {
            "person_id": row.get("person_id"),
            "name": row["name"],
            "open": row.get("open", 0),
            "in_progress": row.get("in_progress", 0),
            "overdue": row.get("overdue", 0),
            "now": row.get("now", 0),
            "shipped": row.get("shipped", 0),
        }
        for row in workload_rows
    ]

    return {
        "generated_at": now,
        "shipped_window_days": shipped_window_days,
        "initiatives": initiatives,
        "shipped": shipped_feed[:SHIPPED_FEED_CAP],
        "shipped_total": len(shipped_feed),
        "overdue": overdue_feed[:OVERDUE_FEED_CAP],
        "overdue_total": len(overdue_feed),
        "working": working_feed[:WORKING_FEED_CAP],
        "working_total": len(working_feed),
        "next_up": next_feed[:NEXT_FEED_CAP],
        "next_total": len(next_feed),
        "load": load,
    }
