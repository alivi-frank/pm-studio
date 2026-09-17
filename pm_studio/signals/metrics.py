"""Metrics derived from the ledger for one time window.

Three families, one payload:
- flow: how work moves (cycle, lead, review dwell, reopen, throughput, WIP);
- workflow & developer experience: how people work (hour-of-day, after-hours, context
  switching, tagging discipline, AI assistance, pull-request latency);
- impact: what each initiative's effort produced (shipped, closed, released) beside
  what it cost.

Everything is computed from slices + per-ticket facts in the window, so the same
function answers for a week or a year.
"""

from __future__ import annotations

from collections import defaultdict
from statistics import median

from .allocation import rollup
from .model import (
    KIND_COMMIT,
    KIND_MERGE,
    KIND_PR_MERGED,
    KIND_PR_OPENED,
    KIND_PR_REVIEW,
    KIND_STATUS,
    KIND_WORKLOG,
    Clock,
)

import re

REVIEW_MARKERS = ("review", "qa", "uat", "test", "verify", "validation")
# A tracker's own category can call "Resolved" or "UAT Completed" in-progress (ADO's
# Agile process does, for Feature and Task). By name these are finished work awaiting
# closure, and counting them as in flight was the single largest source of false
# findings on the first real run - the judge caught it. Names win over categories.
RESOLVED_NAME_RE = re.compile(r"resolved|uat completed|qa passed|ready for (release|prod|deploy)|awaiting (release|closure)|verified|accepted", re.IGNORECASE)
REMOVED_NAME_RE = re.compile(r"rejected|cancel+ed|removed|won'?t (do|fix)|declined|duplicate|obsolete", re.IGNORECASE)


def normalize_cat(status: str, cat: str) -> str:
    """The category a status name really means, whatever the tracker declared."""
    name = status or ""
    if cat == "done":
        return cat
    if REMOVED_NAME_RE.search(name):
        return "removed"
    if RESOLVED_NAME_RE.search(name):
        return "resolved"
    return cat
BLOCKED_MARKERS = ("block", "hold", "wait")
DONE_CATS = ("done",)
ACTIVE_CATS = ("in_progress", "resolved")


def percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    idx = max(0, min(len(ordered) - 1, int(round((pct / 100.0) * (len(ordered) - 1)))))
    return ordered[idx]


def histogram(values: list[float], edges: list[float]) -> list[dict]:
    """Counts per bucket: [<e0], [e0,e1), ..., [>=last]. Labels in days."""
    buckets = [0] * (len(edges) + 1)
    for v in values:
        placed = False
        for i, edge in enumerate(edges):
            if v < edge:
                buckets[i] += 1
                placed = True
                break
        if not placed:
            buckets[-1] += 1
    labels = []
    prev = 0.0
    for edge in edges:
        labels.append(f"{prev:g}–{edge:g}d")
        prev = edge
    labels.append(f"{prev:g}d+")
    return [{"label": l, "count": c} for l, c in zip(labels, buckets)]


def _is_review(name: str) -> bool:
    lowered = (name or "").lower()
    return any(m in lowered for m in REVIEW_MARKERS)


def _is_blocked(name: str) -> bool:
    lowered = (name or "").lower()
    return any(m in lowered for m in BLOCKED_MARKERS)


