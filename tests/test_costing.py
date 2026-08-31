"""Tests for time and cost attribution.

The property that makes this usable is that **the numbers reconcile**: a person's
distributed hours always sum to their declared capacity, because capacity is the input
and signals only decide the proportions. Most of these tests pin that, plus the two
separations that keep the output honest - labour vs agent spend, and additive-up-to-
initiative vs overlapping goals.
"""

import tempfile
import unittest
from pathlib import Path

from pm_studio.costing import (
    KIND_DEV_TASK,
    KIND_PM_TURN,
    UNATTRIBUTED,
    CostingError,
    CostingStore,
    agent_usage,
    week_bounds,
    week_key,
)
from pm_studio.portfolio import PortfolioStore

WEEK = "2026-W31"


class WeekMathTest(unittest.TestCase):
    def test_key_and_bounds_agree(self) -> None:
        start, end = week_bounds(WEEK)
        self.assertEqual(week_key(start), WEEK)
        self.assertEqual(week_key(end - 1), WEEK)
        # The end is exclusive, so it belongs to the next week.
        self.assertNotEqual(week_key(end), WEEK)
        self.assertAlmostEqual(end - start, 7 * 24 * 3600, places=3)

    def test_malformed_week_rejected(self) -> None:
        for bad in ("", "2026", "2026-W99", "nonsense", None):
            with self.assertRaises(CostingError):
                week_bounds(bad)


class AgentUsageTest(unittest.TestCase):
    def test_reads_what_the_cli_already_reports(self) -> None:
        parsed = agent_usage(
            {"total_cost_usd": 0.42, "usage": {"input_tokens": 1200, "output_tokens": 300}}
        )
        self.assertEqual(parsed, {"cost_usd": 0.42, "input_tokens": 1200, "output_tokens": 300})

    def test_missing_fields_are_zero_not_an_error(self) -> None:
        self.assertEqual(
            agent_usage({}), {"cost_usd": 0.0, "input_tokens": 0, "output_tokens": 0}
        )
        self.assertEqual(agent_usage({"total_cost_usd": None})["cost_usd"], 0.0)


class DistributionTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.store = CostingStore(
            path=root / "costing.json",
            activity_path=root / "activity.jsonl",
            blended_rate=100.0,
        )
        self.mid = week_bounds(WEEK)[0] + 3600

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _row(self, report, user_id):
        return next(r for r in report["users"] if r["user_id"] == user_id)

    def test_capacity_splits_by_signal_share(self) -> None:
        """The headline behaviour: 40h split 1:3 across two projects."""
        self.store.record("dana", KIND_PM_TURN, project_id="alpha", at=self.mid)
        self.store.record("dana", KIND_DEV_TASK, project_id="beta", at=self.mid)
        report = self.store.distribute_week(WEEK)
        row = self._row(report, "dana")
        # pm_turn weighs 1, dev_task weighs 3 -> 10h / 30h of a 40h week.
        self.assertAlmostEqual(row["effective_hours"]["alpha"], 10.0)
        self.assertAlmostEqual(row["effective_hours"]["beta"], 30.0)

    def test_hours_always_reconcile_to_capacity(self) -> None:
        """The property the whole design exists for - nobody can argue with the
        denominator."""
        for project in ("alpha", "beta", "gamma", "alpha", "alpha"):
            self.store.record("dana", KIND_PM_TURN, project_id=project, at=self.mid)
        report = self.store.distribute_week(WEEK)
        self.assertAlmostEqual(self._row(report, "dana")["total_hours"], 40.0, places=3)

    def test_declared_capacity_is_respected(self) -> None:
        self.store.set_entry("dana", capacity_hours_per_week=20.0)
        self.store.record("dana", KIND_PM_TURN, project_id="alpha", at=self.mid)
        report = self.store.distribute_week(WEEK)
        self.assertAlmostEqual(self._row(report, "dana")["total_hours"], 20.0)

    def test_someone_with_no_activity_reports_zero_not_a_full_week(self) -> None:
        """Distributing a full capacity across nothing would invent hours."""
        report = self.store.distribute_week(WEEK, user_ids=["idle"])
        row = self._row(report, "idle")
        self.assertEqual(row["effective_hours"], {})
        self.assertEqual(row["total_hours"], 0.0)

    def test_signals_outside_the_week_are_ignored(self) -> None:
        self.store.record("dana", KIND_PM_TURN, project_id="alpha", at=self.mid)
        self.store.record(
            "dana", KIND_PM_TURN, project_id="beta", at=week_bounds(WEEK)[1] + 10
        )
        report = self.store.distribute_week(WEEK)
        self.assertEqual(list(self._row(report, "dana")["effective_hours"]), ["alpha"])

    def test_work_with_no_project_is_parked_not_dropped(self) -> None:
        self.store.record("dana", KIND_PM_TURN, project_id=None, at=self.mid)
        report = self.store.distribute_week(WEEK)
        self.assertIn(UNATTRIBUTED, self._row(report, "dana")["effective_hours"])

    def test_each_person_is_distributed_independently(self) -> None:
        self.store.record("dana", KIND_PM_TURN, project_id="alpha", at=self.mid)
        self.store.record("ravi", KIND_PM_TURN, project_id="beta", at=self.mid)
        report = self.store.distribute_week(WEEK)
        self.assertAlmostEqual(self._row(report, "dana")["effective_hours"]["alpha"], 40.0)
        self.assertAlmostEqual(self._row(report, "ravi")["effective_hours"]["beta"], 40.0)

    def test_unknown_signal_kind_rejected(self) -> None:
        """Silently counting it at zero weight would make effort disappear."""
        with self.assertRaises(CostingError):
            self.store.record("dana", "gardening", project_id="alpha")


class RatesTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.mid = week_bounds(WEEK)[0] + 3600

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _store(self, blended=None):
        return CostingStore(
            path=self.root / "costing.json",
            activity_path=self.root / "activity.jsonl",
            blended_rate=blended,
        )

    def test_blended_rate_applies_when_there_is_no_individual_one(self) -> None:
        """The point of supporting blended: an org that won't put individual salaries in
        a tool still gets project cost."""
        store = self._store(blended=100.0)
        store.record("dana", KIND_PM_TURN, project_id="alpha", at=self.mid)
        row = store.distribute_week(WEEK)["users"][0]
        self.assertEqual(row["rate_source"], "blended")
        self.assertAlmostEqual(row["total_labor_cost"], 4000.0)

    def test_individual_rate_wins(self) -> None:
        store = self._store(blended=100.0)
        store.set_entry("dana", rate_per_hour=150.0)
        store.record("dana", KIND_PM_TURN, project_id="alpha", at=self.mid)
        row = store.distribute_week(WEEK)["users"][0]
        self.assertEqual(row["rate_source"], "individual")
        self.assertAlmostEqual(row["total_labor_cost"], 6000.0)

    def test_clearing_an_individual_rate_falls_back_to_blended(self) -> None:
        store = self._store(blended=100.0)
        store.set_entry("dana", rate_per_hour=150.0)
        store.set_entry("dana", clear_rate=True)
        self.assertIsNone(store.entry_for("dana").rate_per_hour)

    def test_with_no_rate_at_all_hours_are_still_reported(self) -> None:
        """Better an unknown cost than an invented one."""
        store = self._store(blended=None)
        store.record("dana", KIND_PM_TURN, project_id="alpha", at=self.mid)
        row = store.distribute_week(WEEK)["users"][0]
        self.assertEqual(row["rate_source"], "none")
        self.assertAlmostEqual(row["total_hours"], 40.0)
        self.assertEqual(row["labor_cost"], {})

    def test_invalid_rate_and_capacity_rejected(self) -> None:
        store = self._store()
        with self.assertRaises(CostingError):
            store.set_entry("dana", rate_per_hour=-5)
        with self.assertRaises(CostingError):
            store.set_entry("dana", capacity_hours_per_week=0)
        with self.assertRaises(CostingError):
            store.set_entry("dana", capacity_hours_per_week=200)

    def test_unconfigured_person_gets_sane_defaults(self) -> None:
        entry = self._store().entry_for("nobody")
        self.assertIsNone(entry.rate_per_hour)
        self.assertEqual(entry.capacity_hours_per_week, 40.0)

    def test_rates_are_not_world_readable(self) -> None:
        store = self._store()
        store.set_entry("dana", rate_per_hour=150.0)
        self.assertEqual((self.root / "costing.json").stat().st_mode & 0o777, 0o600)


