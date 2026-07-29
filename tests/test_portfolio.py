"""Tests for the work model: Goals <-> Initiative -> Project -> Change.

The shape is the point, so these tests pin the cardinalities directly: a project has
exactly one initiative (or none, which is the reportable "unaligned" state), an
initiative may serve several goals, and the rollup path a change inherits is unique -
which is what makes cost attribution unambiguous later.
"""

import tempfile
import unittest
from pathlib import Path

from pm_studio.portfolio import (
    DEFAULT_CATCH_ALL_PROJECT,
    PortfolioError,
    PortfolioStore,
)
from pm_studio.roadmap import RoadmapItem


class PortfolioShapeTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "portfolio.json"
        self.store = PortfolioStore(self.path)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # ---- cardinality ----

    def test_initiative_can_serve_several_goals(self) -> None:
        """The only many-to-many edge in the model."""
        growth = self.store.create_goal("Grow revenue")
        retention = self.store.create_goal("Keep customers")
        initiative = self.store.create_initiative(
            "Onboarding overhaul", goal_ids=[growth.id, retention.id]
        )
        self.assertEqual(initiative.goal_ids, [growth.id, retention.id])
        self.assertFalse(initiative.is_unaligned)

    def test_duplicate_goal_links_are_collapsed(self) -> None:
        goal = self.store.create_goal("Grow revenue")
        initiative = self.store.create_initiative("Onboarding", goal_ids=[goal.id, goal.id])
        self.assertEqual(initiative.goal_ids, [goal.id])

    def test_project_has_exactly_one_initiative(self) -> None:
        initiative = self.store.create_initiative("Onboarding overhaul")
        project = self.store.create_project("Signup rewrite", initiative_id=initiative.id)
        self.assertEqual(project.initiative_id, initiative.id)
        self.assertFalse(project.is_unaligned)

    def test_unknown_parents_are_rejected(self) -> None:
        with self.assertRaises(PortfolioError):
            self.store.create_initiative("Bad", goal_ids=["nope"])
        with self.assertRaises(PortfolioError):
            self.store.create_project("Bad", initiative_id="nope")

    def test_blank_title_rejected(self) -> None:
        with self.assertRaises(PortfolioError):
            self.store.create_goal("   ")

    # ---- unaligned is a state, not an error ----

    def test_a_project_with_no_initiative_is_unaligned_but_allowed(self) -> None:
        """Alignment is mandatory as a practice; blocking the work that created the gap
        would just make people invent junk parents."""
        project = self.store.create_project("Quick fix")
        self.assertTrue(project.is_unaligned)
        report = self.store.unaligned_report()
        self.assertEqual([p["id"] for p in report["projects"]], [project.id])

    def test_an_initiative_serving_no_goal_is_unaligned(self) -> None:
        initiative = self.store.create_initiative("Floating work")
        report = self.store.unaligned_report()
        self.assertEqual([i["id"] for i in report["initiatives"]], [initiative.id])

    def test_closed_items_drop_out_of_the_unaligned_report(self) -> None:
        project = self.store.create_project("Quick fix")
        self.store.update_project(project.id, status="closed")
        self.assertEqual(self.store.unaligned_report()["projects"], [])

    def test_a_project_can_be_deliberately_unaligned_again(self) -> None:
        initiative = self.store.create_initiative("Onboarding")
        project = self.store.create_project("Signup", initiative_id=initiative.id)
        detached = self.store.update_project(project.id, clear_initiative=True)
        self.assertTrue(detached.is_unaligned)

    # ---- rollup ----

    def test_rollup_path_is_unique_and_stops_at_the_initiative(self) -> None:
        """Change -> Project -> Initiative is single-parent, so those totals are exact.
        Goals are excluded on purpose: an initiative can serve several, so folding them
        in here would invite double-counting."""
        goal_a = self.store.create_goal("A")
        goal_b = self.store.create_goal("B")
        initiative = self.store.create_initiative("I", goal_ids=[goal_a.id, goal_b.id])
        project = self.store.create_project("P", initiative_id=initiative.id)

        path = self.store.rollup_path(project.id)
        self.assertEqual(path, {"project_id": project.id, "initiative_id": initiative.id})
        self.assertNotIn("goal_ids", path)
        # Goals are reachable, but only through a separate call.
        self.assertEqual(
            self.store.goal_ids_for_initiative(initiative.id), [goal_a.id, goal_b.id]
        )

    def test_rollup_path_of_an_unaligned_or_unknown_project(self) -> None:
        orphan = self.store.create_project("Orphan")
        self.assertEqual(
            self.store.rollup_path(orphan.id),
            {"project_id": orphan.id, "initiative_id": None},
        )
        self.assertEqual(
            self.store.rollup_path(None), {"project_id": None, "initiative_id": None}
        )
        self.assertEqual(
            self.store.rollup_path("does-not-exist"),
            {"project_id": None, "initiative_id": None},
        )

    def test_projects_of_initiative(self) -> None:
        initiative = self.store.create_initiative("I")
        first = self.store.create_project("One", initiative_id=initiative.id)
        second = self.store.create_project("Two", initiative_id=initiative.id)
        self.store.create_project("Elsewhere")
        self.assertEqual(
            self.store.projects_of_initiative(initiative.id), [first.id, second.id]
        )

    # ---- delete guards ----

    def test_cannot_delete_a_goal_an_initiative_still_serves(self) -> None:
        goal = self.store.create_goal("Grow revenue")
        self.store.create_initiative("Onboarding", goal_ids=[goal.id])
        with self.assertRaises(PortfolioError) as ctx:
            self.store.delete_goal(goal.id)
        self.assertIn("Onboarding", str(ctx.exception))

    def test_cannot_delete_an_initiative_with_projects(self) -> None:
        initiative = self.store.create_initiative("Onboarding")
        self.store.create_project("Signup", initiative_id=initiative.id)
        with self.assertRaises(PortfolioError):
            self.store.delete_initiative(initiative.id)

    def test_cannot_delete_a_project_that_still_has_changes(self) -> None:
        project = self.store.create_project("Signup")
        with self.assertRaises(PortfolioError):
            self.store.delete_project(project.id, change_count=3)
        # With nothing under it, the same call succeeds.
        self.store.delete_project(project.id, change_count=0)
        self.assertIsNone(self.store.get_project(project.id))

    def test_unlinking_frees_a_goal_for_deletion(self) -> None:
        goal = self.store.create_goal("Grow revenue")
        initiative = self.store.create_initiative("Onboarding", goal_ids=[goal.id])
        self.store.update_initiative(initiative.id, goal_ids=[])
        self.store.delete_goal(goal.id)
        self.assertIsNone(self.store.get_goal(goal.id))

    # ---- the maintenance scaffold ----

    def test_bootstrap_creates_the_aligned_catch_all_trio(self) -> None:
        result = self.store.ensure_maintenance_scaffold()
        self.assertTrue(result["created"])
        project = self.store.get_project(result["project_id"])
        initiative = self.store.get_initiative(result["initiative_id"])
        self.assertTrue(project.is_catch_all)
        self.assertEqual(project.title, DEFAULT_CATCH_ALL_PROJECT)
        # The whole point: the catch-all is itself aligned, so unplanned work is not
        # merely parked - it rolls up.
        self.assertEqual(project.initiative_id, initiative.id)
        self.assertEqual(initiative.goal_ids, [result["goal_id"]])
        self.assertFalse(project.is_unaligned)
        self.assertFalse(initiative.is_unaligned)
        self.assertEqual(self.store.unaligned_report()["projects"], [])

    def test_bootstrap_is_idempotent(self) -> None:
        first = self.store.ensure_maintenance_scaffold()
        second = self.store.ensure_maintenance_scaffold()
        self.assertFalse(second["created"])
        self.assertEqual(first["project_id"], second["project_id"])
        self.assertEqual(len(self.store.list_projects()), 1)

    def test_bootstrap_accepts_deployment_chosen_names(self) -> None:
        """The package must not impose one company's vocabulary."""
        result = self.store.ensure_maintenance_scaffold(
            goal_title="Stay operable",
            initiative_title="Run the business",
            project_title="Ad hoc",
        )
        self.assertEqual(self.store.get_project(result["project_id"]).title, "Ad hoc")
        self.assertEqual(
            self.store.get_initiative(result["initiative_id"]).title, "Run the business"
        )

    def test_only_one_catch_all(self) -> None:
        self.store.ensure_maintenance_scaffold()
        with self.assertRaises(PortfolioError):
            self.store.create_project("Second catch-all", is_catch_all=True)

    def test_the_catch_all_and_its_initiative_cannot_be_removed_or_closed(self) -> None:
        """Unplanned work must always have somewhere aligned to land."""
        result = self.store.ensure_maintenance_scaffold()
        with self.assertRaises(PortfolioError):
            self.store.delete_project(result["project_id"])
        with self.assertRaises(PortfolioError):
            self.store.update_project(result["project_id"], status="closed")
        with self.assertRaises(PortfolioError):
            self.store.delete_initiative(result["initiative_id"])
        with self.assertRaises(PortfolioError):
            self.store.update_initiative(result["initiative_id"], status="closed")

    # ---- lifecycle + persistence ----

    def test_closing_stamps_and_reopening_clears(self) -> None:
        goal = self.store.create_goal("Grow revenue")
        closed = self.store.update_goal(goal.id, status="closed")
        self.assertIsNotNone(closed.closed_at)
        reopened = self.store.update_goal(goal.id, status="open")
        self.assertIsNone(reopened.closed_at)

    def test_unknown_status_rejected(self) -> None:
        goal = self.store.create_goal("Grow revenue")
        with self.assertRaises(PortfolioError):
            self.store.update_goal(goal.id, status="paused")

    def test_survives_a_restart(self) -> None:
        goal = self.store.create_goal("Grow revenue")
        initiative = self.store.create_initiative("Onboarding", goal_ids=[goal.id])
        project = self.store.create_project("Signup", initiative_id=initiative.id)
        reloaded = PortfolioStore(self.path)
        self.assertEqual(
            reloaded.rollup_path(project.id),
            {"project_id": project.id, "initiative_id": initiative.id},
        )
        self.assertEqual(reloaded.goal_ids_for_initiative(initiative.id), [goal.id])

    def test_empty_store_snapshot(self) -> None:
        self.assertTrue(self.store.is_empty)
        snapshot = self.store.snapshot()
        self.assertEqual(snapshot["goals"], [])
        self.assertIsNone(snapshot["catch_all_project_id"])

    def test_subscribers_are_notified(self) -> None:
        events = []
        self.store.subscribe(events.append)
        self.store.create_goal("Grow revenue")
        self.assertEqual(events[0]["entity"], "goal")


class ChangeBelongsToOneProjectTest(unittest.TestCase):
    """A Change is the existing roadmap item - this PR adds a single project_id to it
    rather than inventing a parallel concept."""

    def test_legacy_item_json_loads_without_project_id(self) -> None:
        """Existing boards must keep loading: this is additive, not a migration."""
        legacy = {
            "id": "abc12345", "product": "web", "title": "Old item", "description": "",
            "bucket": "later", "status": "pending", "origin_product": "web",
            "triaged": True, "created_at": 1.0, "updated_at": 1.0, "shipped_at": None,
            "owner": None,
        }
        item = RoadmapItem.from_dict(legacy)
        self.assertIsNone(item.project_id)


if __name__ == "__main__":
    unittest.main()
