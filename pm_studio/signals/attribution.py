"""From a signal to the work model: ticket -> change -> project -> initiative -> goals,
and to the person who did it.

Every signal becomes one or more *slices* - one per resolved ticket reference, each
carrying an equal share - so a commit that names two tickets splits its weight
between them and nothing is counted twice. The chain used to get there is recorded
on the slice as `via`, because "how do you know this hour belongs to that project?"
is the question an auditor asks, and the answer must be on the row.

Resolution order for a ticket reference:
1. a change linked 1:1 to the ticket (roadmap.link_ticket) - the strongest claim;
2. the ticket's parent chain (up to three hops): a parent linked to a change, or an
   epic linked to a project (portfolio.link_epic);
3. the ticket itself is an epic held by a project (epic-level activity);
4. the ticket is known but planned nowhere: product via the tracker's import routes,
   project None -> "unplanned";
5. the reference names a ticket the catalog does not hold -> "unknown ticket".
A signal with no reference at all is attributed to its repository's system only.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

from .model import KIND_AI_TURN, KIND_COMMIT, KIND_MERGE, KIND_MEETING, KIND_MESSAGE, KIND_WORKLOG, Signal

VIA_CHANGE = "change"
VIA_PARENT_CHANGE = "parent-change"
VIA_EPIC_PROJECT = "epic-project"
VIA_OWN_EPIC = "own-epic"
VIA_ROUTE = "route-unplanned"
VIA_DEFAULT_PROJECT = "default-project"
VIA_UNKNOWN_TICKET = "unknown-ticket"
VIA_REPO = "repo-only"
VIA_SESSION = "session"
VIA_NONE = "none"

# Ticket type -> nature of the work. Used for the investment mix and for the
# capitalization default (defects are expensed).
TICKET_NATURE = {"bug": "defect", "story": "feature", "feature": "feature", "epic": "feature", "task": "task", "subtask": "task", "spike": "discovery", "other": "other"}

UNATTRIBUTED_PROJECT = "__unattributed__"

# Automation posing as a person, whatever the source called it. Checked at attribution
# time (not only at collection) so a cache written before a name was known is still
# judged right, and so the people directory is never asked to adopt a connector.
BOT_ACTOR_RE = re.compile(
    r"automation for jira|checklists for jira|herocoders|service accounts?|project collection|"
    r"build service|azure pipelines|wrike|\.sync@|snyk|dependabot|renovate|\[bot\]|jira outlook|atlassian assist",
    re.IGNORECASE,
)


def is_bot_actor(name: str, email: str) -> bool:
    return bool(BOT_ACTOR_RE.search(f"{name} {email}"))


@dataclass
class AttributionContext:
    """Everything the resolver needs, handed in as plain dicts so the module never
    reaches into a store and the tests never need one."""

    tickets: dict[str, dict] = field(default_factory=dict)          # "tracker:KEY" -> ticket dict
    changes_by_ref: dict[str, dict] = field(default_factory=dict)   # "tracker:KEY" -> change dict
    changes_by_id: dict[str, dict] = field(default_factory=dict)
    projects: dict[str, dict] = field(default_factory=dict)
    projects_by_ref: dict[str, str] = field(default_factory=dict)   # epic "tracker:KEY" -> project id
    initiatives: dict[str, dict] = field(default_factory=dict)
    goals: dict[str, dict] = field(default_factory=dict)
    routes: list[dict] = field(default_factory=list)                # {tracker_id, component, project, product, system}
    products_of_system: dict[str, list[str]] = field(default_factory=dict)
    systems_of_product: dict[str, list[str]] = field(default_factory=dict)
    capex_overrides: dict[str, bool] = field(default_factory=dict)  # initiative id -> capitalizable
    default_projects: dict[str, str] = field(default_factory=dict)  # "tracker:project[:type]" -> project id
    version: str = ""

    @classmethod
    def build(cls, *, tickets: list[dict], changes: list[dict], projects: list[dict], initiatives: list[dict], goals: list[dict], routes: list[dict], product_systems: dict[str, tuple[str, ...]] | dict[str, list[str]], capex_overrides: dict[str, bool] | None = None, default_projects: dict[str, str] | None = None) -> "AttributionContext":
        ctx = cls()
        for t in tickets:
            ctx.tickets[f"{t['tracker_id']}:{t['key']}"] = t
        for c in changes:
            ctx.changes_by_id[c["id"]] = c
            if c.get("tracker_id") and c.get("ticket_key"):
                ctx.changes_by_ref[f"{c['tracker_id']}:{c['ticket_key']}"] = c
        for p in projects:
            ctx.projects[p["id"]] = p
            if p.get("tracker_id") and p.get("ticket_key"):
                ctx.projects_by_ref[f"{p['tracker_id']}:{p['ticket_key']}"] = p["id"]
        for i in initiatives:
            ctx.initiatives[i["id"]] = i
        for g in goals:
            ctx.goals[g["id"]] = g
        ctx.routes = list(routes)
        for product, systems in product_systems.items():
            ctx.systems_of_product[product] = list(systems)
            for s in systems:
                ctx.products_of_system.setdefault(s, []).append(product)
        ctx.capex_overrides = dict(capex_overrides or {})
        ctx.default_projects = {k: v for k, v in (default_projects or {}).items() if v in ctx.projects}
        newest = max([float(x.get("updated_at") or 0) for x in list(projects) + list(initiatives) + list(changes)] or [0.0])
        ctx.version = f"{len(tickets)}:{len(changes)}:{len(projects)}:{newest:.0f}:{len(ctx.capex_overrides)}:{sorted(ctx.default_projects.items())}"
        return ctx


def _route_product(ctx: AttributionContext, ticket: dict) -> tuple[str | None, str | None]:
    tracker = ticket.get("tracker_id")
    components = set(ticket.get("components") or [])
    project = ticket.get("project") or ""
    fallback = (None, None)
    for route in ctx.routes:
        if route.get("tracker_id") not in (None, "", tracker):
            continue
        comp, proj = route.get("component") or "", route.get("project") or ""
        if comp and comp in components and (not proj or proj == project):
            return route.get("product"), route.get("system") or None
        if not comp and proj and proj == project and fallback == (None, None):
            fallback = (route.get("product"), route.get("system") or None)
    return fallback


def resolve_ref(ctx: AttributionContext, ref: str) -> dict:
    """The project (and everything above it) a ticket reference belongs to."""
    change = ctx.changes_by_ref.get(ref)
    if change is not None:
        return _from_change(ctx, change, VIA_CHANGE, ref)
    ticket = ctx.tickets.get(ref)
    if ticket is None:
        return {"via": VIA_UNKNOWN_TICKET, "ref": ref, "project_id": None, "product": None, "system": None, "change_id": None, "ticket": None}
    # Parent chain.
    tracker = ticket["tracker_id"]
    parent = ticket.get("parent_key")
    hops = 0
    while parent and hops < 3:
        pref = f"{tracker}:{parent}"
        pchange = ctx.changes_by_ref.get(pref)
        if pchange is not None:
            out = _from_change(ctx, pchange, VIA_PARENT_CHANGE, ref)
            out["change_id"] = None  # the activity is the child's, not the parent change's
            out["ticket"] = ticket
            return out
        if pref in ctx.projects_by_ref:
            return _from_project(ctx, ctx.projects_by_ref[pref], VIA_EPIC_PROJECT, ref, ticket)
        ptick = ctx.tickets.get(pref)
        parent = ptick.get("parent_key") if ptick else None
        hops += 1
    if ref in ctx.projects_by_ref:
        return _from_project(ctx, ctx.projects_by_ref[ref], VIA_OWN_EPIC, ref, ticket)
    if ctx.default_projects:
        project_name = ticket.get("project") or ""
        for key in (f"{tracker}:{project_name}:{ticket.get('raw_type') or ''}", f"{tracker}:{project_name}:{ticket.get('type') or ''}", f"{tracker}:{project_name}"):
            if key in ctx.default_projects:
                out = _from_project(ctx, ctx.default_projects[key], VIA_DEFAULT_PROJECT, ref, ticket)
                out["product"], out["system"] = _route_product(ctx, ticket)
                return out
    product, system = _route_product(ctx, ticket)
    return {"via": VIA_ROUTE, "ref": ref, "project_id": None, "product": product, "system": system, "change_id": None, "ticket": ticket, "initiative_id": None, "goal_ids": []}


def _from_change(ctx: AttributionContext, change: dict, via: str, ref: str) -> dict:
    out = _from_project(ctx, change.get("project_id"), via, ref, ctx.tickets.get(ref))
    out["change_id"] = change["id"]
    out["product"] = change.get("product") or out.get("product")
    out["system"] = change.get("system") or out.get("system")
    return out


def _from_project(ctx: AttributionContext, project_id: str | None, via: str, ref: str, ticket: dict | None) -> dict:
    project = ctx.projects.get(project_id or "")
    initiative_id = project.get("initiative_id") if project else None
    initiative = ctx.initiatives.get(initiative_id or "") if initiative_id else None
    return {
        "via": via, "ref": ref, "ticket": ticket,
        "project_id": project["id"] if project else None,
        "initiative_id": initiative_id if initiative else None,
        "goal_ids": list(initiative.get("goal_ids") or []) if initiative else [],
        "product": None, "system": None, "change_id": None,
    }


def capitalizable(ctx: AttributionContext, initiative_id: str | None, project_id: str | None, nature: str) -> bool:
    """Default capitalization stance: development work on a non-maintenance
    initiative's live project. Defects, maintenance, ideation and unplanned work are
    expensed. A per-initiative override (tuning) wins over the default."""
    if not initiative_id or not project_id:
        return False
    if initiative_id in ctx.capex_overrides:
        return bool(ctx.capex_overrides[initiative_id])
    initiative = ctx.initiatives.get(initiative_id) or {}
    project = ctx.projects.get(project_id) or {}
    if initiative.get("is_maintenance") or project.get("is_catch_all") or project.get("catch_all_for_initiative"):
        return False
    if project.get("status") == "ideation":
        return False
    return nature not in ("defect",)


class Attributor:
    def __init__(self, ctx: AttributionContext, resolver) -> None:
        self.ctx = ctx
        self.resolver = resolver  # IdentityResolver

    def person_for(self, signal: Signal) -> dict:
        meta = signal.meta or {}
        if meta.get("bot") or is_bot_actor(signal.actor, signal.actor_email):
            # Automation is repository/tracker motion, never anyone's effort - and never
            # an "unresolved person" for the directory to adopt.
            return {"id": "bot", "name": signal.actor or "automation", "email": signal.actor_email, "external": False, "matched_by": "bot"}
        if signal.source == "pm":
            return self.resolver.resolve(account_id=signal.actor) if signal.actor else {"id": "agent", "name": "PM Studio agent", "email": "", "external": True, "matched_by": "agent"}
        tracker = None
        key = meta.get("actor_key") or None
        if signal.source == "jira":
            tracker = "jira"
        return self.resolver.resolve(signal.actor, signal.actor_email, tracker_id=tracker, key=key)

    def slices(self, signal: Signal) -> list[dict]:
        person = self.person_for(signal)
        base = {
            "signal_id": signal.id, "at": signal.at, "source": signal.source, "kind": signal.kind,
            "person_id": person["id"], "person_name": person.get("name") or signal.actor or "unknown",
            "person_external": bool(person.get("external")), "matched_by": person.get("matched_by", ""),
            "repo": signal.repo, "bot": bool(signal.meta.get("bot")) or person.get("matched_by") == "bot", "ai": bool(signal.meta.get("ai")),
        }
        meta = signal.meta or {}
        targets: list[dict] = []
        if signal.source == "pm" and meta.get("project_id"):
            targets.append({**_from_project(self.ctx, meta.get("project_id"), VIA_SESSION, "", None), "system": signal.system})
        else:
            for ref in signal.refs:
                targets.append(resolve_ref(self.ctx, ref))
        if not targets:
            targets.append({"via": VIA_REPO if signal.repo else VIA_NONE, "ref": "", "ticket": None, "project_id": None, "initiative_id": None, "goal_ids": [], "product": None, "system": signal.system, "change_id": None})
        share = 1.0 / len(targets)
        out = []
        for target in targets:
            ticket = target.get("ticket")
            ttype = (ticket or {}).get("type") or ("epic" if target.get("via") == VIA_OWN_EPIC else "")
            nature = TICKET_NATURE.get(ttype, "other" if ttype else ("code" if signal.kind in (KIND_COMMIT, KIND_MERGE) else "other"))
            system = target.get("system") or signal.system
            product = target.get("product")
            if not product and system and len(self.ctx.products_of_system.get(system, [])) == 1:
                product = self.ctx.products_of_system[system][0]
            initiative_id = target.get("initiative_id")
            project_id = target.get("project_id")
            initiative = self.ctx.initiatives.get(initiative_id or "") or {}
            out.append({
                **base,
                "share": share,
                "weight": signal.weight * share,
                "minutes": (signal.minutes * share) if signal.minutes is not None else None,
                "ref": target.get("ref") or "",
                "ticket_key": (ticket or {}).get("key") if ticket else (target.get("ref", "").split(":", 1)[1] if ":" in (target.get("ref") or "") else None),
                "tracker_id": (ticket or {}).get("tracker_id") if ticket else (target.get("ref", "").split(":", 1)[0] if ":" in (target.get("ref") or "") else None),
                "ticket_type": ttype, "ticket_state_cat": (ticket or {}).get("state_category") or "",
                "nature": nature,
                "change_id": target.get("change_id"),
                "project_id": project_id,
                "initiative_id": initiative_id,
                "goal_ids": target.get("goal_ids") or [],
                "product": product, "system": system,
                "via": target.get("via"),
                "maintenance": bool(initiative.get("is_maintenance")) if initiative else False,
                "capex": capitalizable(self.ctx, initiative_id, project_id, nature),
                "cost_usd": float(meta.get("cost_usd") or 0.0) * share if signal.kind == KIND_AI_TURN else 0.0,
                "meta": meta,
            })
        return out


def coverage(slices: list[dict]) -> dict:
    """How much of the observed effort the model can place, by weight."""
    total = sum(s["weight"] for s in slices if not s["bot"]) or 1.0
    by_via: dict[str, float] = {}
    for s in slices:
        if s["bot"]:
            continue
        by_via[s["via"]] = by_via.get(s["via"], 0.0) + s["weight"]
    placed = sum(v for k, v in by_via.items() if k in (VIA_CHANGE, VIA_PARENT_CHANGE, VIA_EPIC_PROJECT, VIA_OWN_EPIC, VIA_SESSION, VIA_DEFAULT_PROJECT))
    persons = {s["person_id"] for s in slices if not s["bot"]}
    external = {s["person_id"] for s in slices if s["person_external"] and not s["bot"]}
    return {
        "placed_pct": round(100.0 * placed / total, 1),
        "by_via_pct": {k: round(100.0 * v / total, 1) for k, v in sorted(by_via.items(), key=lambda kv: -kv[1])},
        "people": len(persons), "unresolved_people": len(external),
        "computed_at": time.time(),
    }