class HumanVersusAgentCostTest(unittest.TestCase):
    """The distinction the module exists to protect: an agent grinding for 20 minutes
    while its user is at lunch is machine time, not labour."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.store = CostingStore(
            path=root / "costing.json",
            activity_path=root / "activity.jsonl",
            blended_rate=100.0,
        )
        self.mid = week_bounds(WEEK)[0] + 3600

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_agent_cost_is_summed_never_distributed(self) -> None:
        self.store.record(
            "dana", KIND_DEV_TASK, project_id="alpha", agent_cost_usd=1.25, at=self.mid
        )
        report = self.store.distribute_week(WEEK)
        self.assertAlmostEqual(report["by_project"]["alpha"]["agent_cost"], 1.25)
        # Labour and agent spend are separate columns, never added together.
        self.assertAlmostEqual(report["by_project"]["alpha"]["labor_cost"], 4000.0)
        self.assertAlmostEqual(report["totals"]["agent_cost"], 1.25)
        self.assertAlmostEqual(report["totals"]["labor_cost"], 4000.0)

    def test_machine_only_spend_adds_cost_but_no_hours(self) -> None:
        """An auto-continuation runs with nobody at the keyboard: its tokens count, its
        labour must not."""
        self.store.record(
            "", KIND_PM_TURN, project_id="alpha", agent_cost_usd=0.75, at=self.mid
        )
        report = self.store.distribute_week(WEEK)
        self.assertEqual(report["users"], [])
        self.assertAlmostEqual(report["by_project"]["alpha"]["agent_cost"], 0.75)
        self.assertEqual(report["totals"]["hours"], 0.0)

    def test_machine_only_spend_does_not_dilute_a_human_split(self) -> None:
        self.store.record("dana", KIND_PM_TURN, project_id="alpha", at=self.mid)
        self.store.record("", KIND_PM_TURN, project_id="beta", agent_cost_usd=5.0, at=self.mid)
        report = self.store.distribute_week(WEEK)
        row = next(r for r in report["users"] if r["user_id"] == "dana")
        # All 40 hours stay on alpha - beta was machine-only.
        self.assertAlmostEqual(row["effective_hours"]["alpha"], 40.0)
        self.assertNotIn("beta", row["effective_hours"])

    def test_tokens_are_reported_per_project(self) -> None:
        self.store.record(
            "dana", KIND_DEV_TASK, project_id="alpha",
            input_tokens=1000, output_tokens=200, at=self.mid,
        )
        report = self.store.distribute_week(WEEK)
        self.assertEqual(report["tokens_by_project"]["alpha"], {"input": 1000, "output": 200})


class OverrideTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.store = CostingStore(
            path=root / "costing.json",
            activity_path=root / "activity.jsonl",
            blended_rate=100.0,
        )
        self.mid = week_bounds(WEEK)[0] + 3600
        self.store.record("dana", KIND_PM_TURN, project_id="alpha", at=self.mid)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_override_replaces_effective_but_keeps_derived(self) -> None:
        """A report must always show what the system thought as well as what a human
        decided - an approximation cannot be the only record."""
        self.store.set_override(WEEK, "dana", {"alpha": 12.0, "beta": 28.0})
        row = self.store.distribute_week(WEEK)["users"][0]
        self.assertTrue(row["overridden"])
        self.assertEqual(row["effective_hours"], {"alpha": 12.0, "beta": 28.0})
        self.assertAlmostEqual(row["derived_hours"]["alpha"], 40.0)
        self.assertAlmostEqual(row["total_labor_cost"], 4000.0)

    def test_clearing_an_override_restores_the_derived_split(self) -> None:
        self.store.set_override(WEEK, "dana", {"alpha": 5.0})
        self.store.clear_override(WEEK, "dana")
        row = self.store.distribute_week(WEEK)["users"][0]
        self.assertFalse(row["overridden"])
        self.assertAlmostEqual(row["effective_hours"]["alpha"], 40.0)

    def test_negative_override_rejected(self) -> None:
        with self.assertRaises(CostingError):
            self.store.set_override(WEEK, "dana", {"alpha": -1.0})

    def test_override_survives_a_restart(self) -> None:
        self.store.set_override(WEEK, "dana", {"alpha": 7.0})
        reloaded = CostingStore(
            path=self.store._path, activity_path=self.store._activity_path, blended_rate=100.0
        )
        self.assertEqual(reloaded.override_for(WEEK, "dana"), {"alpha": 7.0})


class RollupTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.store = CostingStore(
            path=root / "costing.json",
            activity_path=root / "activity.jsonl",
            blended_rate=100.0,
        )
        self.portfolio = PortfolioStore(root / "portfolio.json")
        self.mid = week_bounds(WEEK)[0] + 3600

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_project_costs_roll_up_and_add_at_the_initiative(self) -> None:
        initiative = self.portfolio.create_initiative("Onboarding")
        first = self.portfolio.create_project("One", initiative_id=initiative.id)
        second = self.portfolio.create_project("Two", initiative_id=initiative.id)
        self.store.record("dana", KIND_PM_TURN, project_id=first.id, at=self.mid)
        self.store.record("dana", KIND_PM_TURN, project_id=second.id, at=self.mid)
        report = self.store.distribute_week(WEEK)
        rollup = self.store.rollup_to_initiatives(report["by_project"], self.portfolio)
        # Two 20h halves of one 40h week land as a single exact 40h initiative total.
        self.assertAlmostEqual(rollup["initiatives"][initiative.id]["hours"], 40.0)
        self.assertAlmostEqual(rollup["initiatives"][initiative.id]["labor_cost"], 4000.0)

    def test_goals_are_reported_but_flagged_as_overlapping(self) -> None:
        """An initiative can serve several goals, so the same spend contributes to each
        - the payload must carry that warning, not just the numbers."""
        goal_a = self.portfolio.create_goal("A")
        goal_b = self.portfolio.create_goal("B")
        initiative = self.portfolio.create_initiative("I", goal_ids=[goal_a.id, goal_b.id])
        project = self.portfolio.create_project("P", initiative_id=initiative.id)
        self.store.record("dana", KIND_PM_TURN, project_id=project.id, at=self.mid)
        report = self.store.distribute_week(WEEK)
        rollup = self.store.rollup_to_initiatives(report["by_project"], self.portfolio)
        self.assertEqual(
            sorted(rollup["initiatives"][initiative.id]["goal_ids"]),
            sorted([goal_a.id, goal_b.id]),
        )
        self.assertIn("never sum them", rollup["goal_note"])

    def test_unaligned_project_cost_is_visible_not_hidden(self) -> None:
        orphan = self.portfolio.create_project("Orphan")
        self.store.record("dana", KIND_PM_TURN, project_id=orphan.id, at=self.mid)
        report = self.store.distribute_week(WEEK)
        rollup = self.store.rollup_to_initiatives(report["by_project"], self.portfolio)
        self.assertIn(UNATTRIBUTED, rollup["initiatives"])
        self.assertAlmostEqual(rollup["initiatives"][UNATTRIBUTED]["hours"], 40.0)

    def test_rollup_totals_match_the_project_totals(self) -> None:
        """Nothing may be lost or double-counted on the way up."""
        initiative = self.portfolio.create_initiative("I")
        project = self.portfolio.create_project("P", initiative_id=initiative.id)
        orphan = self.portfolio.create_project("Orphan")
        self.store.record("dana", KIND_PM_TURN, project_id=project.id, at=self.mid)
        self.store.record("dana", KIND_DEV_TASK, project_id=orphan.id, at=self.mid)
        report = self.store.distribute_week(WEEK)
        rollup = self.store.rollup_to_initiatives(report["by_project"], self.portfolio)
        self.assertAlmostEqual(
            sum(b["hours"] for b in rollup["initiatives"].values()),
            sum(b["hours"] for b in report["by_project"].values()),
            places=3,
        )


class ActivityLogTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.activity = root / "nested" / "activity.jsonl"
        self.store = CostingStore(path=root / "costing.json", activity_path=self.activity)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_appends_and_creates_its_directory(self) -> None:
        self.store.record("dana", KIND_PM_TURN, project_id="alpha")
        self.store.record("dana", KIND_PM_TURN, project_id="alpha")
        self.assertEqual(len(self.activity.read_text().strip().splitlines()), 2)

    def test_truncated_final_line_does_not_break_a_report(self) -> None:
        mid = week_bounds(WEEK)[0] + 60
        self.store.record("dana", KIND_PM_TURN, project_id="alpha", at=mid)
        with self.activity.open("a") as handle:
            handle.write('{"at": 1.0, "user_id": "x", "kind": "pm')
        self.assertEqual(len(self.store.signals_in_week(WEEK)), 1)

    def test_missing_log_reports_an_empty_week(self) -> None:
        report = self.store.distribute_week(WEEK)
        self.assertEqual(report["signal_count"], 0)
        self.assertEqual(report["users"], [])


class ProjectActivityTest(unittest.TestCase):
    """The signal log read as ACTIVITY rather than cost: per project, when it was last
    touched and how many sessions touched it recently. What lets an ideation project -
    which has no changes by definition - testify to being alive."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.store = CostingStore(
            path=root / "costing.json", activity_path=root / "activity.jsonl"
        )
        self.now = week_bounds(WEEK)[0]
        self.window = self.now - 30 * 86400

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_counts_distinct_recent_sessions_and_keeps_the_last_touch(self) -> None:
        day = 86400
        # An old session outside the window, two recent ones (one touching twice).
        self.store.record("dana", KIND_PM_TURN, project_id="alpha", session_id="s1",
                          at=self.now - 40 * day)
        self.store.record("dana", KIND_PM_TURN, project_id="alpha", session_id="s2",
                          at=self.now - 2 * day)
        self.store.record("dana", KIND_PM_TURN, project_id="alpha", session_id="s2",
                          at=self.now - 1 * day)
        self.store.record("dana", KIND_PM_TURN, project_id="alpha", session_id="s3",
                          at=self.now - 3 * day)
        alpha = self.store.project_activity(recent_since=self.window)["alpha"]
        self.assertEqual(alpha["recent_sessions"], 2)
        self.assertAlmostEqual(alpha["last_at"], self.now - 1 * day, places=3)

    def test_last_at_outlives_the_recent_window(self) -> None:
        """"Last touched 74d ago" must stay sayable long after the window empties -
        that is what lets a dormant idea read as dormant rather than blank."""
        self.store.record("dana", KIND_PM_TURN, project_id="alpha", session_id="s1",
                          at=self.now - 74 * 86400)
        alpha = self.store.project_activity(recent_since=self.window)["alpha"]
        self.assertEqual(alpha["recent_sessions"], 0)
        self.assertAlmostEqual(alpha["last_at"], self.now - 74 * 86400, places=3)

    def test_machine_only_turns_still_count_as_touches(self) -> None:
        """No user at the keyboard is still the project being worked (the PM
        auto-continuing) - activity is not labour, so it counts here."""
        self.store.record("", KIND_PM_TURN, project_id="alpha", session_id="s1",
                          at=self.now - 60)
        alpha = self.store.project_activity(recent_since=self.window)["alpha"]
        self.assertEqual(alpha["recent_sessions"], 1)

    def test_unattributed_signals_are_ignored(self) -> None:
        self.store.record("dana", KIND_PM_TURN, project_id=None, session_id="s1",
                          at=self.now - 60)
        self.assertEqual(self.store.project_activity(recent_since=self.window), {})

    def test_missing_log_is_empty_not_an_error(self) -> None:
        self.assertEqual(self.store.project_activity(recent_since=self.window), {})


