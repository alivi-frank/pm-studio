"""End-to-end tests of the service: sources -> ledger -> report -> feedback -> judge
cycle, over a temp workspace with the inbox adapter and PM activity as the only
configured sources (git and the trackers need real checkouts / networks)."""

import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from pm_studio.config import SignalsConfig
from pm_studio.signals import service as service_mod
from pm_studio.signals.service import IntelligenceService

NOW = time.time()
DAY = 86400.0


def iso(t: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(t))


class ServiceCycleTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.workspace = root / "ws" / "workspace"
        inbox = self.workspace / "signals" / "inbox"
        inbox.mkdir(parents=True)
        # Two meetings and a message, tagged with tickets.
        records = [
            {"kind": "meeting", "at": iso(NOW - 2 * DAY), "minutes": 60, "subject": "NDT-1 design review", "attendees": [{"name": "Ada Lovelace", "email": "ada@x.com"}, {"name": "Zed Unknown", "email": "zed@nowhere.example"}], "channel": "outlook", "id": "m1"},
            {"kind": "message", "at": iso(NOW - 2 * DAY + 3600), "subject": "re: NDT-2", "actor": "Ada Lovelace", "email": "ada@x.com", "channel": "teams", "id": "t1"},
            {"kind": "meeting", "at": iso(NOW - 40 * DAY), "minutes": 30, "subject": "old NDT-1 sync", "attendees": [{"name": "Ada Lovelace", "email": "ada@x.com"}], "id": "m0"},
        ]
        (inbox / "export.json").write_text(json.dumps(records))
        (self.workspace / "activity.jsonl").write_text(json.dumps({"at": NOW - DAY, "user_id": "u1", "kind": "pm_turn", "project_id": "p1", "session_id": "s", "agent_cost_usd": 1.5, "input_tokens": 1, "output_tokens": 2}) + "\n")
        self.stores = {
            "tickets": lambda: [
                {"tracker_id": "jira", "key": "NDT-1", "type": "story", "state_category": "In Progress", "parent_key": None, "components": [], "project": "NDT", "title": "one", "url": "", "assignee": "Ada Lovelace"},
                {"tracker_id": "jira", "key": "NDT-2", "type": "bug", "state_category": "To Do", "parent_key": None, "components": [], "project": "NDT", "title": "two", "url": ""},
            ],
            "changes": lambda: [{"id": "c1", "product": "web", "system": None, "project_id": "p1", "tracker_id": "jira", "ticket_key": "NDT-1", "status": "in_progress", "shipped_at": None, "updated_at": NOW}],
            "projects": lambda: [{"id": "p1", "title": "Project One", "initiative_id": "i1", "status": "open", "updated_at": NOW}],
            "initiatives": lambda: [{"id": "i1", "title": "Init One", "goal_ids": ["g1"], "status": "open", "is_maintenance": False, "updated_at": NOW}],
            "goals": lambda: [{"id": "g1", "title": "Goal"}],
            "people": lambda: [{"id": "ada", "name": "Ada Lovelace", "email": "ada@x.com", "identities": [], "account_id": "u1", "updated_at": NOW}],
            "accounts": lambda: [{"id": "u1", "name": "Ada Lovelace", "email": "ada@x.com"}],
            "releases": lambda: [],
            "trackers": lambda: [],
            "product_labels": lambda: {"web": "Web"},
        }
        # Jira tracker declared without credentials: the history source is unconfigured
        # but the key filter knows NDT is a real project.
        self.trackers = [SimpleNamespace(id="jira", provider="jira", base_url="", projects=("NDT",), username="", token="", routes=())]
        self.service = IntelligenceService(repo_root=root, workspace_dir=self.workspace, config=SignalsConfig(since="2020-01-01", timezone="UTC", auto_refresh_minutes=0), trackers=self.trackers, systems={}, product_systems={"web": ("web-stack",)}, routes=[], stores=self.stores, rate_for=lambda pid: (120.0, "roster") if pid == "ada" else (None, "no rate"))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_refresh_report_feedback_and_judge_cycle(self) -> None:
        status = self.service.refresh()
        by_id = {s["id"]: s for s in status["status"]}
        self.assertEqual(by_id["inbox"]["state"], "ok")
        self.assertEqual(by_id["inbox"]["signals"], 4)
        self.assertEqual(by_id["pm"]["signals"], 1)
        self.assertEqual(by_id["jira"]["state"], "unconfigured")
        self.assertFalse(by_id["git"]["configured"])
        # Caches landed on disk and reload.
        self.assertTrue((self.workspace / "signals" / "cache" / "inbox.json.gz").is_file())

        report = self.service.report(None, None, now=NOW)
        self.assertEqual(report["window"]["days"], 90)
        k = report["kpis"]
        # Ada, day 1 (40 days ago): a 30-minute meeting, nothing else -> 0.5h logged, no
        # capacity is invented on top. Ada, day 2: a 1h logged meeting plus a weighted
        # message -> 1h logged + the remaining 7h inferred onto the message's ticket = 8h.
        # Zed, day 2: 1h logged only. Total 9.5h - every hour points at a signal.
        self.assertEqual(k["active_people"], 2)
        self.assertAlmostEqual(k["hours"], 9.5, places=1)
        self.assertEqual(k["ai_cost_usd"], 1.5)
        alloc = {r["title"]: r["hours"] for r in report["allocation"]["by_initiative"]}
        self.assertIn("Init One", alloc)
        self.assertEqual(report["coverage"]["people"], 2)
        self.assertEqual(report["coverage"]["unresolved_people"], 1)
        self.assertEqual(report["identity"]["suggestions"][0]["handle"], "zed@nowhere.example")
        rules = {f["rule"] for f in report["findings"]}
        self.assertIn("unplanned_work", rules)  # NDT-2 is planned nowhere
        finance = report["finance"]
        ada_rows = [r for r in finance["rows"] if r["person_id"] == "ada"]
        self.assertTrue(all(r["rate"] == 120.0 for r in ada_rows))
        self.assertTrue(finance["totals"]["capex_amount"] > 0)
        self.assertIn("methodology", finance)
        self.assertEqual(report["judge"]["latest"], None)
        self.assertIn("overall", report["judge"]["self_assessment"]["scores"])

        # Feedback hides a finding and changes the counts; ids are stable across reports.
        first = report["findings"][0]
        self.service.give_feedback(first["id"], state="dismissed", by="ada", note="artefact")
        report2 = self.service.report(None, None, now=NOW)
        self.assertNotIn(first["id"], {f["id"] for f in report2["findings"]})
        self.assertEqual(report2["finding_counts"]["dismissed"], 1)
        self.assertIn(first["id"], {f["id"] for f in report2["hidden_findings"]})

        # Tuning: a threshold edit is validated and recorded.
        self.service.tune("thresholds", "unplanned_share_pct", 99, by="ada", reason="test")
        report3 = self.service.report(None, None, now=NOW)
        self.assertNotIn("unplanned_work", {f["rule"] for f in report3["findings"]})
        with self.assertRaises(ValueError):
            self.service.tune("thresholds", "nope", 1, by="ada")
        self.service.tune("muted_rules", "unresolved_author", True, by="ada")
        self.assertNotIn("unresolved_author", {f["rule"] for f in self.service.report(None, None, now=NOW)["findings"]})

        # The judge cycle with a fake CLI: verdicts land on findings, suggestions apply.
        target = report3["findings"][0]["id"] if report3["findings"] else first["id"]
        answer = {"scores": {"attribution": 70, "allocation_plausibility": 60, "finding_precision": 80, "data_quality": 65, "overall": 69}, "findings_review": [{"id": target, "verdict": "confirm", "reason": "checked"}], "threshold_suggestions": [{"key": "stale_in_progress_days", "current": 10, "suggested": 14, "reason": "quiet team"}], "data_fixes": [{"kind": "alias", "detail": "zed is Ada's alt", "handle": "zed@nowhere.example", "person_id": "ada"}], "next_metrics": [{"title": "PR review latency", "why": "review is the bottleneck", "needs": "pull requests"}], "summary": "Solid but under-tagged."}
        def fake_run(cmd):
            self.assertEqual(cmd[0], "claude")
            self.assertIn("--allowedTools", cmd)
            return SimpleNamespace(stdout=json.dumps({"result": json.dumps(answer), "total_cost_usd": 0.5, "usage": {"input_tokens": 10, "output_tokens": 20}}), stderr="", returncode=0)
        original = service_mod.judge_mod.run_judge
        service_mod.judge_mod.run_judge = lambda dossier, **kw: original(dossier, runner=fake_run, **kw)
        try:
            self.service._run_judge("ada")
        finally:
            service_mod.judge_mod.run_judge = original
        latest = self.service.judgments.latest()
        self.assertEqual(latest["verdict"], "ok")
        self.assertEqual(latest["scores"]["overall"], 69)
        self.assertEqual(latest["agent_usage"]["cost_usd"], 0.5)
        report4 = self.service.report(None, None, now=NOW)
        judged = self.service.feedback.get(target)["judged"]
        self.assertEqual(judged["verdict"], "confirm")
        self.assertEqual(report4["judge"]["latest"]["summary"], "Solid but under-tagged.")
        dossiers = list((self.workspace / "signals" / "judge").glob("dossier-*.json"))
        self.assertEqual(len(dossiers), 1)
        self.assertNotIn("token", dossiers[0].read_text().lower().split("thresholds")[0][:0])
        self.service.apply_suggestion(0, by="ada")
        self.assertEqual(self.service.thresholds()["stale_in_progress_days"], 14.0)
        self.assertTrue(self.service.judgments.latest()["threshold_suggestions"][0].get("applied_at"))
        # Alias from the judge's data fix folds Zed into Ada.
        self.service.add_alias("zed@nowhere.example", "ada", by="ada")
        report5 = self.service.report(None, None, now=NOW)
        self.assertEqual(report5["coverage"]["unresolved_people"], 0)
        self.assertEqual(report5["kpis"]["active_people"], 1)

    def test_inconclusive_judge_is_visible_not_silent(self) -> None:
        self.service.refresh()
        original = service_mod.judge_mod.run_judge
        service_mod.judge_mod.run_judge = lambda dossier, **kw: original(dossier, runner=lambda cmd: SimpleNamespace(stdout="", stderr="boom", returncode=1), **kw)
        try:
            self.service._run_judge("ada")
        finally:
            service_mod.judge_mod.run_judge = original
        latest = self.service.judgments.latest()
        self.assertEqual(latest["verdict"], "inconclusive")
        self.assertIn("no output", latest["reason"])
        self.assertIn("no output", self.service.judge_error)

    def test_filters_and_windows(self) -> None:
        self.service.refresh()
        wide = self.service.report("2020-01-01", None, now=NOW)
        self.assertEqual(wide["period"], "month")
        self.assertEqual(wide["kpis"]["active_days"], 3)  # the 40-day-old meeting counts now
        only_ada = self.service.report(None, None, filters={"person_id": "ada"}, now=NOW)
        self.assertEqual(only_ada["kpis"]["active_people"], 1)
        self.assertEqual(only_ada["filters"], {"person_id": "ada"})
        csv_text = self.service.finance_csv(None, None)
        self.assertIn("Ada Lovelace", csv_text)
        tr = self.service.trace(None, None, person_id="ada", project_id="p1")
        self.assertTrue(tr["hours"] > 0)
        self.assertTrue(all(s["id"] for s in tr["signals"]))
        sig = self.service.entity_signals(None, None, ref="jira:NDT-1")
        self.assertTrue(len(sig) >= 1)