def ticket_timelines(issue_facts: dict[str, dict], *, now: float) -> dict[str, dict]:
    """Per ticket: first start, first done, last done, reopen count, dwell per status
    (seconds), and its current status age. Derived once; several metrics read it."""
    out: dict[str, dict] = {}
    for ref, fact in issue_facts.items():
        transitions = sorted(fact.get("transitions") or [], key=lambda t: t["at"])
        created = fact.get("created")
        first_start = None
        first_done = None
        last_done = None
        reopens = 0
        dwell: dict[str, float] = defaultdict(float)
        prev_at = created
        prev_state = None
        was_done = False
        for t in transitions:
            if prev_state is not None and prev_at is not None:
                dwell[prev_state] += max(0.0, t["at"] - prev_at)
            to_cat = normalize_cat(t.get("to") or "", t.get("to_cat") or "")
            if to_cat in ACTIVE_CATS and first_start is None:
                first_start = t["at"]
            if to_cat in DONE_CATS:
                if first_done is None:
                    first_done = t["at"]
                last_done = t["at"]
                was_done = True
            elif was_done and to_cat in ("todo", "in_progress"):
                reopens += 1
                was_done = False
            prev_at, prev_state = t["at"], t["to"]
        current = fact.get("status") or prev_state or ""
        if prev_at is not None and prev_state is not None:
            dwell[prev_state] += max(0.0, now - prev_at)
        review_secs = sum(v for k, v in dwell.items() if _is_review(k))
        blocked_secs = sum(v for k, v in dwell.items() if _is_blocked(k))
        out[ref] = {
            "created": created, "first_start": first_start, "first_done": first_done, "last_done": last_done or fact.get("resolved"),
            "reopens": reopens, "review_secs": review_secs, "blocked_secs": blocked_secs,
            "status": current, "status_cat": normalize_cat(current, fact.get("status_cat") or ""), "status_since": fact.get("status_since") or prev_at,
            "type": fact.get("type") or "", "transitions": len(transitions),
        }
    return out


def flow_metrics(timelines: dict[str, dict], slices: list[dict], *, start: float, end: float, clock: Clock) -> dict:
    """Cycle/lead/review/reopen for tickets that finished in the window, plus weekly
    throughput and a WIP curve."""
    cycle: list[float] = []
    lead: list[float] = []
    review: list[float] = []
    blocked: list[float] = []
    reopened = 0
    finished = 0
    by_nature: dict[str, list[float]] = defaultdict(list)
    finished_by_week: dict[str, int] = defaultdict(int)
    started_by_week: dict[str, int] = defaultdict(int)
    nature_of = {}
    for s in slices:
        if s.get("ref") and s.get("nature"):
            nature_of.setdefault(s["ref"], s["nature"])
    for ref, tl in timelines.items():
        done = tl["last_done"]
        if done and start <= done < end:
            finished += 1
            finished_by_week[clock.week(done)] += 1
            if tl["first_start"]:
                days = (done - tl["first_start"]) / 86400.0
                cycle.append(days)
                by_nature[nature_of.get(ref, "other")].append(days)
            if tl["created"]:
                lead.append((done - tl["created"]) / 86400.0)
            if tl["review_secs"]:
                review.append(tl["review_secs"] / 86400.0)
            if tl["blocked_secs"]:
                blocked.append(tl["blocked_secs"] / 86400.0)
            if tl["reopens"]:
                reopened += 1
        if tl["first_start"] and start <= tl["first_start"] < end:
            started_by_week[clock.week(tl["first_start"])] += 1
    # WIP at each week boundary in the window: started before, not done before.
    weeks = sorted(set(list(finished_by_week) + list(started_by_week)))
    wip_curve = []
    cursor = start
    while cursor < end:
        week = clock.week(cursor)
        wip = sum(1 for tl in timelines.values() if tl["first_start"] and tl["first_start"] < cursor + 7 * 86400 and not (tl["last_done"] and tl["last_done"] < cursor + 7 * 86400))
        wip_curve.append({"week": week, "wip": wip, "finished": finished_by_week.get(week, 0), "started": started_by_week.get(week, 0)})
        cursor += 7 * 86400
    # Finished but never closed, as of now - a live queue, not window-bound: the
    # closure lag the tracker's own "done" figures hide.
    awaiting = [tl for tl in timelines.values() if tl["status_cat"] == "resolved" and tl.get("status_since")]
    now = end
    ages = [(now - tl["status_since"]) / 86400.0 for tl in awaiting]
    return {
        "finished": finished,
        "awaiting_closure": {"count": len(awaiting), "p50_days": _r(percentile(ages, 50)), "p85_days": _r(percentile(ages, 85))},
        "cycle_days": {"p50": _r(percentile(cycle, 50)), "p85": _r(percentile(cycle, 85)), "n": len(cycle), "histogram": histogram(cycle, [1, 3, 7, 14, 30, 60])},
        "lead_days": {"p50": _r(percentile(lead, 50)), "p85": _r(percentile(lead, 85)), "n": len(lead)},
        "review_days": {"p50": _r(percentile(review, 50)), "p85": _r(percentile(review, 85)), "n": len(review)},
        "blocked_days": {"p50": _r(percentile(blocked, 50)), "p85": _r(percentile(blocked, 85)), "n": len(blocked)},
        "reopen_rate_pct": round(100.0 * reopened / finished, 1) if finished else 0.0,
        "cycle_by_nature": {k: {"p50": _r(percentile(v, 50)), "n": len(v)} for k, v in by_nature.items()},
        "weekly": wip_curve,
    }


