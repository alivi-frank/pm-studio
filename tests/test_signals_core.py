"""Tests for attribution, allocation, findings, finance and the judge's parsing - the
deterministic core. Signals and context are handed in as plain records."""

import unittest
from pathlib import Path
import tempfile

from pm_studio.signals.allocation import allocate, rollup, series
from pm_studio.signals.attribution import AttributionContext, Attributor, VIA_CHANGE, VIA_PARENT_CHANGE, VIA_REPO, VIA_ROUTE, VIA_UNKNOWN_TICKET, coverage, resolve_ref
from pm_studio.signals.findings import DEFAULT_THRESHOLDS, detect, effective_thresholds, finding_id, overlay_feedback
from pm_studio.signals.finance import statement, to_csv
from pm_studio.signals.identity import IdentityResolver
from pm_studio.signals.judge import build_dossier, parse_judgment, self_assessment
from pm_studio.signals.metrics import flow_metrics, ticket_timelines
from pm_studio.signals.model import KIND_COMMIT, KIND_STATUS, KIND_WORKLOG, Clock, Signal
from pm_studio.signals.state import FeedbackStore, TuningStore

DAY = 86400.0
T0 = 1_767_225_600.0  # 2026-01-01T00:00Z
CLOCK = Clock("UTC")


def ctx():
    return AttributionContext.build(
        tickets=[
            {"tracker_id": "jira", "key": "NDT-1", "type": "story", "state_category": "In Progress", "parent_key": "NDT-E", "components": [], "project": "NDT", "title": "story one", "url": "u1"},
            {"tracker_id": "jira", "key": "NDT-2", "type": "bug", "state_category": "To Do", "parent_key": None, "components": ["Epic Ride Portal"], "project": "NDT", "title": "bug two", "url": "u2"},
            {"tracker_id": "jira", "key": "NDT-3", "type": "task", "state_category": "In Progress", "parent_key": "NDT-E", "components": [], "project": "NDT", "title": "task three", "url": "u3"},
            {"tracker_id": "jira", "key": "NDT-E", "type": "epic", "state_category": "In Progress", "parent_key": None, "components": [], "project": "NDT", "title": "epic", "url": "ue"},
        ],
        changes=[{"id": "c1", "product": "nemt", "system": "epicride", "project_id": "p1", "tracker_id": "jira", "ticket_key": "NDT-1", "status": "in_progress", "shipped_at": None, "updated_at": T0}],
        projects=[{"id": "p1", "title": "Project One", "initiative_id": "i1", "status": "open", "updated_at": T0}, {"id": "pm", "title": "Catch-all", "initiative_id": "im", "status": "open", "is_catch_all": True, "updated_at": T0}],
        initiatives=[{"id": "i1", "title": "Init One", "goal_ids": ["g1", "g2"], "status": "open", "is_maintenance": False, "updated_at": T0}, {"id": "im", "title": "Maintenance", "goal_ids": ["g1"], "status": "open", "is_maintenance": True, "updated_at": T0}],
        goals=[{"id": "g1", "title": "Goal 1"}, {"id": "g2", "title": "Goal 2"}],
        routes=[{"tracker_id": "jira", "component": "Epic Ride Portal", "project": "", "product": "nemt", "system": "epicride"}],
        product_systems={"nemt": ("epicride",), "pdm": ("pdm-stack",)},
    )


def resolver():
    return IdentityResolver([
        {"id": "ada", "name": "Ada Lovelace", "email": "ada@x.com", "identities": [{"tracker_id": "jira", "key": "acc-ada", "display": "Ada Lovelace", "email": ""}]},
        {"id": "bob", "name": "Bob De la Cruz", "email": "bob@x.com", "identities": []},
    ])


def commit(sha, at, who, email, refs, repo="src/epicride/backend", system="epicride", weight=45.0, **meta):
    return Signal(id=sha, at=at, source="git", kind=KIND_COMMIT, actor=who, actor_email=email, refs=refs, repo=repo, system=system, weight=weight, meta={"subject": sha, "hour_local": 10, "weekday_local": 1, **meta})


