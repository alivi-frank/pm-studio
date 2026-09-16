"""Activity-weighted time: where each person's days actually went.

The rule is simple enough to defend to an auditor:

- A person's *active day* (any non-bot signal of theirs on that calendar day) is
  worth `capacity_hours_per_day` hours. No signal, no hours - the model never invents
  effort on days it cannot see.
- Explicit time (worklogs, meeting durations) is booked first, exactly where it was
  logged.
- Whatever of the day's capacity remains is split across everything else the person
  touched that day, in proportion to signal weight.
- If explicit time already exceeds capacity, the day is worth the explicit total and
  nothing is inferred on top.

Hours are therefore an approximation by construction, but they *reconcile*: a person's
hours in a window always equal active days x capacity (or more, only where they
logged more), and every hour points at the signals that earned it.
"""

from __future__ import annotations

from collections import defaultdict

from .attribution import UNATTRIBUTED_PROJECT
from .model import Clock


def _target_key(slice_: dict) -> tuple:
    """Rows are kept per piece of work, not just per project: the ticket (or, with no
    ticket, the repository) is what lets an hour's FATE be looked up later - did the
    work it went into finish, stall, or never get planned. Project rollups sum them."""
    return (slice_["project_id"] or UNATTRIBUTED_PROJECT, slice_.get("ref") or "", "" if slice_.get("ref") else (slice_.get("repo") or ""))


TRUST_FULL = "full"
TRUST_SIGNAL = "signal"
TRUST_IGNORE = "ignore"
# A worklog kept as evidence only: its weight grows with its size but is bounded, so a
# tool that writes "8h" on every status move cannot dominate a day the way a real
# eight-hour entry would.
SIGNAL_WORKLOG_MIN_WEIGHT = 15.0
SIGNAL_WORKLOG_MAX_WEIGHT = 120.0


def allocate(slices: list[dict], clock: Clock, *, capacity_hours: float = 8.0, start: float | None = None, end: float | None = None, mode: str = "observed", worklog_trust: dict[str, str] | None = None) -> dict:
    """Returns {"rows": [...], "days": {...}} where each row is one (day, person,
    piece of work) allocation with its hours, method and the signal ids behind it.
    `worklog_trust` maps a source id to full / signal / ignore (default signal)."""
    trust = worklog_trust or {}
    per_day: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for s in slices:
        if s.get("minutes") is not None:
            # Meetings dropped in the inbox are durations someone attended - trusted
            # unless the deployment says otherwise; tracker time defaults to evidence.
            level = trust.get(s["source"], TRUST_FULL if s["source"] == "inbox" else TRUST_SIGNAL)
            if level == TRUST_IGNORE:
                continue
            if level == TRUST_SIGNAL:
                # Evidence, not hours: the minutes become a bounded weight and the
                # explicit time is dropped so nothing downstream books it.
                s = {**s, "minutes": None, "weight": max(SIGNAL_WORKLOG_MIN_WEIGHT, min(SIGNAL_WORKLOG_MAX_WEIGHT, float(s["minutes"]) / 2.0)) * s.get("share", 1.0)}
        if s["bot"]:
            continue
        if start is not None and s["at"] < start:
            continue
        if end is not None and s["at"] >= end:
            continue
        if s["kind"] == "ai_turn":
            # Agent turns are spend, not a person's hours.
            continue
        per_day[(clock.day(s["at"]), s["person_id"])].append(s)

    rows: list[dict] = []
    for (day, person_id), day_slices in per_day.items():
        person_name = day_slices[0]["person_name"]
        explicit: dict[str, float] = defaultdict(float)
        weights: dict[str, float] = defaultdict(float)
        evidence: dict[str, list[str]] = defaultdict(list)
        facts: dict[str, dict] = {}
        for s in day_slices:
            key = _target_key(s)
            facts.setdefault(key, {"initiative_id": s["initiative_id"], "goal_ids": s["goal_ids"], "product": s["product"], "system": s["system"], "capex": s["capex"], "maintenance": s["maintenance"], "nature": s["nature"], "via": s["via"], "person_external": s["person_external"], "ref": s.get("ref") or None, "repo": s.get("repo"), "change_id": s.get("change_id"), "ticket_type": s.get("ticket_type") or ""})
            if len(evidence[key]) < 40:
                evidence[key].append(s["signal_id"])
            if s["minutes"]:
                explicit[key] += s["minutes"] / 60.0
            elif s["weight"] > 0:
                weights[key] += s["weight"]
        explicit_total = sum(explicit.values())
        remaining = max(0.0, capacity_hours - explicit_total)
        weight_total = sum(weights.values())
        for key in set(explicit) | set(weights):
            hours = explicit.get(key, 0.0)
            inferred = 0.0
            if weight_total > 0 and remaining > 0 and weights.get(key):
                inferred = remaining * weights[key] / weight_total
            if hours + inferred <= 0:
                continue
            method = "logged" if inferred == 0 else ("mixed" if hours else "inferred")
            rows.append({
                "day": day, "week": clock.week(clock.day_start(day) + 43200), "month": day[:7],
                "person_id": person_id, "person_name": person_name,
                "project_id": None if key[0] == UNATTRIBUTED_PROJECT else key[0],
                **facts[key],
                "hours": round(hours + inferred, 3), "logged_hours": round(hours, 3), "inferred_hours": round(inferred, 3),
                "method": method, "signals": len(evidence[key]), "signal_ids": evidence[key],
            })
    rows.sort(key=lambda r: (r["day"], r["person_name"], r["project_id"] or "", r.get("ref") or ""))
    if mode == "scaled":
        rows = scale_to_weeks(rows, weekly_capacity=capacity_hours * 5)
    return {"rows": rows, "active_days": len(per_day), "mode": mode}