def _r(value: float | None) -> float | None:
    return None if value is None else round(value, 1)


def workflow_metrics(slices: list[dict], alloc_rows: list[dict], *, start: float, end: float, clock: Clock, repo_facts: dict) -> dict:
    """How people work: when commits happen, how fragmented days are, how well work
    is tagged, how much is AI-assisted, how long PRs stay open."""
    heat = [[0] * 24 for _ in range(7)]
    commits = after_hours = weekend = ai = keyed = 0
    per_repo: dict[str, dict] = defaultdict(lambda: {"commits": 0, "keyed": 0, "merges": 0, "authors": set(), "ai": 0})
    pr_open_hours: list[float] = []
    prs_opened = prs_merged = reviews = 0
    per_person_days: dict[str, set] = defaultdict(set)
    kind_counts: dict[str, int] = defaultdict(int)
    source_counts: dict[str, int] = defaultdict(int)
    for s in slices:
        if s["at"] < start or s["at"] >= end or s["bot"]:
            continue
        kind_counts[s["kind"]] += 1
        source_counts[s["source"]] += 1
        if s["kind"] == KIND_COMMIT:
            weight_once = s["share"]  # sums to 1 per commit across its slices
            commits += weight_once
            hour = s["meta"].get("hour_local")
            wd = s["meta"].get("weekday_local")
            if hour is not None and wd is not None:
                heat[wd][hour] += weight_once
                if wd >= 5:
                    weekend += weight_once
                elif hour < 8 or hour >= 19:
                    after_hours += weight_once
            if s["ai"]:
                ai += weight_once
            if s["ref"]:
                keyed += weight_once
            repo = per_repo[s["repo"] or "?"]
            repo["commits"] += weight_once
            repo["keyed"] += weight_once if s["ref"] else 0
            repo["ai"] += weight_once if s["ai"] else 0
            repo["authors"].add(s["person_id"])
        elif s["kind"] == KIND_MERGE:
            per_repo[s["repo"] or "?"]["merges"] += s["share"]
        elif s["kind"] == KIND_PR_OPENED:
            prs_opened += s["share"]
        elif s["kind"] == KIND_PR_MERGED:
            prs_merged += s["share"]
            if s["meta"].get("hours_open") is not None:
                pr_open_hours.append(float(s["meta"]["hours_open"]))
        elif s["kind"] == KIND_PR_REVIEW:
            reviews += s["share"]
        per_person_days[s["person_id"]].add(clock.day(s["at"]))
    # Context switching from the allocation rows: projects per person-day.
    per_day_projects: dict[tuple[str, str], set] = defaultdict(set)
    for r in alloc_rows:
        per_day_projects[(r["person_id"], r["day"])].add(r["project_id"] or "?")
    switch_counts = [len(v) for v in per_day_projects.values()]
    fragmented_days = sum(1 for c in switch_counts if c >= 3)
    repos = []
    for repo, info in per_repo.items():
        repos.append({"repo": repo, "commits": round(info["commits"]), "keyed_pct": round(100.0 * info["keyed"] / info["commits"], 1) if info["commits"] else None, "merges": round(info["merges"]), "authors": len(info["authors"]), "ai_pct": round(100.0 * info["ai"] / info["commits"], 1) if info["commits"] else None})
    repos.sort(key=lambda r: -r["commits"])
    return {
        "commits": round(commits), "after_hours_pct": round(100.0 * after_hours / commits, 1) if commits else 0.0, "weekend_pct": round(100.0 * weekend / commits, 1) if commits else 0.0,
        "keyed_pct": round(100.0 * keyed / commits, 1) if commits else None, "ai_assisted_pct": round(100.0 * ai / commits, 1) if commits else 0.0, "ai_commits": round(ai),
        "heatmap": [[round(v, 1) for v in row] for row in heat],
        "context": {"avg_projects_per_day": round(sum(switch_counts) / len(switch_counts), 2) if switch_counts else 0.0, "fragmented_days_pct": round(100.0 * fragmented_days / len(switch_counts), 1) if switch_counts else 0.0, "person_days": len(switch_counts)},
        "pull_requests": {"opened": round(prs_opened), "merged": round(prs_merged), "reviews": round(reviews), "hours_open_p50": _r(percentile(pr_open_hours, 50)), "hours_open_p85": _r(percentile(pr_open_hours, 85))},
        "repos": repos[:30],
        "active_people": len(per_person_days),
        "kinds": dict(sorted(kind_counts.items(), key=lambda kv: -kv[1])),
        "sources": dict(sorted(source_counts.items(), key=lambda kv: -kv[1])),
    }


