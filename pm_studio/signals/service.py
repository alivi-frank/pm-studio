"""The composition root: stores in, one report out.

`IntelligenceService` owns the ledger, the human-state files and the judge cycle, and
answers `report(window, filters)` - the single payload the Intelligence page renders.
It is constructed by server.py with callables onto the existing stores, so this
package never imports a store module and can be exercised in tests with plain
functions returning lists of dicts.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from datetime import date, timedelta
from pathlib import Path
from typing import Callable

from ..config import SignalsConfig
from . import judge as judge_mod
from .allocation import allocate, rollup, series
from .attribution import AttributionContext
from .findings import DEFAULT_THRESHOLDS, RULES, detect, effective_thresholds, overlay_feedback
from .finance import statement, to_csv, trace
from .identity import IdentityResolver
from .ledger import SignalLedger
from .metrics import flow_metrics, impact_metrics, ticket_timelines, workflow_metrics
from .model import DEFAULT_WEIGHTS, Clock
from .sources.ado import AdoHistorySource, AdoPullRequestSource
from .sources.git import GitSource, discover_repos
from .sources.inbox import InboxSource
from .sources.jira import JiraHistorySource
from .sources.pm import PMActivitySource
from .state import FeedbackStore, JudgmentLog, TuningStore

FILTER_KEYS = ("initiative_id", "project_id", "goal_id", "product", "system", "person_id", "repo", "source")


class IntelligenceService:
    def __init__(self, *, repo_root: Path, workspace_dir: Path, config: SignalsConfig, trackers: list, systems: dict, product_systems: dict, routes: list[dict], stores: dict[str, Callable], rate_for: Callable[[str], tuple[float | None, str]] | None = None, currency: str = "USD", fallback_model: str = "") -> None:
        self.repo_root = repo_root
        self.config = config
        self.data_dir = workspace_dir / "signals"
        self.clock = Clock(config.timezone)
        self.stores = stores  # tickets, changes, projects, initiatives, goals, people, accounts, releases
        self.routes = routes
        self.product_systems = {k: list(v) for k, v in product_systems.items()}
        self.systems = systems
        self.rate_for = rate_for or (lambda pid: (None, "no rate"))
        self.currency = currency
        self.fallback_model = fallback_model
        self.feedback = FeedbackStore(self.data_dir / "feedback.json")
        self.tuning = TuningStore(self.data_dir / "tuning.json")
        self.judgments = JudgmentLog(self.data_dir / "judgments.jsonl")
        self.aliases_path = self.data_dir / "aliases.json"
        self._report_cache: OrderedDict[str, dict] = OrderedDict()
        self._timelines_cache: tuple[int, dict] | None = None
        self.judge_running = False
        self.judge_error: str | None = None
        self._judge_lock = threading.Lock()
        self.ledger = SignalLedger(self.data_dir, since=config.since, sources=self._build_sources(trackers), context_builder=self.context)
        self._resolver_cache: tuple[str, IdentityResolver] | None = None

    # ---- wiring ----

    def weights(self) -> dict[str, float]:
        return {**DEFAULT_WEIGHTS, **self.config.weights, **self.tuning.weights()}

    def thresholds(self) -> dict[str, float]:
        return effective_thresholds(self.config.thresholds, self.tuning.thresholds())

    def _build_sources(self, trackers: list) -> list:
        weights = self.weights()
        jira_projects: set[str] = set()
        ado_enabled = False
        sources: list = []
        system_paths = {sid: getattr(spec, "path", "") or "" for sid, spec in self.systems.items()}
        for t in trackers:
            if t.provider == "jira":
                jira_projects.update(t.projects)
                sources.append(JiraHistorySource(t.id, t.base_url, tuple(t.projects), t.username, t.token, weights=weights))
            elif t.provider == "ado":
                ado_enabled = True
                sources.append(AdoHistorySource(t.id, t.base_url, tuple(t.projects), t.token, weights=weights))
                if self.config.ado_pr_projects:
                    sources.append(AdoPullRequestSource(t.id, t.base_url, tuple(self.config.ado_pr_projects), t.token, repo_resolver=self._repo_resolver(system_paths), weights=weights))
        git = GitSource(self.repo_root, system_paths, self.config.extra_repos, jira_projects=jira_projects or None, ado_enabled=ado_enabled, weights=weights)
        sources.insert(0, git)
        sources.append(PMActivitySource(self.data_dir.parent, weights=weights))
        sources.append(InboxSource(self.data_dir / "inbox", jira_projects=jira_projects or None, ado_enabled=ado_enabled, weights=weights))
        return sources

    def _repo_resolver(self, system_paths: dict[str, str]):
        def resolve(name: str) -> tuple[str | None, str | None]:
            if not name:
                return None, None
            wanted = name.lower().replace(" ", "-")
            for sid, path in system_paths.items():
                if not path:
                    continue
                base = self.repo_root / path
                if base.name.lower() == wanted or base.name.lower() == name.lower():
                    return path, sid
                for rel in discover_repos(self.repo_root, [path]):
                    leaf = rel.rsplit("/", 1)[-1].lower()
                    if leaf == name.lower() or leaf == wanted:
                        return rel, sid
            return None, None
        return resolve

    def context(self) -> tuple[AttributionContext, IdentityResolver]:
        ctx = AttributionContext.build(
            tickets=self.stores["tickets"](), changes=self.stores["changes"](), projects=self.stores["projects"](), initiatives=self.stores["initiatives"](), goals=self.stores["goals"](),
            routes=self.routes, product_systems=self.product_systems, capex_overrides=self.tuning.capex_overrides(),
        )
        people = self.stores["people"]()
        key = f"{len(people)}:{max([float(p.get('updated_at') or 0) for p in people] or [0]):.0f}:{self.aliases_path.stat().st_mtime if self.aliases_path.is_file() else 0}"
        if self._resolver_cache and self._resolver_cache[0] == key:
            resolver = self._resolver_cache[1]
        else:
            resolver = IdentityResolver(people, aliases_path=self.aliases_path, accounts=self.stores.get("accounts", lambda: [])())
            self._resolver_cache = (key, resolver)
        return ctx, resolver

    # ---- refresh & background ----

    def refresh(self, source_ids: list[str] | None = None) -> dict:
        result = self.ledger.refresh(source_ids)
        self._report_cache.clear()
        self._timelines_cache = None
        if not result.get("running") and self.config.auto_judge:
            self.start_judge()
        return result

    def start_refresh(self, source_ids: list[str] | None = None) -> bool:
        if self.ledger.is_refreshing:
            return False
        threading.Thread(target=self.refresh, args=(source_ids,), daemon=True).start()
        return True

    def background_loop(self) -> None:
        interval = self.config.auto_refresh_minutes * 60.0
        if interval <= 0:
            return
        time.sleep(20)
        while True:
            try:
                last = self.ledger.last_refresh_at or 0.0
                if time.time() - last >= interval and not self.ledger.is_refreshing:
                    self.refresh()
            except Exception as exc:  # noqa: BLE001
                print(f"[signals] refresh loop error: {exc}")
            time.sleep(60)

    # ---- windows & filters ----

    def window(self, from_day: str | None, to_day: str | None, *, now: float | None = None) -> dict:
        now = now or time.time()
        today = date.fromisoformat(self.clock.today(now))
        try:
            end_day = date.fromisoformat(to_day) if to_day else today
        except ValueError:
            end_day = today
        try:
            start_day = date.fromisoformat(from_day) if from_day else end_day - timedelta(days=89)
        except ValueError:
            start_day = end_day - timedelta(days=89)
        if start_day > end_day:
            start_day, end_day = end_day, start_day
        start = self.clock.day_start(start_day.isoformat())
        end = self.clock.day_end(end_day.isoformat())
        return {"from": start_day.isoformat(), "to": end_day.isoformat(), "start": start, "end": end, "days": (end_day - start_day).days + 1}

    @staticmethod
    def _matches(slice_: dict, filters: dict) -> bool:
        for key, value in filters.items():
            if not value:
                continue
            if key == "goal_id":
                if value not in (slice_.get("goal_ids") or []):
                    return False
            elif slice_.get(key) != value:
                return False
        return True

    def timelines(self, now: float) -> dict:
        gen = self.ledger.generation
        if self._timelines_cache and self._timelines_cache[0] == gen:
            return self._timelines_cache[1]
        tl = ticket_timelines(self.ledger.issue_facts(), now=now)
        self._timelines_cache = (gen, tl)
        return tl

    # ---- the report ----

    def allocation_mode(self, requested: str | None = None) -> str:
        return requested if requested in ("observed", "scaled") else (self.config.allocation_mode if self.config.allocation_mode in ("observed", "scaled") else "observed")

    def report(self, from_day: str | None = None, to_day: str | None = None, filters: dict | None = None, *, now: float | None = None, include_rows: bool = True, mode: str | None = None) -> dict:
        now = now or time.time()
        window = self.window(from_day, to_day, now=now)
        filters = {k: (filters or {}).get(k) or "" for k in FILTER_KEYS}
        mode = self.allocation_mode(mode)
        key = f"{window['from']}|{window['to']}|{sorted(filters.items())}|{mode}|{self.ledger.generation}|{len(self.tuning.data['history'])}|{len(self.feedback.data)}|{int(now // 300)}"
        if key in self._report_cache:
            cached = self._report_cache[key]
            # Live state rides on top of the cached figures: whether the judge or a
            # refresh is running changes by the second, the report does not.
            cached["judge"]["running"] = self.judge_running
            cached["judge"]["error"] = self.judge_error
            cached["refreshing"] = self.ledger.is_refreshing
            return cached
        slices_all, cov_all, resolver = self.ledger.slices()
        ctx, _ = self.context()
        slices = [s for s in slices_all if self._matches(s, filters)] if any(filters.values()) else slices_all
        in_window = [s for s in slices if window["start"] <= s["at"] < window["end"]]
        alloc = allocate(in_window, self.clock, capacity_hours=self.config.capacity_hours_per_day, mode=mode)
        rows = alloc["rows"]
        timelines = self.timelines(now)
        if any(filters.values()):
            refs = {s["ref"] for s in slices if s["ref"]}
            timelines = {k: v for k, v in timelines.items() if k in refs}
        tickets = ctx.tickets
        changes = list(ctx.changes_by_id.values())
        projects, initiatives, goals = ctx.projects, ctx.initiatives, ctx.goals
        flow = flow_metrics(timelines, in_window, start=window["start"], end=window["end"], clock=self.clock)
        workflow = workflow_metrics(in_window, rows, start=window["start"], end=window["end"], clock=self.clock, repo_facts=self.ledger.facts("git").get("repos") or {})
        impact = impact_metrics(in_window, rows, timelines, changes=changes, releases=self.stores.get("releases", lambda: [])(), initiatives=initiatives, projects=projects, goals=goals, start=window["start"], end=window["end"])
        thresholds = self.thresholds()
        raw_findings = detect(slices=slices, alloc_rows=rows, timelines=timelines, tickets=tickets, changes=changes, projects=projects if not filters.get("initiative_id") else {k: v for k, v in projects.items() if v.get("initiative_id") == filters["initiative_id"]}, initiatives=initiatives if not filters.get("initiative_id") else {k: v for k, v in initiatives.items() if k == filters["initiative_id"]}, resolver_suggestions=resolver.suggestions(), thresholds=thresholds, clock=self.clock, now=now, start=window["start"], end=window["end"])
        visible, counts = overlay_feedback(raw_findings, self.feedback.all(), self.tuning.muted_rules(), now=now)
        finance = statement(rows, projects=projects, initiatives=initiatives, rate_for=self.rate_for, currency=self.currency)
        cov = _coverage_for(in_window)
        by_initiative = _titled(rollup(rows, "initiative_id"), initiatives, "Unattributed")
        for row in by_initiative:
            init = initiatives.get(row["key"] or "") or {}
            row["maintenance"] = bool(init.get("is_maintenance"))
            row["status"] = init.get("status")
            row["capex_hours"] = round(sum(r["hours"] for r in rows if r["initiative_id"] == row["key"] and r["capex"]), 1)
        by_project = _titled(rollup(rows, "project_id"), projects, "Unattributed")
        for row in by_project:
            row["initiative_id"] = (projects.get(row["key"] or "") or {}).get("initiative_id")
            row["status"] = (projects.get(row["key"] or "") or {}).get("status")
        by_person = rollup(rows, "person_id")
        names = {r["person_id"]: r["person_name"] for r in rows}
        for row in by_person:
            row["title"] = names.get(row["key"], row["key"])
            row["external"] = any(r["person_external"] for r in rows if r["person_id"] == row["key"])
            row["projects"] = len({r["project_id"] for r in rows if r["person_id"] == row["key"]})
        by_goal = _titled(rollup(rows, "goal_ids"), goals, "No goal")
        period = "week" if window["days"] <= 200 else "month"
        weekly = series(rows, period, "initiative_id", top=7)
        for s in weekly["series"]:
            s["title"] = "Other" if s["key"] == "__other__" else ((initiatives.get(s["key"] or "") or {}).get("title") or ("Unattributed" if s["key"] is None else s["key"]))
        weekly_nature = series(rows, period, "nature", top=6)
        for s in weekly_nature["series"]:
            s["title"] = s["key"] or "other"
        total_hours = round(sum(r["hours"] for r in rows), 1)
        ai_cost = round(sum(s.get("cost_usd", 0.0) for s in in_window), 2)
        kpis = {
            "hours": total_hours, "active_people": len({r["person_id"] for r in rows}), "active_days": alloc["active_days"],
            "commits": workflow["commits"], "tickets_touched": len({s["ref"] for s in in_window if s["ref"]}), "tickets_done": flow["finished"],
            "changes_shipped": sum(r["changes_shipped"] for r in impact["initiatives"]), "releases": impact["releases"],
            "placed_pct": cov["placed_pct"], "capex_pct": finance["capex_pct"], "logged_pct": finance["logged_pct"],
            "findings_high": sum(1 for f in visible if f["severity"] == "high"), "findings_total": len(visible),
            "ai_cost_usd": ai_cost, "ai_commits_pct": workflow["ai_assisted_pct"], "signals": len(in_window),
            "cycle_p50": flow["cycle_days"]["p50"], "maintenance_pct": round(100.0 * sum(r["hours"] for r in rows if r["maintenance"]) / total_hours, 1) if total_hours else 0.0,
        }
        latest = self.judgments.latest()
        history = [{"at": j.get("at"), "scores": j.get("scores"), "kind": j.get("kind")} for j in self.judgments.all() if j.get("scores")][-24:]
        report = {
            "generated_at": now, "window": window, "filters": {k: v for k, v in filters.items() if v}, "period": period, "allocation_mode": mode,
            "kpis": kpis, "coverage": cov, "coverage_all_time": cov_all,
            "allocation": {"by_initiative": by_initiative, "by_project": by_project[:40], "by_person": by_person, "by_goal": by_goal, "by_product": rollup(rows, "product"), "by_system": rollup(rows, "system"), "by_nature": rollup(rows, "nature"), "by_capex": rollup(rows, "capex"), "by_via": rollup(rows, "via"), "series": weekly, "series_nature": weekly_nature},
            "impact": impact, "flow": flow, "workflow": workflow,
            "findings": visible, "finding_counts": counts, "hidden_findings": [f for f in raw_findings if f["id"] not in {v["id"] for v in visible}][:100],
            "rules": [{"rule": k, **v, "muted": k in self.tuning.muted_rules(), "count": sum(1 for f in visible if f["rule"] == k)} for k, v in RULES.items()],
            "thresholds": thresholds, "threshold_defaults": DEFAULT_THRESHOLDS, "weights": self.weights(),
            "finance": finance if include_rows else {k: v for k, v in finance.items() if k != "rows"},
            "judge": {"latest": latest, "history": history, "running": self.judge_running, "error": self.judge_error, "self_assessment": None},
            "identity": {"suggestions": resolver.suggestions()[:40], "unresolved": len(resolver.unresolved)},
            "sources": self.ledger.describe(), "refreshing": self.ledger.is_refreshing, "last_refresh_at": self.ledger.last_refresh_at,
            "lookup": {"initiatives": {k: {"title": v.get("title"), "is_maintenance": v.get("is_maintenance"), "status": v.get("status"), "goal_ids": v.get("goal_ids")} for k, v in initiatives.items()}, "projects": {k: {"title": v.get("title"), "initiative_id": v.get("initiative_id"), "status": v.get("status")} for k, v in projects.items()}, "goals": {k: v.get("title") for k, v in goals.items()}, "people": names, "products": self.stores.get("product_labels", lambda: {})(), "systems": {k: getattr(v, "label", k) for k, v in self.systems.items()}},
            "tuning": self.tuning.snapshot(), "config": {"since": self.config.since, "capacity_hours_per_day": self.config.capacity_hours_per_day, "allocation_mode": self.config.allocation_mode, "timezone": self.config.timezone, "auto_refresh_minutes": self.config.auto_refresh_minutes, "auto_judge": self.config.auto_judge},
        }
        report["judge"]["self_assessment"] = judge_mod.self_assessment(judge_mod.build_dossier(report, feedback=self.feedback.all(), previous=latest, thresholds=thresholds))
        self._report_cache[key] = report
        while len(self._report_cache) > 6:
            self._report_cache.popitem(last=False)
        return report

    def finance_csv(self, from_day: str | None, to_day: str | None, filters: dict | None = None, mode: str | None = None) -> str:
        report = self.report(from_day, to_day, filters, mode=mode)
        return to_csv(report["finance"], period_label=f"{report['window']['from']}..{report['window']['to']}")

    def trace(self, from_day: str | None, to_day: str | None, *, person_id: str, project_id: str | None, filters: dict | None = None, mode: str | None = None) -> dict:
        report = self.report(from_day, to_day, filters, mode=mode)
        window = report["window"]
        slices_all, _, _ = self.ledger.slices()
        slices = [s for s in slices_all if window["start"] <= s["at"] < window["end"] and self._matches(s, {k: (filters or {}).get(k) or "" for k in FILTER_KEYS})]
        rows = allocate(slices, self.clock, capacity_hours=self.config.capacity_hours_per_day, mode=report["allocation_mode"])["rows"]
        return trace(rows, self.ledger.signals_by_id, person_id=person_id, project_id=project_id)

    def entity_signals(self, from_day: str | None, to_day: str | None, *, ref: str | None = None, project_id: str | None = None, limit: int = 200) -> list[dict]:
        window = self.window(from_day, to_day)
        slices_all, _, _ = self.ledger.slices()
        out = [s for s in slices_all if window["start"] <= s["at"] < window["end"] and ((ref and s["ref"] == ref) or (project_id and s["project_id"] == project_id))]
        out.sort(key=lambda s: -s["at"])
        return [{k: v for k, v in s.items() if k != "meta"} | {"meta": {mk: mv for mk, mv in s["meta"].items() if mk in ("subject", "from", "to", "field", "chars", "title", "activity", "pr", "repo_name")}} for s in out[:limit]]

    # ---- feedback cycle ----

    def give_feedback(self, finding_id: str, *, state: str, by: str, note: str = "", snooze_days: float | None = None) -> dict:
        until = time.time() + snooze_days * 86400 if state == "snoozed" and snooze_days else None
        entry = self.feedback.set(finding_id, state=state, by=by, note=note, until=until)
        self._report_cache.clear()
        return entry

    def tune(self, kind: str, key: str, value, *, by: str, reason: str = "") -> dict:
        if kind == "thresholds" and key not in DEFAULT_THRESHOLDS:
            raise ValueError(f"unknown threshold {key!r}")
        if kind == "weights" and key not in DEFAULT_WEIGHTS:
            raise ValueError(f"unknown signal kind {key!r}")
        if kind not in ("thresholds", "weights", "capex_overrides", "muted_rules"):
            raise ValueError(f"unknown tuning kind {kind!r}")
        if kind == "muted_rules" and key not in RULES:
            raise ValueError(f"unknown rule {key!r}")
        self.tuning.set(kind, key, value, by=by, reason=reason)
        self._report_cache.clear()
        if kind == "weights":
            # Weights are stamped on signals at collection time; a refresh re-stamps.
            self.ledger.sources = {s.id: s for s in self._build_sources(self._trackers_snapshot())}
        return self.tuning.snapshot()

    def _trackers_snapshot(self) -> list:
        return self.stores.get("trackers", lambda: [])()

    def add_alias(self, handle: str, person_id: str, *, by: str) -> None:
        _, resolver = self.context()
        resolver.save_alias(handle, person_id)
        self._resolver_cache = None
        self.ledger._slices_cache = None
        self._report_cache.clear()
        self.tuning.data["history"].append({"at": time.time(), "kind": "alias", "key": handle, "value": person_id, "by": by, "reason": ""})

    def start_judge(self, *, by: str = "system") -> bool:
        with self._judge_lock:
            if self.judge_running:
                return False
            self.judge_running = True
            self.judge_error = None
        threading.Thread(target=self._run_judge, args=(by,), daemon=True).start()
        return True

    def _run_judge(self, by: str) -> None:
        try:
            report = self.report(include_rows=False)
            thresholds = self.thresholds()
            previous = self.judgments.latest()
            dossier = judge_mod.build_dossier(report, feedback=self.feedback.all(), previous=previous, thresholds=thresholds)
            model = judge_mod.judge_model(self.config.judge_model, self.fallback_model)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            judgment = judge_mod.run_judge(dossier, dossier_path=self.data_dir / "judge" / f"dossier-{stamp}.json", repo_root=self.repo_root, model=model)
            judgment["at"] = time.time()
            judgment["window"] = report["window"]
            judgment["requested_by"] = by
            judgment["self_assessment"] = report["judge"]["self_assessment"]["scores"]
            self.judgments.append(judgment)
            for review in judgment.get("findings_review") or []:
                self.feedback.set_judged(review["id"], review["verdict"], review["reason"], judgment["at"])
            if judgment.get("verdict") != "ok":
                self.judge_error = judgment.get("reason")
        except Exception as exc:  # noqa: BLE001
            self.judge_error = str(exc)[:400]
        finally:
            self.judge_running = False
            self._report_cache.clear()

    def apply_suggestion(self, index: int, *, by: str) -> dict:
        latest = self.judgments.latest() or {}
        suggestions = latest.get("threshold_suggestions") or []
        if index < 0 or index >= len(suggestions):
            raise ValueError("no such suggestion")
        s = suggestions[index]
        self.tune("thresholds", s["key"], s["suggested"], by=by, reason=f"judge: {s.get('reason', '')}")
        s["applied_at"] = time.time()
        # Rewrite the log entry so the UI shows it as applied.
        rows = self.judgments.all()
        if rows:
            rows[-1] = latest
            self.judgments.path.write_text("".join(__import__("json").dumps(r) + "\n" for r in rows))
        return self.tuning.snapshot()


def _titled(rows: list[dict], lookup: dict, none_title: str) -> list[dict]:
    for row in rows:
        entry = lookup.get(row["key"] or "") or {}
        row["title"] = entry.get("title") or (none_title if row["key"] is None else row["key"])
    return rows


def _coverage_for(slices: list[dict]) -> dict:
    from .attribution import coverage
    return coverage(slices)
