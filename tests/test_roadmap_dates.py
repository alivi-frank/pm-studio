"""Tests for per-change schedule dates: the optional `start_at` / `target_at` pair that
turns the roadmap's timeline from horizon-derived bands into real Gantt bars.

The properties that matter here are the ones a UI cannot enforce for itself: dates are
stored as calendar-date STRINGS (never instants), either one may be absent, an invalid
or inverted pair is refused without half-applying, `is_overdue` is derived at read time
and never persisted, and boards written before this feature existed still load.
"""

import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from pm_studio import roadmap as roadmap_module
from pm_studio.roadmap import RoadmapItem, RoadmapStore, parse_date


def _iso(days_from_today: int) -> str:
    return (date.today() + timedelta(days=days_from_today)).isoformat()


class ParseDateTest(unittest.TestCase):
    def test_blank_clears_and_valid_passes_through(self) -> None:
        self.assertIsNone(parse_date("", "target_at"))
        self.assertIsNone(parse_date("   ", "target_at"))
        self.assertIsNone(parse_date(None, "target_at"))
        self.assertEqual(parse_date(" 2026-09-30 ", "target_at"), "2026-09-30")

    def test_rejects_other_spellings_of_a_real_date(self) -> None:
        """date.fromisoformat alone would accept these. One stored format is the point:
        every reader compares dates as plain strings."""
        for value in ("20260930", "2026-09-30T00:00:00", "30/09/2026", "Sept 30"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError) as caught:
                    parse_date(value, "target_at")
                self.assertIn("target_at", str(caught.exception))

    def test_rejects_a_well_formed_impossible_date(self) -> None:
        with self.assertRaises(ValueError) as caught:
            parse_date("2026-02-30", "start_at")
        self.assertIn("not a real date", str(caught.exception))


class RoadmapDatesTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_dir = roadmap_module.ROADMAP_DIR
        self._orig_products = roadmap_module.PRODUCTS
        roadmap_module.ROADMAP_DIR = Path(self._tmp.name)
        roadmap_module.PRODUCTS = {"web": "Web App", "platform": "Platform"}
        self.store = RoadmapStore()

    def tearDown(self) -> None:
        roadmap_module.ROADMAP_DIR = self._orig_dir
        roadmap_module.PRODUCTS = self._orig_products
        self._tmp.cleanup()

    # ---- the fields themselves ----

    def test_dates_are_optional_and_independent(self) -> None:
        """Each of the four shapes is legal: neither, one, the other, both. An undated
        change is a normal change - the board plans in horizons first."""
        neither = self.store.create("web", "Undated")
        self.assertIsNone(neither.start_at)
        self.assertIsNone(neither.target_at)

        target_only = self.store.create("web", "Milestone", target_at="2026-09-30")
        self.assertIsNone(target_only.start_at)
        self.assertEqual(target_only.target_at, "2026-09-30")

        start_only = self.store.create("web", "Underway", start_at="2026-09-01")
        self.assertEqual(start_only.start_at, "2026-09-01")
        self.assertIsNone(start_only.target_at)

        both = self.store.create("web", "Scheduled", start_at="2026-09-01",
                                 target_at="2026-09-30")
        self.assertEqual((both.start_at, both.target_at), ("2026-09-01", "2026-09-30"))

    def test_update_follows_the_none_means_no_change_convention(self) -> None:
        item = self.store.create("web", "Thing", start_at="2026-09-01",
                                 target_at="2026-09-30")
        # Touching an unrelated field leaves both dates alone.
        kept = self.store.update(item.id, status="in_progress")
        self.assertEqual((kept.start_at, kept.target_at), ("2026-09-01", "2026-09-30"))
        # "" clears one without disturbing the other.
        cleared = self.store.update(item.id, start_at="")
        self.assertIsNone(cleared.start_at)
        self.assertEqual(cleared.target_at, "2026-09-30")

    def test_start_after_target_is_refused(self) -> None:
        with self.assertRaises(ValueError) as caught:
            self.store.create("web", "Backwards", start_at="2026-10-01",
                              target_at="2026-09-01")
        self.assertIn("before it begins", str(caught.exception))

    def test_order_is_checked_against_the_resulting_pair(self) -> None:
        """The regression this guards: a PATCH carrying only `start_at` can invert an
        order the item already had, so the check cannot look at the payload alone."""
        item = self.store.create("web", "Thing", target_at="2026-09-01")
        with self.assertRaises(ValueError):
            self.store.update(item.id, start_at="2026-10-01")

    def test_a_rejected_schedule_leaves_the_item_untouched(self) -> None:
        """Dates are resolved before ANY field is written, so a bad one cannot land a
        half-applied update."""
        item = self.store.create("web", "Thing", target_at="2026-09-01")
        with self.assertRaises(ValueError):
            self.store.update(item.id, title="Renamed", start_at="2026-10-01")
        after = self.store.get(item.id)
        self.assertEqual(after.title, "Thing")
        self.assertIsNone(after.start_at)

    # ---- is_overdue: derived, never stored ----

    def test_overdue_is_past_target_and_still_open(self) -> None:
        late = self.store.create("web", "Late", target_at=_iso(-3))
        self.assertTrue(late.is_overdue)

        due_today = self.store.create("web", "Due today", target_at=_iso(0))
        self.assertFalse(due_today.is_overdue, "a change is not late on its target day")

        upcoming = self.store.create("web", "Soon", target_at=_iso(3))
        self.assertFalse(upcoming.is_overdue)

        undated = self.store.create("web", "Undated")
        self.assertFalse(undated.is_overdue)

    def test_shipping_ends_overdue(self) -> None:
        item = self.store.create("web", "Late", target_at=_iso(-3))
        self.assertTrue(item.is_overdue)
        shipped = self.store.update(item.id, status="done")
        self.assertFalse(shipped.is_overdue)
        # The target survives shipping - the gap between it and shipped_at is the slip,
        # and the timeline draws exactly that.
        self.assertEqual(shipped.target_at, _iso(-3))

    def test_is_overdue_is_public_only_and_never_written_to_disk(self) -> None:
        item = self.store.create("web", "Late", target_at=_iso(-3))
        self.assertNotIn("is_overdue", item.to_dict())
        self.assertTrue(item.to_public_dict()["is_overdue"])

        stored = json.loads((Path(self._tmp.name) / "web.json").read_text())
        self.assertNotIn("is_overdue", stored[0])
        # A stored flag would be wrong by morning; round-tripping proves nothing derived
        # leaked into the file.
        self.assertEqual(RoadmapItem.from_dict(stored[0]).id, item.id)

    def test_reads_hand_out_the_public_shape(self) -> None:
        self.store.create("web", "Late", target_at=_iso(-3))
        for label, rows in (
            ("list_all", self.store.list_all()["web"]),
            ("list_product", self.store.list_product("web")),
        ):
            with self.subTest(read=label):
                self.assertTrue(rows[0]["is_overdue"])
                self.assertEqual(rows[0]["target_at"], _iso(-3))

    def test_upsert_events_carry_the_public_shape(self) -> None:
        """The board renders straight off the websocket payload, so an event missing
        `is_overdue` would drop the flag until the next full reload."""
        events: list[dict] = []
        self.store.subscribe(events.append)
        self.store.create("web", "Late", target_at=_iso(-3))
        self.assertTrue(events[-1]["item"]["is_overdue"])

    # ---- what the PM sees ----

    def test_pm_context_spells_out_the_schedule(self) -> None:
        self.store.create("web", "On time", start_at=_iso(1), target_at=_iso(8))
        deep = self.store.describe_own_product("web")
        self.assertIn(f"starts {_iso(1)}", deep)
        self.assertIn(f"target {_iso(8)}", deep)
        self.assertIn("8d left", deep)

    def test_pm_context_shouts_about_an_overdue_change(self) -> None:
        self.store.create("web", "Late", target_at=_iso(-5))
        deep = self.store.describe_own_product("web")
        self.assertIn("OVERDUE", deep)
        self.assertIn("5d ago", deep)

    def test_other_products_digest_carries_only_the_fact_of_lateness(self) -> None:
        self.store.create("web", "Late", start_at=_iso(-20), target_at=_iso(-5))
        shallow = self.store.describe_other_products("platform")
        self.assertIn("(OVERDUE)", shallow)
        # Someone else's exact dates are noise in an awareness digest.
        self.assertNotIn(_iso(-20), shallow)

    def test_undated_change_adds_nothing_to_the_pm_context(self) -> None:
        self.store.create("web", "Undated")
        deep = self.store.describe_own_product("web")
        self.assertNotIn("target", deep)
        self.assertNotIn("OVERDUE", deep)

    # ---- boards written before this feature ----

    def test_legacy_item_json_loads_without_dates(self) -> None:
        legacy = {
            "id": "abc12345", "product": "web", "title": "Old item", "description": "",
            "bucket": "later", "status": "pending", "origin_product": "web",
            "triaged": True, "created_at": 1.0, "updated_at": 1.0, "shipped_at": None,
        }
        item = RoadmapItem.from_dict(legacy)
        self.assertIsNone(item.start_at)
        self.assertIsNone(item.target_at)
        self.assertFalse(item.is_overdue)

    def test_overdue_follows_the_clock_not_the_stored_value(self) -> None:
        """Same item, two different days, two different answers - which is the whole
        reason the flag is a property rather than a field."""
        item = self.store.create("web", "Thing", target_at="2026-09-30")
        with mock.patch.object(roadmap_module, "date", wraps=date) as clock:
            clock.today.return_value = date(2026, 9, 29)
            self.assertFalse(item.is_overdue)
            clock.today.return_value = date(2026, 10, 1)
            self.assertTrue(item.is_overdue)


if __name__ == "__main__":
    unittest.main()