class ResolveRefTest(unittest.TestCase):
    def test_chain(self) -> None:
        c = ctx()
        direct = resolve_ref(c, "jira:NDT-1")
        self.assertEqual((direct["via"], direct["project_id"], direct["initiative_id"], direct["goal_ids"], direct["change_id"]), (VIA_CHANGE, "p1", "i1", ["g1", "g2"], "c1"))
        # NDT-3's parent NDT-E is not linked; NDT-E is not a project epic -> route by component fails -> unplanned
        via_parent = resolve_ref(c, "jira:NDT-3")
        self.assertEqual(via_parent["via"], VIA_ROUTE)
        self.assertIsNone(via_parent["project_id"])
        routed = resolve_ref(c, "jira:NDT-2")
        self.assertEqual((routed["via"], routed["product"], routed["system"]), (VIA_ROUTE, "nemt", "epicride"))
        self.assertEqual(resolve_ref(c, "jira:NDT-404")["via"], VIA_UNKNOWN_TICKET)

    def test_parent_change_and_epic_project(self) -> None:
        c = ctx()
        c.changes_by_ref["jira:NDT-E"] = {"id": "ce", "product": "nemt", "system": None, "project_id": "p1"}
        out = resolve_ref(c, "jira:NDT-3")
        self.assertEqual((out["via"], out["project_id"], out["change_id"]), (VIA_PARENT_CHANGE, "p1", None))
        del c.changes_by_ref["jira:NDT-E"]
        c.projects_by_ref["jira:NDT-E"] = "p1"
        out = resolve_ref(c, "jira:NDT-3")
        self.assertEqual((out["via"], out["project_id"]), ("epic-project", "p1"))


class DefaultProjectTest(unittest.TestCase):
    def test_declared_home_for_unplanned_type(self) -> None:
        c = ctx()
        c.tickets["jira:NDT-2"]["raw_type"] = "Bug"
        self.assertEqual(resolve_ref(c, "jira:NDT-2")["via"], VIA_ROUTE)
        c2 = AttributionContext.build(tickets=list(c.tickets.values()), changes=list(c.changes_by_id.values()), projects=list(c.projects.values()), initiatives=list(c.initiatives.values()), goals=list(c.goals.values()), routes=c.routes, product_systems={"nemt": ("epicride",)}, default_projects={"jira:NDT:Bug": "pm", "jira:NOPE": "p1", "jira:NDT:Story": "missing"})
        out = resolve_ref(c2, "jira:NDT-2")
        self.assertEqual((out["via"], out["project_id"], out["initiative_id"], out["product"]), ("default-project", "pm", "im", "nemt"))
        self.assertEqual(resolve_ref(c2, "jira:NDT-3")["via"], VIA_ROUTE)  # task: no declaration, unknown target dropped


class DefaultRepoTest(unittest.TestCase):
    def test_unkeyed_commits_land_on_the_declared_repo_home(self) -> None:
        c = AttributionContext.build(tickets=[], changes=[], projects=[{"id": "p9", "title": "Data platform", "initiative_id": None, "status": "open"}], initiatives=[], goals=[], routes=[], product_systems={}, default_repos={"src/dataflow": "p9", "nemtos": "p9"})
        a = Attributor(c, resolver())
        by_path = a.slices(commit("a", T0, "Ada", "ada@x.com", [], repo="src/dataflow/sub", system="dataflow"))[0]
        by_system = a.slices(commit("b", T0, "Ada", "ada@x.com", [], repo="src/nemtos", system="nemtos"))[0]
        other = a.slices(commit("c", T0, "Ada", "ada@x.com", [], repo="src/pdm", system="pdm-stack"))[0]
        self.assertEqual((by_path["via"], by_path["project_id"]), ("default-project", "p9"))
        self.assertEqual((by_system["via"], by_system["project_id"]), ("default-project", "p9"))
        self.assertEqual((other["via"], other["project_id"]), (VIA_REPO, None))


class SuggestionSafetyTest(unittest.TestCase):
    def test_first_name_alone_is_never_a_candidate(self) -> None:
        r = IdentityResolver([{"id": "ruque", "name": "Jorge Ruque", "email": "jorge.ruque@x.com", "identities": []}, {"id": "gonz", "name": "Jorge Gonzalez Perez", "email": "", "identities": []}])
        r.resolve("Jorge Luis Piña González", "jorge.gonzalez@x.com")
        sug = r.suggestions()[0]
        self.assertNotIn("ruque", sug["candidates"])
        self.assertIn("gonz", sug["candidates"])  # surname shared through the email local part