def impact_metrics(slices: list[dict], alloc_rows: list[dict], timelines: dict[str, dict], *, changes: list[dict], releases: list[dict], initiatives: dict[str, dict], projects: dict[str, dict], goals: dict[str, dict], start: float, end: float) -> dict:
    """Per initiative: effort in, outcomes out."""
    hours_by_init = {r["key"]: r for r in rollup(alloc_rows, "initiative_id")}
    commits: dict = defaultdict(float)
    tickets_touched: dict = defaultdict(set)
    tickets_done: dict = defaultdict(int)
    agent_cost: dict = defaultdict(float)
    people: dict = defaultdict(set)
    done_refs_seen: set = set()
    for s in slices:
        if s["at"] < start or s["at"] >= end or s["bot"]:
            continue
        init = s["initiative_id"]
        if s["kind"] == KIND_COMMIT:
            commits[init] += s["share"]
        if s["ref"]:
            tickets_touched[init].add(s["ref"])
            if s["kind"] == KIND_STATUS and (s["meta"].get("to_cat") == "done") and s["ref"] not in done_refs_seen:
                done_refs_seen.add(s["ref"])
                tickets_done[init] += 1
        agent_cost[init] += s.get("cost_usd", 0.0)
        if s["kind"] != "ai_turn":
            people[init].add(s["person_id"])
    shipped: dict = defaultdict(int)
    project_init = {pid: p.get("initiative_id") for pid, p in projects.items()}
    for c in changes:
        if c.get("status") != "done":
            continue
        # The tracker's own done date beats the board's shipped_at, which is stamped
        # when the status was MIRRORED - a bulk import marks thousands of old tickets
        # as shipped on import day.
        shipped_at = c.get("shipped_at")
        if c.get("tracker_id") and c.get("ticket_key"):
            tl = timelines.get(f"{c['tracker_id']}:{c['ticket_key']}")
            if tl and tl.get("last_done"):
                shipped_at = tl["last_done"]
        if shipped_at and start <= shipped_at < end:
            shipped[project_init.get(c.get("project_id"))] += 1
    rows = []
    keys = set(hours_by_init) | set(commits) | set(tickets_touched) | set(shipped)
    for key in keys:
        initiative = initiatives.get(key or "") or {}
        hours = hours_by_init.get(key, {}).get("hours", 0.0)
        done = tickets_done.get(key, 0)
        rows.append({
            "initiative_id": key, "title": initiative.get("title") or ("Unattributed" if key is None else key), "is_maintenance": bool(initiative.get("is_maintenance")), "status": initiative.get("status"), "goal_ids": initiative.get("goal_ids") or [],
            "hours": hours, "people": len(people.get(key, ())), "commits": round(commits.get(key, 0.0)), "tickets_touched": len(tickets_touched.get(key, ())), "tickets_done": done, "changes_shipped": shipped.get(key, 0),
            "agent_cost_usd": round(agent_cost.get(key, 0.0), 2),
            "done_per_100h": round(100.0 * done / hours, 1) if hours else None,
        })
    rows.sort(key=lambda r: -r["hours"])
    total_hours = sum(r["hours"] for r in rows) or 1.0
    for r in rows:
        r["hours_pct"] = round(100.0 * r["hours"] / total_hours, 1)
    # Goals: non-additive - an initiative serving two goals counts fully under both.
    by_goal: dict = defaultdict(lambda: {"hours": 0.0, "tickets_done": 0, "changes_shipped": 0, "initiatives": 0})
    for r in rows:
        for gid in r["goal_ids"] or ([None] if r["initiative_id"] else []):
            g = by_goal[gid]
            g["hours"] += r["hours"]
            g["tickets_done"] += r["tickets_done"]
            g["changes_shipped"] += r["changes_shipped"]
            g["initiatives"] += 1
    goal_rows = [{"goal_id": gid, "title": (goals.get(gid or "") or {}).get("title") or "No goal", **{k: (round(v, 1) if isinstance(v, float) else v) for k, v in vals.items()}, "share_pct": round(100.0 * vals["hours"] / total_hours, 1)} for gid, vals in by_goal.items()]
    goal_rows.sort(key=lambda r: -r["hours"])
    released = [r for r in releases if r.get("released") and r.get("release_date") and start <= _date_epoch(r["release_date"]) < end]
    return {"initiatives": rows, "goals": goal_rows, "releases": len(released), "release_names": [r["name"] for r in sorted(released, key=lambda r: r["release_date"], reverse=True)[:12]]}


