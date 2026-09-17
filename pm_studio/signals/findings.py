"""Automatic problem detection over the ledger.

A finding is a rule firing on an entity, with the evidence that made it fire. Ids are
stable (rule + entity), so feedback given once sticks across refreshes, and the judge
can review the same finding a human sees.

Every threshold below is a default; `[signals.thresholds]` in config and the tuning
file (what the judge's accepted suggestions write) override it. Severity is a
statement about how much the finding distorts the picture of where effort goes -
not about how urgent the underlying ticket is.
"""

from __future__ import annotations

import hashlib
import time
from collections import defaultdict

from .attribution import VIA_CHANGE, VIA_DEFAULT_PROJECT, VIA_EPIC_PROJECT, VIA_OWN_EPIC, VIA_PARENT_CHANGE, VIA_SESSION
from .model import KIND_COMMIT, KIND_PR_MERGED, KIND_PR_REVIEW, KIND_STATUS, Clock

DEFAULT_THRESHOLDS: dict[str, float] = {
    "stale_in_progress_days": 10,
    "stale_project_days": 21,
    "silent_initiative_days": 30,
    "zombie_in_progress_days": 45,
    "work_after_done_days": 7,
    "status_lag_days": 14,
    "review_dwell_days": 5,
    "unkeyed_commit_pct": 40,          # finding when MORE than this % of a repo's commits lack a key
    "unkeyed_min_commits": 15,
    "fragmentation_projects_per_week": 6,
    "bus_factor_min_commits": 20,
    "bus_factor_pct": 90,
    "maintenance_share_pct": 50,
    "unplanned_share_pct": 25,
    "reopen_count": 2,
    "unresolved_author_min_signals": 25,
    "idle_assignee_days": 14,
    "abandoned_after_days": 180,        # stale this long -> folded into one backlog finding per project
    "unreviewed_merge_pct": 50,
    "unreviewed_min_merges": 10,
    "closure_lag_days": 14,
    "closure_lag_min_tickets": 10,
    "stranded_share_pct": 40,
    "stranded_min_hours": 100,
    "sibling_fold_min": 3,              # stale tickets under one parent fold into one finding
}

SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2, "info": 3}

RULES: dict[str, dict] = {
    "stale_in_progress": {"title": "In progress but silent", "family": "lifecycle", "what": "Tickets the tracker says are being worked on, with no signal of any kind for N days."},
    "zombie_in_progress": {"title": "In progress far too long", "family": "lifecycle", "what": "Tickets active longer than N days end to end - either huge, stuck, or a status nobody maintains."},
    "stale_project": {"title": "Open project with no activity", "family": "lifecycle", "what": "Open projects that still hold open changes but show no signal for N days."},
    "silent_initiative": {"title": "Initiative with no activity", "family": "investment", "what": "Open, non-maintenance initiatives with no signal in the window."},
    "work_after_done": {"title": "Commits after the ticket closed", "family": "hygiene", "what": "Commits naming a ticket that was already done N+ days earlier - work the board no longer sees, or a ticket closed early."},
    "status_lag": {"title": "Commits on a ticket still To Do", "family": "hygiene", "what": "Tickets with recent commits whose status never left the backlog - the board under-reports what is in flight."},
    "review_dwell": {"title": "Waiting in review / QA", "family": "flow", "what": "Tickets sitting in a review-type status longer than N days right now."},
    "reopened": {"title": "Reopened repeatedly", "family": "flow", "what": "Tickets that went done and came back N+ times in the window."},
    "unkeyed_commits": {"title": "Commits without a ticket key", "family": "hygiene", "what": "Repositories where too large a share of commits name no ticket, so their effort cannot be placed on any project."},
    "unresolved_author": {"title": "Author not in the people directory", "family": "data", "what": "Git identities with real activity that map to nobody - their hours are counted, but under an unknown person."},
    "unplanned_work": {"title": "Effort on unplanned tickets", "family": "investment", "what": "Share of effort on tickets that belong to no project. Real work that the portfolio does not know about."},
    "maintenance_share": {"title": "Maintenance dominates", "family": "investment", "what": "Maintenance initiatives absorb more than N% of placed effort in the window."},
    "fragmentation": {"title": "Person spread thin", "family": "workflow", "what": "Someone active on N+ projects in one week - context-switching cost, or attribution that is too fine."},
    "bus_factor": {"title": "Single-person project", "family": "workflow", "what": "Projects with meaningful commit volume where one person wrote almost all of it."},
    "ideation_with_commits": {"title": "Ideation project already being built", "family": "lifecycle", "what": "Projects declared in ideation that have commits - the phase should be open."},
    "idle_assignee": {"title": "Assigned in-progress ticket, assignee inactive", "family": "lifecycle", "what": "Active tickets whose assignee has produced no signal anywhere for N days."},
    "abandoned_backlog": {"title": "Abandoned in-progress backlog", "family": "lifecycle", "what": "Tickets the tracker still shows in progress that nobody has touched for more than N days, one finding per tracker project - a cleanup, not 500 alarms."},
    "unreviewed_merges": {"title": "Pull requests merged without review", "family": "workflow", "what": "Repositories where most merged pull requests carried no reviewer vote - or were merged within minutes of opening."},
    "stale_epic": {"title": "Stale work under one epic", "family": "lifecycle", "what": "Several in-progress tickets under the same parent, all silent - one dead epic, one finding, instead of one alarm per child."},
    "stranded_effort": {"title": "Effort going nowhere", "family": "investment", "what": "Initiatives where more than N% of the traceable hours went into work that is now stale, abandoned, removed or never planned - measured in hours, so it does not depend on how a team slices tickets."},
    "closure_lag": {"title": "Finished but never closed", "family": "flow", "what": "Tickets resolved / UAT-complete for more than N days without being closed, one finding per tracker project - the hygiene gap that makes WIP and cycle time read worse than they are."},
}