class AttributorTest(unittest.TestCase):
    def test_slices_split_evenly_and_carry_people(self) -> None:
        a = Attributor(ctx(), resolver())
        two = a.slices(commit("s1", T0, "Ada Lovelace", "ada@x.com", ["jira:NDT-1", "jira:NDT-2"]))
        self.assertEqual(len(two), 2)
        self.assertAlmostEqual(sum(s["weight"] for s in two), 45.0)
        self.assertEqual(two[0]["person_id"], "ada")
        self.assertEqual(two[0]["matched_by"], "email")
        self.assertTrue(two[0]["capex"])           # story on a live non-maintenance project
        self.assertFalse(two[1]["capex"])          # unplanned bug
        self.assertEqual(two[1]["nature"], "defect")
        none = a.slices(commit("s2", T0, "Bob de la Cruz Rojas", "bob@personal.example", []))
        self.assertEqual(none[0]["via"], VIA_REPO)
        self.assertEqual(none[0]["person_id"], "bob")   # fuzzy name match
        self.assertEqual(none[0]["product"], "nemt")    # single product on the repo's system
        stranger = a.slices(commit("s3", T0, "Zed", "zed@nowhere", ["jira:NDT-1"]))
        self.assertTrue(stranger[0]["person_external"])
        self.assertEqual(a.resolver.suggestions()[0]["handle"], "zed@nowhere")

    def test_coverage_counts_placed_weight(self) -> None:
        a = Attributor(ctx(), resolver())
        slices = a.slices(commit("s1", T0, "Ada", "ada@x.com", ["jira:NDT-1"])) + a.slices(commit("s2", T0, "Ada", "ada@x.com", []))
        cov = coverage(slices)
        self.assertEqual(cov["placed_pct"], 50.0)
        self.assertEqual(cov["people"], 1)


class AllocationTest(unittest.TestCase):
    def slices(self):
        a = Attributor(ctx(), resolver())
        out = []
        out += a.slices(commit("s1", T0 + 9 * 3600, "Ada", "ada@x.com", ["jira:NDT-1"]))            # placed, weight 45
        out += a.slices(commit("s2", T0 + 10 * 3600, "Ada", "ada@x.com", [], weight=45.0))          # repo-only, weight 45
        out += a.slices(Signal(id="w1", at=T0 + 11 * 3600, source="jira", kind=KIND_WORKLOG, actor="Ada", actor_email="ada@x.com", refs=["jira:NDT-1"], weight=0.0, minutes=120.0, meta={}))
        out += a.slices(commit("s3", T0 + DAY + 9 * 3600, "Bob", "bob@x.com", ["jira:NDT-1"]))
        return out

    def test_days_reconcile_to_capacity(self) -> None:
        result = allocate(self.slices(), CLOCK, capacity_hours=8.0, worklog_trust={"jira": "full"})
        rows = result["rows"]
        ada_day1 = [r for r in rows if r["person_id"] == "ada" and r["day"] == "2026-01-01"]
        self.assertAlmostEqual(sum(r["hours"] for r in ada_day1), 8.0)
        placed = next(r for r in ada_day1 if r["project_id"] == "p1")
        self.assertEqual((placed["method"], placed["logged_hours"], placed["inferred_hours"], placed["hours"]), ("mixed", 2.0, 3.0, 5.0))
        unplaced = next(r for r in ada_day1 if r["project_id"] is None)
        self.assertEqual((unplaced["method"], unplaced["hours"]), ("inferred", 3.0))  # 6h remaining split 45/45
        bob = [r for r in rows if r["person_id"] == "bob"]
        self.assertEqual(bob[0]["hours"], 8.0)
        self.assertEqual(result["active_days"], 2)

    def test_worklogs_are_evidence_unless_trusted(self) -> None:
        a = Attributor(ctx(), resolver())
        s = a.slices(Signal(id="w", at=T0, source="jira", kind=KIND_WORKLOG, actor="Ada", actor_email="ada@x.com", refs=["jira:NDT-1"], weight=0.0, minutes=480.0, meta={}))
        s += a.slices(commit("c", T0 + 60, "Ada", "ada@x.com", ["jira:NDT-2"]))
        # Default: the 8h worklog is a bounded weight (120) beside a 45 commit -> 8h split ~73/27, nothing logged.
        rows = allocate(s, CLOCK, capacity_hours=8.0)["rows"]
        self.assertEqual(sum(r["logged_hours"] for r in rows), 0.0)
        self.assertAlmostEqual(sum(r["hours"] for r in rows), 8.0)
        p1 = next(r for r in rows if r["project_id"] == "p1")
        self.assertAlmostEqual(p1["hours"], 8.0 * 120 / 165, places=2)
        # Trusted: booked as logged.
        rows = allocate(s, CLOCK, capacity_hours=8.0, worklog_trust={"jira": "full"})["rows"]
        self.assertEqual(next(r for r in rows if r["project_id"] == "p1")["logged_hours"], 8.0)
        # Ignored: the worklog vanishes, the commit takes the day.
        rows = allocate(s, CLOCK, capacity_hours=8.0, worklog_trust={"jira": "ignore"})["rows"]
        self.assertEqual([(r["project_id"], r["hours"]) for r in rows], [(None, 8.0)])

    def test_logged_over_capacity_is_not_topped_up(self) -> None:
        a = Attributor(ctx(), resolver())
        s = a.slices(Signal(id="w", at=T0, source="jira", kind=KIND_WORKLOG, actor="Ada", actor_email="ada@x.com", refs=["jira:NDT-1"], weight=0.0, minutes=600.0, meta={}))
        s += a.slices(commit("c", T0 + 60, "Ada", "ada@x.com", []))
        rows = allocate(s, CLOCK, capacity_hours=8.0, worklog_trust={"jira": "full"})["rows"]
        self.assertEqual([r["hours"] for r in rows], [10.0])

    def test_rollup_and_series_conserve_hours(self) -> None:
        rows = allocate(self.slices(), CLOCK, worklog_trust={"jira": "full"})["rows"]
        total = sum(r["hours"] for r in rows)
        self.assertAlmostEqual(sum(r["hours"] for r in rollup(rows, "initiative_id")), total, places=1)
        self.assertAlmostEqual(sum(r["hours"] for r in rollup(rows, "goal_ids")), total, places=1)
        weekly = series(rows, "week", "initiative_id", top=1)
        self.assertEqual(weekly["periods"], ["2026-W01"])
        self.assertAlmostEqual(sum(sum(s["values"]) for s in weekly["series"]), total, places=1)
        self.assertTrue(any(s["key"] == "__other__" for s in weekly["series"]))


