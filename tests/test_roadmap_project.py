"""Tests for the Change -> Project link on roadmap items: assignment, detaching, and
the per-project queries the server uses to guard project deletion and build the
initiative pivot."""

import tempfile
import unittest
from pathlib import Path

from pm_studio import roadmap as roadmap_module
from pm_studio.roadmap import RoadmapStore


class RoadmapProjectLinkTest(unittest.TestCase):
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

    def test_create_with_a_project(self) -> None:
        item = self.store.create("web", "Signup rewrite", project_id="  proj123  ")
        self.assertEqual(item.project_id, "proj123")

    def test_create_without_a_project_leaves_it_unset(self) -> None:
        """A deployment not using the work model keeps behaving exactly as before."""
        self.assertIsNone(self.store.create("web", "Anything").project_id)

    def test_update_assigns_and_detaches(self) -> None:
        item = self.store.create("web", "Signup rewrite", project_id="proj123")
        # None means "no change", matching every other field on update().
        unchanged = self.store.update(item.id, status="in_progress")
        self.assertEqual(unchanged.project_id, "proj123")
        moved = self.store.update(item.id, project_id="proj456")
        self.assertEqual(moved.project_id, "proj456")
        # "" detaches, making the change unaligned.
        detached = self.store.update(item.id, project_id="")
        self.assertIsNone(detached.project_id)

    def test_per_project_queries(self) -> None:
        first = self.store.create("web", "One", project_id="proj123")
        second = self.store.create("platform", "Two", project_id="proj123")
        self.store.create("web", "Elsewhere", project_id="proj456")
        self.assertEqual(self.store.count_by_project("proj123"), 2)
        self.assertEqual(
            [i["id"] for i in self.store.list_by_project("proj123")],
            [first.id, second.id],
        )
        self.assertEqual(self.store.count_by_project("nobody"), 0)

    def test_a_project_spans_products(self) -> None:
        """Product hangs off the Change, not the tree - which is exactly why one
        project (and so one initiative) can cover several products with no
        many-to-many bookkeeping."""
        self.store.create("web", "Web side", project_id="proj123")
        self.store.create("platform", "Platform side", project_id="proj123")
        products = {i["product"] for i in self.store.list_by_project("proj123")}
        self.assertEqual(products, {"web", "platform"})

    def test_unassigned_changes_are_reportable(self) -> None:
        orphan = self.store.create("web", "No parent")
        self.store.create("web", "Parented", project_id="proj123")
        self.assertEqual([i["id"] for i in self.store.unassigned_items()], [orphan.id])

    def test_project_survives_a_reload(self) -> None:
        item = self.store.create("web", "Signup rewrite", project_id="proj123")
        reloaded = RoadmapStore()
        self.assertEqual(reloaded.get(item.id).project_id, "proj123")

    def test_moving_products_keeps_the_project(self) -> None:
        """Re-assigning which product owns a change says nothing about which project it
        belongs to, so the link must survive the move."""
        item = self.store.create("web", "Signup rewrite", project_id="proj123")
        moved = self.store.move(item.id, to_product="platform")
        self.assertEqual(moved.project_id, "proj123")


if __name__ == "__main__":
    unittest.main()
