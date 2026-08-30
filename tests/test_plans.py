"""Tests for the weekly commitment refs: capture-once with a grace window, and the
pure plan-vs-actual classification where every planned item lands in exactly one
bucket."""

import json
import tempfile
import unittest
from pathlib import Path

from pm_studio.costing import week_bounds, week_key
from pm_studio.plans import GRACE_SECONDS, PlanStore, plan_vs_actual

NOW = 1_760_000_000.0


def change(cid, bucket="now", status="pending", title=None):
    return {"id": cid, "title": title or f"Change {cid}", "product": "web",
            "project_id": None, "bucket": bucket, "status": status}


class PlanCaptureTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = PlanStore(Path(self._tmp.name) / "plans")
        self.week = week_key(NOW)
        self.start = week_bounds(self.week)[0]

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_nothing_is_pinned_inside_the_grace_window(self) -> None:
        self.store.ensure_current([change("a")], now=self.start + GRACE_SECONDS - 60)
        self.assertIsNone(self.store.plan_for(week_key(self.start)))

    def test_capture_happens_once_and_records_when(self) -> None:
        at = self.start + GRACE_SECONDS + 3600
        self.store.ensure_current([change("a"), change("b", bucket="next")], now=at)
        plan = self.store.plan_for(week_key(at))
        self.assertEqual([p["id"] for p in plan["planned"]], ["a"])
        self.assertEqual(plan["captured_at"], at)
        # a later call with different state must NOT rewrite the pinned plan
        self.store.ensure_current([change("c")], now=at + 7200)
        plan2 = self.store.plan_for(week_key(at))
        self.assertEqual([p["id"] for p in plan2["planned"]], ["a"])

    def test_done_now_items_are_not_part_of_the_plan(self) -> None:
        at = self.start + GRACE_SECONDS + 60
        self.store.ensure_current([change("a", status="done")], now=at)
        self.assertEqual(self.store.plan_for(week_key(at))["planned"], [])


class PlanVsActualTest(unittest.TestCase):
    def _plan(self, *ids):
        return {"week": "2026-W35", "captured_at": NOW,
                "planned": [{"id": i, "title": i, "product": "web", "project_id": None}
                            for i in ids]}

    def test_every_planned_item_lands_in_exactly_one_bucket(self) -> None:
        result = plan_vs_actual(self._plan("ship", "stay", "move", "gone"), [
            change("ship", status="done"),
            change("stay"),
            change("move", bucket="later"),
            change("new_now"),
        ])
        self.assertEqual(result["planned_total"], 4)
        self.assertEqual(result["shipped"], 1)
        self.assertEqual(result["still_now"], 1)
        self.assertEqual(result["moved"], 1)
        self.assertEqual(result["gone"], 1)
        self.assertEqual(result["added"], 1)
        self.assertEqual(result["shipped_fraction"], 0.25)

    def test_empty_plan_reports_no_fraction(self) -> None:
        result = plan_vs_actual(self._plan(), [change("a")])
        self.assertIsNone(result["shipped_fraction"])
        self.assertEqual(result["added"], 1)


if __name__ == "__main__":
    unittest.main()