def scale_to_weeks(rows: list[dict], *, weekly_capacity: float) -> list[dict]:
    """The timesheet stance: a person who was active in a week worked the whole week,
    and the ledger only decides the split. Each (person, week)'s inferred hours are
    scaled so the week totals `weekly_capacity`; logged hours are never touched and a
    week already above capacity is left alone. `scale` is kept on the row so a reader
    can see how much of the hour was observed and how much was filled in."""
    totals: dict[tuple[str, str], float] = defaultdict(float)
    logged: dict[tuple[str, str], float] = defaultdict(float)
    for r in rows:
        totals[(r["person_id"], r["week"])] += r["hours"]
        logged[(r["person_id"], r["week"])] += r["logged_hours"]
    out = []
    for r in rows:
        key = (r["person_id"], r["week"])
        inferred_week = totals[key] - logged[key]
        target_inferred = max(0.0, weekly_capacity - logged[key])
        factor = (target_inferred / inferred_week) if inferred_week > 0 and totals[key] < weekly_capacity else 1.0
        scaled = dict(r)
        scaled["inferred_hours"] = round(r["inferred_hours"] * factor, 3)
        scaled["hours"] = round(r["logged_hours"] + scaled["inferred_hours"], 3)
        scaled["scale"] = round(factor, 3)
        out.append(scaled)
    return out


def rollup(rows: list[dict], key: str) -> list[dict]:
    """Hours by one dimension (initiative_id, project_id, person_id, product, system,
    nature, capex...). Unset keys land under None so totals stay conserved."""
    out: dict = {}
    for r in rows:
        value = r.get(key)
        if isinstance(value, list):
            values = value or [None]
            share = 1.0 / len(values)
        else:
            values, share = [value], 1.0
        for v in values:
            entry = out.setdefault(v, {"key": v, "hours": 0.0, "logged_hours": 0.0, "inferred_hours": 0.0, "people": set(), "days": set(), "signals": 0})
            entry["hours"] += r["hours"] * share
            entry["logged_hours"] += r["logged_hours"] * share
            entry["inferred_hours"] += r["inferred_hours"] * share
            entry["people"].add(r["person_id"])
            entry["days"].add(r["day"])
            entry["signals"] += r["signals"]
    result = []
    for entry in out.values():
        entry["people"] = len(entry["people"])
        entry["days"] = len(entry["days"])
        for k in ("hours", "logged_hours", "inferred_hours"):
            entry[k] = round(entry[k], 2)
        result.append(entry)
    result.sort(key=lambda e: -e["hours"])
    return result


def series(rows: list[dict], period: str, key: str, top: int = 8) -> dict:
    """Stacked time series: {periods: [...], series: [{key, values: [...]}], other}.
    The top-N keys by total hours keep their own line; the rest fold into "Other" -
    never a 9th hue."""
    totals: dict = defaultdict(float)
    grid: dict = defaultdict(lambda: defaultdict(float))
    periods: set[str] = set()
    for r in rows:
        p = r[period]
        periods.add(p)
        value = r.get(key)
        values = value if isinstance(value, list) and value else [value if not isinstance(value, list) else None]
        share = 1.0 / len(values)
        for v in values:
            totals[v] += r["hours"] * share
            grid[v][p] += r["hours"] * share
    ordered = sorted(totals, key=lambda k: -totals[k])
    keep = ordered[:top]
    fold = ordered[top:]
    plist = sorted(periods)
    out_series = [{"key": k, "total": round(totals[k], 1), "values": [round(grid[k][p], 2) for p in plist]} for k in keep]
    if fold:
        out_series.append({"key": "__other__", "total": round(sum(totals[k] for k in fold), 1), "values": [round(sum(grid[k][p] for k in fold), 2) for p in plist], "folded": len(fold)})
    return {"periods": plist, "series": out_series}