if __name__ == "__main__":
    unittest.main()


class CumulativeToDateTest(unittest.TestCase):
    """The to-date view folds every recorded week; its exactness rests on each week's
    distribution reconciling to that week's capacity, so summing them is safe."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.store = CostingStore(
            path=root / "costing.json",
            activity_path=root / "activity.jsonl",
            blended_rate=100.0,
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_cumulative_is_the_sum_of_its_weeks(self) -> None:
        w1 = week_bounds("2026-W30")[0] + 3600
        w2 = week_bounds("2026-W31")[0] + 3600
        self.store.record(at=w1, user_id="u1", kind="pm_turn", project_id="p1",
                          agent_cost_usd=1.0)
        self.store.record(at=w1, user_id="u1", kind="dev_task", project_id="p2",
                          agent_cost_usd=2.0)
        self.store.record(at=w2, user_id="u1", kind="pm_turn", project_id="p1",
                          agent_cost_usd=3.0)
        cumulative = self.store.cumulative_to_date(user_ids=["u1"])
        self.assertEqual(cumulative["weeks"], ["2026-W30", "2026-W31"])
        r30 = self.store.distribute_week("2026-W30", user_ids=["u1"])
        r31 = self.store.distribute_week("2026-W31", user_ids=["u1"])
        self.assertAlmostEqual(
            cumulative["totals"]["hours"],
            r30["totals"]["hours"] + r31["totals"]["hours"], places=3)
        self.assertAlmostEqual(
            cumulative["totals"]["labor_cost"],
            r30["totals"]["labor_cost"] + r31["totals"]["labor_cost"], places=3)
        self.assertAlmostEqual(cumulative["totals"]["agent_cost"], 6.0, places=3)
        p1 = cumulative["by_project"]["p1"]
        self.assertAlmostEqual(
            p1["hours"],
            r30["by_project"]["p1"]["hours"] + r31["by_project"]["p1"]["hours"],
            places=3)

    def test_empty_log_is_an_empty_view(self) -> None:
        cumulative = self.store.cumulative_to_date(user_ids=["u1"])
        self.assertEqual(cumulative["weeks"], [])
        self.assertEqual(cumulative["by_project"], {})