class FindingsTest(unittest.TestCase):
    def test_rules_fire_with_evidence_and_stable_ids(self) -> None:
        c = ctx()
        a = Attributor(c, resolver())
        now = T0 + 60 * DAY
        slices = a.slices(commit("old", T0, "Ada", "ada@x.com", ["jira:NDT-1"]))          # in progress, silent since day 0
        slices += a.slices(commit("todo", now - DAY, "Ada", "ada@x.com", ["jira:NDT-2"]))   # commits on a To Do ticket
        for i in range(20):
            slices += a.slices(commit(f"u{i}", now - 2 * DAY, "Ada", "ada@x.com", [], repo="src/dataflow", system="dataflow"))
        timelines = ticket_timelines({"jira:NDT-1": {"created": T0 - DAY, "status": "In Progress", "status_cat": "in_progress", "status_since": T0, "transitions": [{"at": T0, "from": "To Do", "to": "In Progress", "from_cat": "todo", "to_cat": "in_progress"}]},
                                      "jira:NDT-2": {"created": T0, "status": "To Do", "status_cat": "todo", "transitions": []}}, now=now)
        rows = allocate(slices, CLOCK)["rows"]
        found = detect(slices=slices, alloc_rows=rows, timelines=timelines, tickets=c.tickets, changes=list(c.changes_by_id.values()), projects=c.projects, initiatives=c.initiatives, resolver_suggestions=[], thresholds=DEFAULT_THRESHOLDS, clock=CLOCK, now=now, start=T0 - DAY, end=now + DAY)
        rules = {f["rule"] for f in found}
        self.assertIn("stale_in_progress", rules)
        # Staleness is the root cause: the long-running and review-queue rules stay
        # quiet for a ticket already reported stale, so one problem is one finding.
        self.assertNotIn("zombie_in_progress", rules)
        self.assertIn("status_lag", rules)
        self.assertIn("unkeyed_commits", rules)
        self.assertIn("unplanned_work", rules)
        lag = next(f for f in found if f["rule"] == "status_lag")
        self.assertEqual(lag["id"], finding_id("status_lag", "jira:NDT-2"))
        self.assertEqual(lag["evidence"][0]["who"], "Ada Lovelace")
        self.assertEqual(found[0]["severity"], "high")

    def test_ancient_stale_tickets_fold_into_one_backlog_finding(self) -> None:
        c = ctx()
        now = T0 + 400 * DAY
        timelines = {}
        for i in range(3):
            key = f"jira:NDT-{100 + i}"
            c.tickets[key] = {"tracker_id": "jira", "key": f"NDT-{100 + i}", "type": "task", "state_category": "In Progress", "parent_key": None, "components": [], "project": "NDT", "title": f"old {i}", "url": "", "assignee": "Ada Lovelace"}
            timelines[key] = {"created": T0 - DAY, "status": "In Progress", "status_cat": "in_progress", "status_since": T0, "transitions": [{"at": T0, "from": "To Do", "to": "In Progress", "from_cat": "todo", "to_cat": "in_progress"}], "first_start": T0, "last_done": None, "reopens": 0, "review_secs": 0, "blocked_secs": 0, "type": "task"}
        slices = Attributor(c, resolver()).slices(commit("s", T0, "Ada", "ada@x.com", ["jira:NDT-100", "jira:NDT-101", "jira:NDT-102"]))
        found = detect(slices=slices, alloc_rows=[], timelines=timelines, tickets=c.tickets, changes=[], projects={}, initiatives={}, resolver_suggestions=[], thresholds=DEFAULT_THRESHOLDS, clock=CLOCK, now=now, start=now - 90 * DAY, end=now + DAY)
        rules = [f["rule"] for f in found]
        self.assertEqual(rules.count("abandoned_backlog"), 1)
        self.assertNotIn("stale_in_progress", rules)
        self.assertNotIn("zombie_in_progress", rules)
        backlog = next(f for f in found if f["rule"] == "abandoned_backlog")
        self.assertEqual(backlog["value"], 3)
        self.assertEqual(backlog["entity"], "NDT")
        self.assertEqual(len(backlog["evidence"]), 3)

    def test_feedback_overlay_hides_but_counts(self) -> None:
        findings = [{"id": "a", "rule": "stale_in_progress", "severity": "high"}, {"id": "b", "rule": "bus_factor", "severity": "low"}, {"id": "c", "rule": "reopened", "severity": "low"}]
        visible, counts = overlay_feedback(findings, {"a": {"state": "dismissed"}, "c": {"state": "snoozed", "until": 1.0}}, {"bus_factor"}, now=100.0)
        self.assertEqual([f["id"] for f in visible], ["c"])     # snooze expired -> open again
        self.assertEqual(counts["dismissed"], 1)
        self.assertEqual(counts["muted"], 1)

    def test_thresholds_layer(self) -> None:
        t = effective_thresholds({"stale_in_progress_days": 3}, {"stale_in_progress_days": 5, "bogus": 1})
        self.assertEqual(t["stale_in_progress_days"], 5.0)
        self.assertNotIn("bogus", t)