def _date_epoch(day: str) -> float:
    from datetime import datetime, timezone
    try:
        y, m, d = (int(p) for p in day[:10].split("-"))
        return datetime(y, m, d, tzinfo=timezone.utc).timestamp()
    except ValueError:
        return 0.0


REALIZED = "realized"
IN_FLIGHT = "in_flight"
STRANDED = "stranded"
UNTRACEABLE = "untraceable"
PROD_BRANCH_RE = re.compile(r"^(main|master|prod|production|release|releases?/.*|release-candidate/.*|develop|development)$", re.IGNORECASE)


def weighted_percentile(pairs: list[tuple[float, float]], pct: float) -> float | None:
    """pairs of (value, weight)."""
    if not pairs:
        return None
    ordered = sorted(pairs)
    total = sum(w for _, w in ordered)
    if total <= 0:
        return None
    acc = 0.0
    for value, weight in ordered:
        acc += weight
        if acc >= total * pct / 100.0:
            return value
    return ordered[-1][0]


def realization(alloc_rows: list[dict], timelines: dict[str, dict], changes_by_ref: dict[str, dict], last_signal_by_ref: dict[str, float], *, now: float, stale_days: float, clock: Clock) -> dict:
    """What became of the hours. Each allocation row is one person-day on one piece of
    work; the piece's current fate classifies the hours - the unit is the hour, so a
    team that writes one epic and one that writes forty subtasks get the same answer.

    realized    - the ticket (or its linked change) reached done
    in_flight   - still open and touched within `stale_days`
    stranded    - open but stale, abandoned, or never planned onto a project
    untraceable - no ticket at all (unkeyed commits, agent turns): fate unknowable
    """
    per_init: dict = defaultdict(lambda: {REALIZED: 0.0, IN_FLIGHT: 0.0, STRANDED: 0.0, UNTRACEABLE: 0.0, "lead": []})
    weekly: dict[str, float] = defaultdict(float)
    totals = {REALIZED: 0.0, IN_FLIGHT: 0.0, STRANDED: 0.0, UNTRACEABLE: 0.0}
    lead_all: list[tuple[float, float]] = []
    for r in alloc_rows:
        ref = r.get("ref")
        hours = r["hours"]
        init = r.get("initiative_id")
        bucket = UNTRACEABLE
        done_at = None
        if ref:
            tl = timelines.get(ref)
            change = changes_by_ref.get(ref)
            if tl and tl.get("status_cat") == "done" and tl.get("last_done"):
                bucket, done_at = REALIZED, tl["last_done"]
            elif change is not None and change.get("status") == "done":
                bucket, done_at = REALIZED, change.get("shipped_at")
            elif tl and tl.get("status_cat") == "removed":
                bucket = STRANDED
            elif not r.get("project_id"):
                bucket = STRANDED  # real work the portfolio never planned
            else:
                last = last_signal_by_ref.get(ref)
                bucket = IN_FLIGHT if last and (now - last) / 86400.0 < stale_days else STRANDED
        per_init[init][bucket] += hours
        totals[bucket] += hours
        if bucket == REALIZED and done_at:
            days = max(0.0, (done_at - clock.day_start(r["day"])) / 86400.0)
            per_init[init]["lead"].append((days, hours))
            lead_all.append((days, hours))
            weekly[clock.week(done_at)] += hours
    rows = {}
    for init, b in per_init.items():
        total = b[REALIZED] + b[IN_FLIGHT] + b[STRANDED] + b[UNTRACEABLE]
        traceable = total - b[UNTRACEABLE]
        rows[init] = {
            "hours": round(total, 1), REALIZED: round(b[REALIZED], 1), IN_FLIGHT: round(b[IN_FLIGHT], 1), STRANDED: round(b[STRANDED], 1), UNTRACEABLE: round(b[UNTRACEABLE], 1),
            "realization_pct": round(100.0 * b[REALIZED] / traceable, 1) if traceable else None,
            "stranded_pct": round(100.0 * b[STRANDED] / traceable, 1) if traceable else None,
            "lead_days_p50": _r(weighted_percentile(b["lead"], 50)),
        }
    grand = sum(totals.values())
    traceable = grand - totals[UNTRACEABLE]
    weeks = sorted(weekly)
    return {
        "by_initiative": rows,
        "totals": {k: round(v, 1) for k, v in totals.items()},
        "realization_pct": round(100.0 * totals[REALIZED] / traceable, 1) if traceable else None,
        "stranded_pct": round(100.0 * totals[STRANDED] / traceable, 1) if traceable else None,
        "untraceable_pct": round(100.0 * totals[UNTRACEABLE] / grand, 1) if grand else None,
        "lead_days_p50": _r(weighted_percentile(lead_all, 50)),
        "lead_days_p85": _r(weighted_percentile(lead_all, 85)),
        "weekly_realized": [{"week": w, "hours": round(weekly[w], 1)} for w in weeks],
    }


def production_merges(slices: list[dict], *, start: float, end: float) -> dict:
    """Pull requests merged into a mainline branch, per initiative - a delivery event
    that does not depend on how the team slices tickets. Only sources that know the
    target branch (ADO pull requests) contribute; git merge commits name their source."""
    # A PR naming tickets in two initiatives counts half for each (its slices carry
    # equal shares), so the column sums to the number of merges and never double counts.
    out: dict = defaultdict(float)
    for s in slices:
        if s["kind"] != KIND_PR_MERGED or s["at"] < start or s["at"] >= end:
            continue
        target = str(s["meta"].get("target") or "")
        if PROD_BRANCH_RE.match(target):
            out[s["initiative_id"]] += s["share"]
    return {k: round(v, 1) for k, v in out.items()}
