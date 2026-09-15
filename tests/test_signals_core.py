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
        result = allocate(self.slices(), CLOCK, capacity_hours=8.0)
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

    def test_logged_over_capacity_is_not_topped_up(self) -> None:
        a = Attributor(ctx(), resolver())
        s = a.slices(Signal(id="w", at=T0, source="jira", kind=KIND_WORKLOG, actor="Ada", actor_email="ada@x.com", refs=["jira:NDT-1"], weight=0.0, minutes=600.0, meta={}))
        s += a.slices(commit("c", T0 + 60, "Ada", "ada@x.com", []))
        rows = allocate(s, CLOCK, capacity_hours=8.0)["rows"]
        self.assertEqual([r["hours"] for r in rows], [10.0])

    def test_rollup_and_series_conserve_hours(self) -> None:
        rows = allocate(self.slices(), CLOCK)["rows"]
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