class FinanceTest(unittest.TestCase):
    def test_statement_splits_capex_and_prices_when_it_can(self) -> None:
        c = ctx()
        a = Attributor(c, resolver())
        slices = a.slices(commit("s1", T0, "Ada", "ada@x.com", ["jira:NDT-1"])) + a.slices(commit("s2", T0 + 60, "Ada", "ada@x.com", ["jira:NDT-2"]))
        rows = allocate(slices, CLOCK)["rows"]
        report = statement(rows, projects=c.projects, initiatives=c.initiatives, rate_for=lambda pid: (100.0, "roster") if pid == "ada" else (None, "no rate"))
        self.assertEqual(report["totals"]["hours"], 8.0)
        self.assertEqual(report["totals"]["capex_hours"], 4.0)
        self.assertEqual(report["totals"]["capex_amount"], 400.0)
        self.assertEqual(report["capex_pct"], 50.0)
        treatments = {r["project_title"]: r["treatment"] for r in report["rows"]}
        self.assertEqual(treatments, {"Project One": "capitalize", "Unattributed": "expense"})
        csv_text = to_csv(report, period_label="p")
        self.assertIn("Project One", csv_text)
        self.assertEqual(csv_text.count("\n"), 3)


class FlowTest(unittest.TestCase):
    def test_cycle_and_reopen(self) -> None:
        facts = {"jira:NDT-9": {"created": T0, "status": "Done", "status_cat": "done", "transitions": [
            {"at": T0 + DAY, "from": "To Do", "to": "In Progress", "from_cat": "todo", "to_cat": "in_progress"},
            {"at": T0 + 3 * DAY, "from": "In Progress", "to": "Code Review", "from_cat": "in_progress", "to_cat": "in_progress"},
            {"at": T0 + 5 * DAY, "from": "Code Review", "to": "Done", "from_cat": "in_progress", "to_cat": "done"},
            {"at": T0 + 6 * DAY, "from": "Done", "to": "In Progress", "from_cat": "done", "to_cat": "in_progress"},
            {"at": T0 + 7 * DAY, "from": "In Progress", "to": "Done", "from_cat": "in_progress", "to_cat": "done"}]}}
        tl = ticket_timelines(facts, now=T0 + 10 * DAY)["jira:NDT-9"]
        self.assertEqual(tl["reopens"], 1)
        self.assertEqual(tl["review_secs"], 2 * DAY)
        flow = flow_metrics(ticket_timelines(facts, now=T0 + 10 * DAY), [], start=T0, end=T0 + 10 * DAY, clock=CLOCK)
        self.assertEqual(flow["finished"], 1)
        self.assertEqual(flow["cycle_days"]["p50"], 6.0)
        self.assertEqual(flow["lead_days"]["p50"], 7.0)
        self.assertEqual(flow["reopen_rate_pct"], 100.0)


