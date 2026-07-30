"""Tests for the initiative pivot: the same changes regrouped as
Initiative -> Project -> Change.

The invariant that matters is that switching lens never loses or duplicates a change.
The per-product board can show everything because every change has a product; the
initiative lens has two ways to fall off the tree (no project, or a project with no
initiative), so both are caught in a trailing group instead of vanishing.
"""

import tempfile
import unittest
from pathlib import Path

from pm_studio.portfolio import PortfolioStore


def change(change_id: str, product: str, project_id=None, status: str = "pending") -> dict:
    """A roadmap item as the store serializes it - only the keys the pivot reads."""
    return {
        "id": change_id,
        "product": product,
        "title": f"Change {change_id}",
        "status": status,
        "project_id": project_id,
    }


class InitiativePivotTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = PortfolioStore(Path(self._tmp.name) / "portfolio.json")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _ids(self, groups) -> list[str]:
        return [
            c["id"]
            for group in groups
            for entry in group["projects"]
            for c in entry["changes"]
        ]

    def test_changes_group_under_initiative_then_project(self) -> None:
        initiative = self.store.create_initiative("Onboarding")
        project = self.store.create_project("Signup", initiative_id=initiative.id)
        groups = self.store.group_changes_by_initiative(
            [change("a", "web", project.id), change("b", "web", project.id)]
        )
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["initiative"]["id"], initiative.id)
        self.assertEqual(len(groups[0]["projects"]), 1)
        self.assertEqual(groups[0]["projects"][0]["project"]["id"], project.id)
        self.assertEqual(self._ids(groups), ["a", "b"])

    def test_one_initiative_spans_products(self) -> None:
        """The whole point of this lens - the per-product board structurally cannot
        show that a single initiative covers several products."""
        initiative = self.store.create_initiative("Onboarding")
        project = self.store.create_project("Signup", initiative_id=initiative.id)
        groups = self.store.group_changes_by_initiative(
            [change("a", "web", project.id), change("b", "platform", project.id)]
        )
        products = {c["product"] for c in groups[0]["projects"][0]["changes"]}
        self.assertEqual(products, {"web", "platform"})

    def test_an_initiative_with_no_changes_still_appears(self) -> None:
        """An empty initiative is information (nothing is happening on it), so it must
        not be hidden just because it has no changes yet."""
        initiative = self.store.create_initiative("Not started")
        self.store.create_project("Planned", initiative_id=initiative.id)
        groups = self.store.group_changes_by_initiative([])
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["projects"][0]["changes"], [])

    def test_a_project_with_no_initiative_lands_in_the_unaligned_group(self) -> None:
        orphan = self.store.create_project("Loose end")
        groups = self.store.group_changes_by_initiative([change("a", "web", orphan.id)])
        self.assertIsNone(groups[-1]["initiative"])
        self.assertEqual(groups[-1]["projects"][0]["project"]["id"], orphan.id)
        self.assertEqual(self._ids(groups), ["a"])

    def test_a_change_with_no_project_lands_in_the_unaligned_group(self) -> None:
        groups = self.store.group_changes_by_initiative([change("a", "web", None)])
        self.assertEqual(len(groups), 1)
        self.assertIsNone(groups[0]["initiative"])
        self.assertIsNone(groups[0]["projects"][0]["project"])
        self.assertEqual(self._ids(groups), ["a"])

    def test_a_change_pointing_at_a_deleted_project_is_not_lost(self) -> None:
        """A dangling project_id must degrade to "unassigned" rather than silently
        dropping the change off the board."""
        groups = self.store.group_changes_by_initiative([change("a", "web", "gone")])
        self.assertIsNone(groups[0]["projects"][0]["project"])
        self.assertEqual(self._ids(groups), ["a"])

    def test_loose_projects_and_loose_changes_share_one_group(self) -> None:
        """Two kinds of unaligned must not render as two separate 'Unaligned'
        headings."""
        orphan = self.store.create_project("Loose end")
        groups = self.store.group_changes_by_initiative(
            [change("a", "web", orphan.id), change("b", "web", None)]
        )
        unaligned = [g for g in groups if g["initiative"] is None]
        self.assertEqual(len(unaligned), 1)
        self.assertEqual(len(unaligned[0]["projects"]), 2)
        self.assertEqual(sorted(self._ids(groups)), ["a", "b"])

    def test_every_change_appears_exactly_once(self) -> None:
        """The invariant: switching lens must never lose or duplicate work."""
        goal = self.store.create_goal("Grow")
        first = self.store.create_initiative("One", goal_ids=[goal.id])
        second = self.store.create_initiative("Two")
        aligned = self.store.create_project("Aligned", initiative_id=first.id)
        other = self.store.create_project("Other", initiative_id=second.id)
        orphan = self.store.create_project("Orphan")
        boot = self.store.ensure_maintenance_scaffold()

        changes = [
            change("a", "web", aligned.id),
            change("b", "platform", aligned.id),
            change("c", "web", other.id),
            change("d", "web", orphan.id),
            change("e", "web", None),
            change("f", "web", boot["project_id"]),
            change("g", "web", "dangling"),
        ]
        groups = self.store.group_changes_by_initiative(changes)
        seen = self._ids(groups)
        self.assertEqual(sorted(seen), ["a", "b", "c", "d", "e", "f", "g"])
        self.assertEqual(len(seen), len(set(seen)))

    def test_empty_portfolio_puts_everything_under_unassigned(self) -> None:
        """A deployment that never touched /portfolio still gets a coherent lens."""
        groups = self.store.group_changes_by_initiative(
            [change("a", "web"), change("b", "platform")]
        )
        self.assertEqual(len(groups), 1)
        self.assertIsNone(groups[0]["initiative"])
        self.assertEqual(sorted(self._ids(groups)), ["a", "b"])

    def test_no_portfolio_and_no_changes_is_empty(self) -> None:
        self.assertEqual(self.store.group_changes_by_initiative([]), [])

    def test_groups_expose_the_flags_the_ui_renders(self) -> None:
        boot = self.store.ensure_maintenance_scaffold()
        groups = self.store.group_changes_by_initiative(
            [change("a", "web", boot["project_id"])]
        )
        self.assertTrue(groups[0]["initiative"]["is_maintenance"])
        self.assertFalse(groups[0]["initiative"]["is_unaligned"])
        self.assertTrue(groups[0]["projects"][0]["project"]["is_catch_all"])


if __name__ == "__main__":
    unittest.main()