PLACED = (VIA_CHANGE, VIA_PARENT_CHANGE, VIA_EPIC_PROJECT, VIA_OWN_EPIC, VIA_SESSION, VIA_DEFAULT_PROJECT)


def finding_id(rule: str, entity: str) -> str:
    return hashlib.sha1(f"{rule}\x1f{entity}".encode()).hexdigest()[:12]


def effective_thresholds(*layers: dict[str, float]) -> dict[str, float]:
    out = dict(DEFAULT_THRESHOLDS)
    for layer in layers:
        for k, v in (layer or {}).items():
            if k in out:
                try:
                    out[k] = float(v)
                except (TypeError, ValueError):
                    continue
    return out


def _finding(rule: str, entity: str, severity: str, title: str, detail: str, *, evidence: list[dict] | None = None, links: dict | None = None, value: float | None = None, unit: str = "") -> dict:
    return {"id": finding_id(rule, entity), "rule": rule, "family": RULES[rule]["family"], "rule_title": RULES[rule]["title"], "entity": entity, "severity": severity, "title": title, "detail": detail, "evidence": (evidence or [])[:8], "links": links or {}, "value": value, "unit": unit}


def detect(*, slices: list[dict], alloc_rows: list[dict], timelines: dict[str, dict], tickets: dict[str, dict], changes: list[dict], projects: dict[str, dict], initiatives: dict[str, dict], resolver_suggestions: list[dict], thresholds: dict[str, float], clock: Clock, now: float, start: float, end: float, realization: dict | None = None) -> list[dict]:
    T = thresholds
    day = 86400.0
    findings: list[dict] = []
    in_window = [s for s in slices if start <= s["at"] < end and not s["bot"]]

    # ---- indexes ----
    last_signal_by_ref: dict[str, float] = {}
    last_signal_by_person: dict[str, float] = {}
    commits_by_ref: dict[str, list[dict]] = defaultdict(list)
    last_signal_by_project: dict[str, float] = {}
    last_signal_by_initiative: dict[str, float] = {}
    for s in slices:
        if s["bot"]:
            continue
        if s["ref"]:
            last_signal_by_ref[s["ref"]] = max(last_signal_by_ref.get(s["ref"], 0.0), s["at"])
            if s["kind"] == KIND_COMMIT:
                commits_by_ref[s["ref"]].append(s)
        if s["kind"] != "ai_turn":
            last_signal_by_person[s["person_id"]] = max(last_signal_by_person.get(s["person_id"], 0.0), s["at"])
        if s["project_id"]:
            last_signal_by_project[s["project_id"]] = max(last_signal_by_project.get(s["project_id"], 0.0), s["at"])
        if s["initiative_id"]:
            last_signal_by_initiative[s["initiative_id"]] = max(last_signal_by_initiative.get(s["initiative_id"], 0.0), s["at"])
    person_name = {s["person_id"]: s["person_name"] for s in slices}
    ticket_title = {ref: t.get("title", "") for ref, t in tickets.items()}
    ticket_url = {ref: t.get("url", "") for ref, t in tickets.items()}
    assignee_of = {ref: t.get("assignee", "") for ref, t in tickets.items()}

    # ---- ticket-level rules ----
    abandoned: dict[str, list[dict]] = defaultdict(list)
    stale_candidates: list[tuple[str | None, dict]] = []
    for ref, tl in timelines.items():
        ticket = tickets.get(ref) or {}
        title = ticket_title.get(ref) or ref
        cat = tl.get("status_cat") or ""
        links = {"url": ticket_url.get(ref, ""), "ref": ref}
        if cat == "in_progress":
            last = last_signal_by_ref.get(ref)
            idle = (now - last) / day if last else None
            stale = idle is not None and idle >= T["stale_in_progress_days"]
            if stale and idle >= T["abandoned_after_days"]:
                # Not an alarm each: a ticket nobody touched for half a year is backlog
                # rot, and hundreds of them are one cleanup. Grouped per tracker project.
                abandoned[ticket.get("project") or ref.split(":", 1)[0]].append({"ref": ref, "title": title[:70], "url": ticket_url.get(ref, ""), "idle": round(idle), "status": tl["status"], "who": assignee_of.get(ref, "")})
                continue
            if stale:
                # One finding per stale ticket: staleness is the root cause, so the
                # review-queue and long-running rules stay quiet for it below. Siblings
                # under one parent are folded after the loop.
                stale_candidates.append((ticket.get("parent_key") and f"{ticket['tracker_id']}:{ticket['parent_key']}", _finding("stale_in_progress", ref, "high" if idle >= 2 * T["stale_in_progress_days"] else "medium", f"{ref.split(':', 1)[1]} · {title[:80]}", f"Status {tl['status']!r}, last signal {idle:.0f} days ago" + (f", assigned to {assignee_of.get(ref)}" if assignee_of.get(ref) else "") + ".", links=links, value=round(idle, 1), unit="days idle")))
            if not stale and tl.get("first_start") and (now - tl["first_start"]) / day >= T["zombie_in_progress_days"]:
                age = (now - tl["first_start"]) / day
                findings.append(_finding("zombie_in_progress", ref, "medium", f"{ref.split(':', 1)[1]} · {title[:80]}", f"Started {age:.0f} days ago and still {tl['status']!r}, with activity in the last {idle:.0f} days." if idle is not None else f"Started {age:.0f} days ago and still {tl['status']!r}.", links=links, value=round(age, 0), unit="days active"))
            if not stale and _looks_review(tl["status"]) and tl.get("status_since") and (now - tl["status_since"]) / day >= T["review_dwell_days"]:
                wait = (now - tl["status_since"]) / day
                findings.append(_finding("review_dwell", ref, "medium" if wait < 3 * T["review_dwell_days"] else "high", f"{ref.split(':', 1)[1]} · {title[:80]}", f"In {tl['status']!r} for {wait:.0f} days.", links=links, value=round(wait, 1), unit="days waiting"))
            assignee = assignee_of.get(ref)
            if assignee:
                pid = next((p for p, n in person_name.items() if n == assignee), None)
                if pid and last_signal_by_person.get(pid) and (now - last_signal_by_person[pid]) / day >= T["idle_assignee_days"]:
                    findings.append(_finding("idle_assignee", ref, "low", f"{ref.split(':', 1)[1]} · {title[:80]}", f"Assigned to {assignee}, who has shown no activity anywhere for {(now - last_signal_by_person[pid]) / day:.0f} days.", links=links))
        if cat == "todo":
            recent = [c for c in commits_by_ref.get(ref, []) if c["at"] >= now - T["status_lag_days"] * day]
            if recent:
                findings.append(_finding("status_lag", ref, "medium", f"{ref.split(':', 1)[1]} · {title[:80]}", f"{len(recent)} commit(s) in the last {T['status_lag_days']:.0f} days by {', '.join(sorted({c['person_name'] for c in recent}))}, but the ticket is still {tl['status']!r}.", evidence=[{"at": c["at"], "who": c["person_name"], "what": c["meta"].get("subject", "")[:100], "repo": c["repo"]} for c in recent[:5]], links=links, value=len(recent), unit="commits"))
        done_at = tl.get("last_done")
        if done_at and cat == "done":
            late = [c for c in commits_by_ref.get(ref, []) if c["at"] >= done_at + T["work_after_done_days"] * day and start <= c["at"] < end]
            if late:
                findings.append(_finding("work_after_done", ref, "medium" if len(late) < 5 else "high", f"{ref.split(':', 1)[1]} · {title[:80]}", f"{len(late)} commit(s) landed {(late[-1]['at'] - done_at) / day:.0f}+ days after the ticket was done ({clock.day(done_at)}).", evidence=[{"at": c["at"], "who": c["person_name"], "what": c["meta"].get("subject", "")[:100], "repo": c["repo"]} for c in late[:5]], links=links, value=len(late), unit="commits after done"))
        if tl.get("reopens", 0) >= T["reopen_count"] and tl.get("last_done") and start <= tl["last_done"] < end:
            findings.append(_finding("reopened", ref, "low", f"{ref.split(':', 1)[1]} · {title[:80]}", f"Reopened {tl['reopens']} times.", links=links, value=tl["reopens"], unit="reopens"))

    by_parent: dict[str, list[dict]] = defaultdict(list)
    for parent_ref, f in stale_candidates:
        # Fold key: the parent when there is one, else the title stem - twelve
        # "Remove FF ..." tickets are one cleanup whether or not they share an epic.
        key = parent_ref or _title_stem(ticket_title.get(f["entity"], "")) or f["entity"]
        by_parent[key].append(f)
    for parent_ref, group in by_parent.items():
        if len(group) >= T["sibling_fold_min"] and parent_ref not in tickets and parent_ref.startswith("stem:"):
            worst = max(group, key=lambda f: f.get("value") or 0)
            findings.append(_finding("stale_epic", parent_ref, worst["severity"], f"{len(group)} silent tickets titled \u201c{parent_ref[5:]}\u2026\u201d", f"{len(group)} in-progress tickets with the same title stem are all silent; the quietest for {worst['value']:.0f} days. One decision covers them.", evidence=[{"ref": f["entity"], "title": f["title"].split(" · ", 1)[-1][:70] + f" — {f['value']:.0f}d idle"} for f in sorted(group, key=lambda f: -(f.get("value") or 0))[:8]], value=len(group), unit="stale siblings"))
        elif len(group) >= T["sibling_fold_min"] and parent_ref in tickets:
            parent = tickets[parent_ref]
            worst = max(group, key=lambda f: f.get("value") or 0)
            findings.append(_finding("stale_epic", parent_ref, worst["severity"], f"{parent_ref.split(':', 1)[1]} · {(parent.get('title') or '')[:80]}", f"{len(group)} in-progress tickets under this {parent.get('raw_type') or parent.get('type') or 'parent'} are all silent; the quietest for {worst['value']:.0f} days. Decide the epic, not the children.", evidence=[{"ref": f["entity"], "title": f["title"].split(" · ", 1)[-1][:70] + f" — {f['value']:.0f}d idle"} for f in sorted(group, key=lambda f: -(f.get("value") or 0))[:8]], links={"url": parent.get("url", ""), "ref": parent_ref}, value=len(group), unit="stale children"))
        else:
            findings.extend(group)

    awaiting: dict[str, list[dict]] = defaultdict(list)
    for ref, tl in timelines.items():
        if tl.get("status_cat") == "resolved" and tl.get("status_since") and (now - tl["status_since"]) / day >= T["closure_lag_days"]:
            ticket = tickets.get(ref) or {}
            awaiting[ticket.get("project") or ref.split(":", 1)[0]].append({"ref": ref, "title": (ticket_title.get(ref) or ref)[:70], "url": ticket_url.get(ref, ""), "age": round((now - tl["status_since"]) / day), "status": tl["status"], "who": assignee_of.get(ref, "")})
    for project_name, items in awaiting.items():
        if len(items) < T["closure_lag_min_tickets"]:
            continue
        items.sort(key=lambda i: -i["age"])
        ages = sorted(i["age"] for i in items)
        median = ages[len(ages) // 2]
        by_status: dict[str, int] = defaultdict(int)
        for i in items:
            by_status[i["status"]] += 1
        findings.append(_finding("closure_lag", project_name, "medium" if len(items) < 50 else "high", f"{len(items)} tickets finished but not closed in {project_name}", f"Median {median} days since they reached {', '.join(f'{n} {st}' for st, n in sorted(by_status.items(), key=lambda kv: -kv[1])[:3])}; oldest {items[0]['age']} days. Closing them fixes WIP, cycle time and the board's in-flight count in one move.", evidence=[{"ref": i["ref"], "title": f"{i['title']} — {i['status']}, {i['age']}d, {i['who'] or 'unassigned'}", "url": i["url"]} for i in items[:8]], links={"tracker_project": project_name}, value=len(items), unit="tickets"))

    for project_name, items in abandoned.items():
        items.sort(key=lambda i: -i["idle"])
        by_who: dict[str, int] = defaultdict(int)
        by_status: dict[str, int] = defaultdict(int)
        for i in items:
            by_who[i["who"] or "unassigned"] += 1
            by_status[i["status"]] += 1
        who = ", ".join(f"{n} {w}" for w, n in sorted(by_who.items(), key=lambda kv: -kv[1])[:5])
        statuses = ", ".join(f"{n} {st}" for st, n in sorted(by_status.items(), key=lambda kv: -kv[1])[:4])
        findings.append(_finding("abandoned_backlog", project_name, "high" if len(items) >= 20 else "medium", f"{len(items)} in-progress tickets untouched for {T['abandoned_after_days']:.0f}+ days in {project_name}", f"Oldest idle {items[0]['idle']} days. By status: {statuses}. By assignee: {who}. Close or remove them so the board stops reporting them as in flight.", evidence=[{"ref": i["ref"], "title": f"{i['title']} — {i['status']}, {i['idle']}d idle, {i['who'] or 'unassigned'}", "url": i["url"]} for i in items[:8]], links={"tracker_project": project_name}, value=len(items), unit="tickets"))

    # ---- project / initiative rules ----
    open_changes_by_project: dict[str, int] = defaultdict(int)
    for c in changes:
        if c.get("status") != "done" and c.get("project_id"):
            open_changes_by_project[c["project_id"]] += 1
    for pid, project in projects.items():
        if project.get("status") == "open" and open_changes_by_project.get(pid):
            last = last_signal_by_project.get(pid)
            idle = (now - last) / day if last else None
            if idle is None or idle >= T["stale_project_days"]:
                findings.append(_finding("stale_project", pid, "medium" if idle is not None else "low", project.get("title", pid)[:90], (f"{open_changes_by_project[pid]} open change(s); last signal {idle:.0f} days ago." if idle is not None else f"{open_changes_by_project[pid]} open change(s); no signal in the ledger at all."), links={"project_id": pid, "initiative_id": project.get("initiative_id")}, value=round(idle, 0) if idle is not None else None, unit="days idle"))
        if project.get("status") == "ideation" and last_signal_by_project.get(pid):
            commits = [s for s in in_window if s["project_id"] == pid and s["kind"] == KIND_COMMIT]
            if commits:
                findings.append(_finding("ideation_with_commits", pid, "low", project.get("title", pid)[:90], f"{len(commits)} commit(s) in the window on a project still in ideation.", links={"project_id": pid}, value=len(commits), unit="commits"))
    for iid, initiative in initiatives.items():
        if initiative.get("status") != "open" or initiative.get("is_maintenance"):
            continue
        # Only initiatives with open, non-ideation projects are expected to move.
        live = [p for p in projects.values() if p.get("initiative_id") == iid and p.get("status") == "open"]
        if not live:
            continue
        last = last_signal_by_initiative.get(iid)
        if last is None or last < end - T["silent_initiative_days"] * day:
            findings.append(_finding("silent_initiative", iid, "high", initiative.get("title", iid)[:90], f"{len(live)} open project(s), " + (f"last signal {(now - last) / day:.0f} days ago." if last else "no signal ever recorded."), links={"initiative_id": iid}, value=round((now - last) / day, 0) if last else None, unit="days silent"))

    # ---- investment mix ----
    total = sum(r["hours"] for r in alloc_rows) or 0.0
    if total > 0:
        maint = sum(r["hours"] for r in alloc_rows if r.get("maintenance"))
        unplanned = sum(r["hours"] for r in alloc_rows if not r.get("project_id"))
        if 100.0 * maint / total > T["maintenance_share_pct"]:
            findings.append(_finding("maintenance_share", "window", "medium", f"Maintenance took {100.0 * maint / total:.0f}% of hours", f"{maint:.0f} of {total:.0f} activity-weighted hours in the window went to maintenance initiatives (threshold {T['maintenance_share_pct']:.0f}%).", value=round(100.0 * maint / total, 1), unit="%"))
        if 100.0 * unplanned / total > T["unplanned_share_pct"]:
            top_refs: dict[str, float] = defaultdict(float)
            for s in in_window:
                if not s["project_id"] and s["ref"]:
                    top_refs[s["ref"]] += s["weight"]
            evidence = [{"ref": ref, "title": ticket_title.get(ref, "")[:80], "url": ticket_url.get(ref, "")} for ref, _ in sorted(top_refs.items(), key=lambda kv: -kv[1])[:6]]
            findings.append(_finding("unplanned_work", "window", "high" if 100.0 * unplanned / total > 2 * T["unplanned_share_pct"] else "medium", f"{100.0 * unplanned / total:.0f}% of hours on work outside any project", f"{unplanned:.0f} of {total:.0f} hours could not be placed on a project (unplanned tickets, unkeyed commits, unknown tickets).", evidence=evidence, value=round(100.0 * unplanned / total, 1), unit="%"))

    for iid, r in ((realization or {}).get("by_initiative") or {}).items():
        if not iid or r["hours"] < T["stranded_min_hours"] or r.get("stranded_pct") is None:
            continue
        if r["stranded_pct"] > T["stranded_share_pct"]:
            initiative = initiatives.get(iid) or {}
            findings.append(_finding("stranded_effort", iid, "high" if r["stranded_pct"] > 1.5 * T["stranded_share_pct"] else "medium", initiative.get("title", iid)[:90], f"{r['stranded']:.0f} of {r['hours'] - r['untraceable']:.0f} traceable hours ({r['stranded_pct']:.0f}%) went into work that is now stale, abandoned or unplanned; {r['realized']:.0f} h realized.", links={"initiative_id": iid}, value=round(r["stranded_pct"]), unit="% stranded"))

    # ---- hygiene per repo ----
    per_repo: dict[str, dict] = defaultdict(lambda: {"commits": 0.0, "keyed": 0.0, "people": defaultdict(float)})
    for s in in_window:
        if s["kind"] == KIND_COMMIT and s["repo"]:
            r = per_repo[s["repo"]]
            r["commits"] += s["share"]
            r["keyed"] += s["share"] if s["ref"] else 0.0
    for repo, r in per_repo.items():
        if r["commits"] >= T["unkeyed_min_commits"]:
            unkeyed_pct = 100.0 * (r["commits"] - r["keyed"]) / r["commits"]
            if unkeyed_pct > T["unkeyed_commit_pct"]:
                findings.append(_finding("unkeyed_commits", repo, "medium" if unkeyed_pct < 80 else "high", repo, f"{unkeyed_pct:.0f}% of {r['commits']:.0f} commits in the window name no ticket.", links={"repo": repo}, value=round(unkeyed_pct, 0), unit="% unkeyed"))

    reviewed_prs: set = {(s["repo"], s["meta"].get("pr")) for s in in_window if s["kind"] == KIND_PR_REVIEW}
    merges_by_repo: dict[str, dict] = defaultdict(lambda: {"merged": 0, "unreviewed": 0, "quick": 0})
    seen_prs: set = set()
    for s in in_window:
        if s["kind"] != KIND_PR_MERGED:
            continue
        key = (s["repo"], s["meta"].get("pr"))
        if key in seen_prs:
            continue
        seen_prs.add(key)
        repo = s["repo"] or s["meta"].get("repo_name") or "?"
        m = merges_by_repo[repo]
        m["merged"] += 1
        if key not in reviewed_prs:
            m["unreviewed"] += 1
        if (s["meta"].get("hours_open") or 0) < 0.25:
            m["quick"] += 1
    for repo, m in merges_by_repo.items():
        if m["merged"] >= T["unreviewed_min_merges"]:
            share = 100.0 * m["unreviewed"] / m["merged"]
            if share > T["unreviewed_merge_pct"]:
                findings.append(_finding("unreviewed_merges", repo, "medium", repo.replace("src/", ""), f"{m['unreviewed']} of {m['merged']} merged pull requests had no reviewer vote; {m['quick']} were merged within 15 minutes of opening.", links={"repo": repo}, value=round(share), unit="% unreviewed"))

    # ---- people ----
    for sug in sorted(resolver_suggestions, key=lambda x: -x["signals"])[:25]:
        if sug["signals"] >= T["unresolved_author_min_signals"]:
            findings.append(_finding("unresolved_author", sug["handle"], "low" if not sug["candidates"] else "medium", sug["name"] or sug["handle"], f"{sug['signals']} signals from {sug['handle']!r} map to nobody in the directory" + (f"; looks like {', '.join(person_name.get(c, c) for c in sug['candidates'])}." if sug["candidates"] else "."), links={"handle": sug["handle"], "candidates": sug["candidates"], "person_id": sug["person_id"]}, value=sug["signals"], unit="signals"))
    per_person_week: dict[tuple[str, str], set] = defaultdict(set)
    for r in alloc_rows:
        if r["project_id"]:
            per_person_week[(r["person_id"], r["week"])].add(r["project_id"])
    worst: dict[str, tuple[int, str]] = {}
    for (pid, week), projs in per_person_week.items():
        if len(projs) >= T["fragmentation_projects_per_week"] and len(projs) > worst.get(pid, (0, ""))[0]:
            worst[pid] = (len(projs), week)
    for pid, (count, week) in worst.items():
        findings.append(_finding("fragmentation", pid, "low", person_name.get(pid, pid), f"Active on {count} projects in {week}.", links={"person_id": pid}, value=count, unit="projects/week"))
    per_project_people: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for s in in_window:
        if s["kind"] == KIND_COMMIT and s["project_id"]:
            per_project_people[s["project_id"]][s["person_name"]] += s["share"]
    for pid, people in per_project_people.items():
        total_c = sum(people.values())
        if total_c >= T["bus_factor_min_commits"]:
            top_name, top = max(people.items(), key=lambda kv: kv[1])
            if 100.0 * top / total_c >= T["bus_factor_pct"] and len(people) >= 1:
                findings.append(_finding("bus_factor", pid, "low", (projects.get(pid) or {}).get("title", pid)[:90], f"{top_name} wrote {100.0 * top / total_c:.0f}% of {total_c:.0f} commits.", links={"project_id": pid}, value=round(100.0 * top / total_c, 0), unit="% by one person"))

    findings.sort(key=lambda f: (SEVERITY_ORDER.get(f["severity"], 9), -(f.get("value") or 0)))
    return findings


_STEM_STOP = {"the", "a", "an", "to", "for", "of", "in", "on", "and", "with", "from", "by", "at", "is", "be"}


def _title_stem(title: str) -> str:
    """The first three meaningful words of a title, lower-cased - "Remove FF release..."
    tickets share one whatever follows."""
    words = [w for w in "".join(ch if ch.isalnum() or ch.isspace() else " " for ch in (title or "").lower()).split() if w not in _STEM_STOP and not w.isdigit()]
    return "stem:" + " ".join(words[:3]) if len(words) >= 3 else ""


def _looks_review(status: str) -> bool:
    lowered = (status or "").lower()
    return any(m in lowered for m in ("review", "qa", "uat", "test", "verify"))


def overlay_feedback(findings: list[dict], feedback: dict, muted: set[str], *, now: float) -> tuple[list[dict], dict]:
    """Attach feedback/judge state; split into visible and hidden. A dismissed or
    snoozed finding, or one whose rule is muted, leaves the default list but stays
    countable - suppression is a filter, never a deletion."""
    visible, hidden = [], []
    counts = {"open": 0, "confirmed": 0, "dismissed": 0, "snoozed": 0, "muted": 0, "judged_noise": 0}
    for f in findings:
        fb = feedback.get(f["id"]) or {}
        state = fb.get("state", "open")
        if state == "snoozed" and fb.get("until") and fb["until"] < now:
            state = "open"
        f = {**f, "feedback": {k: v for k, v in fb.items() if k != "judged"}, "state": state, "judged": fb.get("judged")}
        if f["rule"] in muted:
            f["state"] = "muted"
            counts["muted"] += 1
            hidden.append(f)
            continue
        counts[state] = counts.get(state, 0) + 1
        if f["judged"] and f["judged"].get("verdict") == "noise" and state == "open":
            counts["judged_noise"] += 1
        if state in ("dismissed", "snoozed"):
            hidden.append(f)
        else:
            visible.append(f)
    return visible, {"visible": len(visible), "hidden": len(hidden), **counts}