class RealizationTest(unittest.TestCase):
    def test_hours_are_classified_by_the_fate_of_the_work(self) -> None:
        from pm_studio.signals.metrics import realization, production_merges
        c = ctx()
        a = Attributor(c, resolver())
        now = T0 + 30 * DAY
        slices = a.slices(commit("done", T0, "Ada", "ada@x.com", ["jira:NDT-1"]))       # p1, will be done
        slices += a.slices(commit("unplanned", T0, "Bob", "bob@x.com", ["jira:NDT-2"]))  # known ticket, no project -> stranded
        slices += a.slices(commit("nokey", T0, "Bob", "bob@x.com", []))                  # untraceable
        rows = allocate(slices, CLOCK)["rows"]
        timelines = {"jira:NDT-1": {"status_cat": "done", "last_done": T0 + 5 * DAY}, "jira:NDT-2": {"status_cat": "todo", "last_done": None}}
        r = realization(rows, timelines, c.changes_by_ref, {"jira:NDT-1": T0, "jira:NDT-2": T0}, now=now, stale_days=10, clock=CLOCK)
        self.assertEqual(r["totals"], {"realized": 8.0, "in_flight": 0.0, "stranded": 4.0, "untraceable": 4.0})
        self.assertEqual(r["realization_pct"], 66.7)
        self.assertEqual(r["lead_days_p50"], 5.0)
        self.assertEqual(r["weekly_realized"], [{"week": CLOCK.week(T0 + 5 * DAY), "hours": 8.0}])
        self.assertEqual(r["by_initiative"]["i1"]["realization_pct"], 100.0)
        self.assertIsNone(r["by_initiative"][None]["realization_pct"] if r["by_initiative"][None]["hours"] == r["by_initiative"][None]["untraceable"] else None)
        merged = Signal(id="pr", at=T0, source="ado-prs", kind="pr_merged", actor="Ada", actor_email="ada@x.com", refs=["jira:NDT-1"], weight=10.0, meta={"pr": 7, "target": "master"})
        self.assertEqual(production_merges(a.slices(merged), start=T0 - 1, end=now), {"i1": 1})

    def test_stranded_rule_and_unattributed_breakdown(self) -> None:
        from pm_studio.signals.service import unattributed_breakdown
        c = ctx()
        a = Attributor(c, resolver())
        now = T0 + 30 * DAY
        slices = []
        for i in range(3):
            slices += a.slices(commit(f"s{i}", T0 + i * DAY, "Ada", "ada@x.com", ["jira:NDT-1"]))
        rows = allocate(slices, CLOCK)["rows"]
        realized = {"by_initiative": {"i1": {"hours": 200.0, "realized": 20.0, "in_flight": 0.0, "stranded": 180.0, "untraceable": 0.0, "stranded_pct": 90.0}}}
        found = detect(slices=slices, alloc_rows=rows, timelines={}, tickets=c.tickets, changes=[], projects=c.projects, initiatives=c.initiatives, resolver_suggestions=[], thresholds=DEFAULT_THRESHOLDS, clock=CLOCK, now=now, start=T0 - DAY, end=now, realization=realized)
        stranded = [f for f in found if f["rule"] == "stranded_effort"]
        self.assertEqual(len(stranded), 1)
        self.assertEqual((stranded[0]["severity"], stranded[0]["entity"]), ("high", "i1"))
        rows2 = allocate(a.slices(commit("u", T0, "Bob", "bob@x.com", ["jira:NDT-3"])) + a.slices(commit("k", T0 + 60, "Bob", "bob@x.com", [], repo="src/x", system=None)), CLOCK)["rows"]
        u = unattributed_breakdown(rows2, [], c.tickets, {"src/x": {"keyed_commits": 1, "human_commits": 4}})
        self.assertEqual(u["hours"], 8.0)
        self.assertEqual(set(u["by_via"]), {"route-unplanned", "repo-only"})
        self.assertEqual(u["parents"][0]["ref"], "jira:NDT-E")
        self.assertEqual(u["parents"][0]["tickets"], 1)
        self.assertEqual(u["repos"][0], {"repo": "src/x", "hours": 4.0, "keyed_pct": 25.0})


