"""Tests for external-owner tracking on roadmap items: work done by other people or
teams is carried on the board (additive `owner` field), flagged to PMs as
track-don't-build, and clearable back to built-here."""

import tempfile
import unittest
from pathlib import Path

from pm_studio import roadmap as roadmap_module
from pm_studio.roadmap import RoadmapItem, RoadmapStore


class RoadmapOwnerTest(unittest.TestCase):
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

    def test_create_with_owner_and_clear(self) -> None:
        item = self.store.create("web", "API migration", owner="  Alice's team  ")
        self.assertEqual(item.owner, "Alice's team")
        # None = no change; "" = clear back to built-here.
        unchanged = self.store.update(item.id, status="in_progress")
        self.assertEqual(unchanged.owner, "Alice's team")
        cleared = self.store.update(item.id, owner="")
        self.assertIsNone(cleared.owner)

    def test_owner_flagged_in_pm_context(self) -> None:
        self.store.create("web", "Onboarding redesign", owner="Design team")
        deep = self.store.describe_own_product("web")
        self.assertIn("EXTERNAL - owned by Design team", deep)
        self.assertIn("never dispatch", deep)
        shallow = self.store.describe_other_products("platform")
        self.assertIn("(external: Design team)", shallow)

    def test_legacy_item_json_loads_without_owner(self) -> None:
        """Pre-owner roadmap JSON (no `owner` key) must load unchanged."""
        legacy = {
            "id": "abc12345", "product": "web", "title": "Old item", "description": "",
            "bucket": "later", "status": "pending", "origin_product": "web",
            "triaged": True, "created_at": 1.0, "updated_at": 1.0, "shipped_at": None,
        }
        item = RoadmapItem.from_dict(legacy)
        self.assertIsNone(item.owner)


if __name__ == "__main__":
    unittest.main()