class SiblingFoldTest(unittest.TestCase):
    def test_stale_siblings_fold_into_the_parent(self) -> None:
        c = ctx()
        now = T0 + 60 * DAY
        timelines = {}
        for i in range(3):
            key = f"jira:NDT-{200 + i}"
            c.tickets[key] = {"tracker_id": "jira", "key": f"NDT-{200 + i}", "type": "task", "state_category": "In Progress", "parent_key": "NDT-E", "components": [], "project": "NDT", "title": f"child {i}", "url": ""}
            timelines[key] = {"created": T0 - DAY, "status": "In Progress", "status_cat": "in_progress", "status_since": T0, "transitions": [{"at": T0, "from": "To Do", "to": "In Progress", "from_cat": "todo", "to_cat": "in_progress"}], "first_start": T0, "last_done": None, "reopens": 0, "review_secs": 0, "blocked_secs": 0, "type": "task"}
        slices = Attributor(c, resolver()).slices(commit("s", T0, "Ada", "ada@x.com", ["jira:NDT-200", "jira:NDT-201", "jira:NDT-202"]))
        found = detect(slices=slices, alloc_rows=[], timelines=timelines, tickets=c.tickets, changes=[], projects={}, initiatives={}, resolver_suggestions=[], thresholds=DEFAULT_THRESHOLDS, clock=CLOCK, now=now, start=now - 90 * DAY, end=now + DAY)
        rules = [f["rule"] for f in found]
        self.assertEqual(rules.count("stale_epic"), 1)
        self.assertNotIn("stale_in_progress", rules)
        epic = next(f for f in found if f["rule"] == "stale_epic")
        self.assertEqual((epic["entity"], epic["value"], len(epic["evidence"])), ("jira:NDT-E", 3, 3))

    def test_coverage_separates_evidence_from_declaration(self) -> None:
        c = AttributionContext.build(tickets=[{"tracker_id": "jira", "key": "NDT-2", "type": "bug", "state_category": "To Do", "parent_key": None, "components": [], "project": "NDT", "raw_type": "Bug"}], changes=[], projects=[{"id": "pm", "title": "Catch-all", "initiative_id": None, "status": "open"}], initiatives=[], goals=[], routes=[], product_systems={}, default_projects={"jira:NDT:Bug": "pm"})
        a = Attributor(c, resolver())
        slices = a.slices(commit("d", T0, "Ada", "ada@x.com", ["jira:NDT-2"]))
        cov = coverage(slices)
        self.assertEqual((cov["placed_pct"], cov["evidence_pct"], cov["declared_pct"]), (100.0, 0.0, 100.0))


class StatusNormalizationTest(unittest.TestCase):
    def test_resolved_by_name_is_not_in_progress(self) -> None:
        from pm_studio.signals.metrics import normalize_cat
        self.assertEqual(normalize_cat("Resolved", "in_progress"), "resolved")
        self.assertEqual(normalize_cat("UAT Completed", "in_progress"), "resolved")
        self.assertEqual(normalize_cat("Rejected", "in_progress"), "removed")
        self.assertEqual(normalize_cat("Active", "in_progress"), "in_progress")
        self.assertEqual(normalize_cat("Closed", "done"), "done")

    def test_closure_lag_folds_resolved_tickets_and_keeps_stale_quiet(self) -> None:
        c = ctx()
        now = T0 + 100 * DAY
        timelines = {}
        for i in range(12):
            key = f"ado:{900 + i}"
            c.tickets[key] = {"tracker_id": "ado", "key": str(900 + i), "type": "task", "state_category": "In Progress", "parent_key": None, "components": [], "project": "Arizona", "title": f"res {i}", "url": ""}
            timelines[key] = {"created": T0, "status": "Resolved", "status_cat": "in_progress", "status_since": T0 + 10 * DAY, "transitions": [{"at": T0 + 10 * DAY, "from": "Active", "to": "Resolved", "from_cat": "in_progress", "to_cat": "in_progress"}]}
        tl = ticket_timelines(timelines, now=now)
        self.assertTrue(all(v["status_cat"] == "resolved" for v in tl.values()))
        found = detect(slices=[], alloc_rows=[], timelines=tl, tickets=c.tickets, changes=[], projects={}, initiatives={}, resolver_suggestions=[], thresholds=DEFAULT_THRESHOLDS, clock=CLOCK, now=now, start=now - 90 * DAY, end=now + DAY)
        rules = [f["rule"] for f in found]
        self.assertEqual(rules.count("closure_lag"), 1)
        self.assertNotIn("stale_in_progress", rules)
        self.assertNotIn("abandoned_backlog", rules)
        self.assertEqual(next(f for f in found if f["rule"] == "closure_lag")["value"], 12)
        flow = flow_metrics(tl, [], start=now - 90 * DAY, end=now, clock=CLOCK)
        self.assertEqual(flow["awaiting_closure"]["count"], 12)

    def test_service_accounts_are_bots_at_attribution_time(self) -> None:
        a = Attributor(ctx(), resolver())
        for name, email in (("[arizonaproject]\\Project Collection Service Accounts", ""), ("Checklists for Jira (Pro) by HeroCoders", ""), ("wrike.sync@alivi.com", "wrike.sync@alivi.com")):
            s = a.slices(commit("b", T0, name, email, ["jira:NDT-1"]))[0]
            self.assertTrue(s["bot"], name)
            self.assertEqual(s["person_id"], "bot")
        self.assertEqual(a.resolver.suggestions(), [])


class JudgeParsingTest(unittest.TestCase):
    def test_parse_accepts_fenced_json_and_rejects_shapes(self) -> None:
        good = parse_judgment('```json\n{"scores": {"overall": "72.4", "attribution": 130}, "findings_review": [{"id": "x", "verdict": "noise", "reason": "bot"}, {"id": "y", "verdict": "maybe"}], "threshold_suggestions": [{"key": "stale_in_progress_days", "current": 10, "suggested": 14, "reason": "r"}], "summary": "ok"}\n```')
        self.assertEqual(good["scores"], {"overall": 72, "attribution": 100})
        self.assertEqual(len(good["findings_review"]), 1)
        self.assertEqual(good["threshold_suggestions"][0]["suggested"], 14.0)
        self.assertIsNone(parse_judgment("not json"))
        self.assertIsNone(parse_judgment('{"summary": "no scores"}'))

    def test_self_assessment_and_dossier(self) -> None:
        report = {"window": {}, "coverage": {"placed_pct": 60, "people": 10, "unresolved_people": 2}, "workflow": {"keyed_pct": 80}, "finance": {"logged_pct": 0}, "sources": [{"configured": True, "collected_at": 0}], "finding_counts": {"visible": 10, "dismissed": 5}, "findings": [{"id": "a", "rule": "r", "severity": "high", "title": "t", "detail": "d", "evidence": [], "state": "open"}]}
        dossier = build_dossier(report, feedback={"a": {"state": "dismissed", "source": "human"}}, previous=None, thresholds=DEFAULT_THRESHOLDS)
        self.assertEqual(dossier["human_feedback"], {"a": {"state": "dismissed", "note": None}})
        scores = self_assessment(dossier)["scores"]
        self.assertEqual(scores["attribution"], 60)
        self.assertEqual(scores["finding_precision"], 67)
        self.assertEqual(scores["freshness"], 0)


class StateTest(unittest.TestCase):
    def test_feedback_and_tuning_persist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fb = FeedbackStore(Path(tmp) / "feedback.json")
            fb.set("f1", state="dismissed", by="ada", note="bot")
            fb.set_judged("f1", "noise", "automation", 1.0)
            again = FeedbackStore(Path(tmp) / "feedback.json")
            self.assertEqual(again.get("f1")["state"], "dismissed")
            self.assertEqual(again.get("f1")["judged"]["verdict"], "noise")
            tuning = TuningStore(Path(tmp) / "tuning.json")
            tuning.set("thresholds", "stale_in_progress_days", 14, by="judge")
            tuning.set("muted_rules", "bus_factor", True, by="ada")
            again = TuningStore(Path(tmp) / "tuning.json")
            self.assertEqual(again.thresholds(), {"stale_in_progress_days": 14.0})
            self.assertEqual(again.muted_rules(), {"bus_factor"})
            self.assertEqual(len(again.data["history"]), 2)
