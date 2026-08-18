"""Tests for initiative-scoped sessions: a session that works IN an initiative spanning
several products, instead of being pinned to one board.

The two axes are the point, so these tests keep them apart:

- `initiative_id` is SCOPE - what the session is about, and where its cost lands.
- `product` + `adopted_products` are AUTHORITY - which boards it may write to.

Breadth is deliberately not authority: an initiative session sees its whole initiative at
full depth from turn one, but starts able to write nowhere, and widens only by adopting a
board explicitly. That is what the allowlist assertions below pin down.
"""

import dataclasses
import tempfile
import threading
import unittest
from pathlib import Path

from pm_studio import agent as agent_module
from pm_studio import roadmap as roadmap_module
from pm_studio.costing import KIND_PM_TURN, CostingStore, current_week, week_bounds
from pm_studio.portfolio import PortfolioError, PortfolioStore
from pm_studio.roadmap import owned_subtrees
from pm_studio.sessions import Session


def _session(**overrides) -> Session:
    """A Session record with only the fields these tests care about set. Built directly
    rather than through SessionManager.create, which would want a git worktree."""
    defaults = dict(
        id="s1",
        name="Test session",
        branch="session/s1",
        worktree_path=None,
        base_branch="main",
        created_at=0.0,
        status="active",
        is_default=False,
    )
    return Session(**{**defaults, **overrides})


class ProductTaxonomyTest(unittest.TestCase):
    """`owned_subtrees` - the union that replaces "one product's subtree" once a session
    can own several roots."""

    def setUp(self) -> None:
        self._orig_products = roadmap_module.PRODUCTS
        self._orig_parents = roadmap_module.PRODUCT_PARENTS
        roadmap_module.PRODUCTS = {
            "web": "Web App",
            "auth": "Auth & Identity",
            "sso": "SSO",
            "billing": "Billing",
            "mobile": "Mobile",
        }
        roadmap_module.PRODUCT_PARENTS = {"auth": "web", "sso": "auth"}

    def tearDown(self) -> None:
        roadmap_module.PRODUCTS = self._orig_products
        roadmap_module.PRODUCT_PARENTS = self._orig_parents

    def test_adopting_a_parent_adopts_everything_below_it(self) -> None:
        self.assertEqual(owned_subtrees(["web"]), ["web", "auth", "sso"])

    def test_unrelated_roots_union_in_display_order(self) -> None:
        """Not adoption order: the context block and the allowlist should read as the
        taxonomy does, however the session got there."""
        self.assertEqual(owned_subtrees(["mobile", "billing"]), ["billing", "mobile"])

    def test_overlapping_subtrees_are_named_once(self) -> None:
        """Adopting a child and then its parent must not grant the child twice - the
        allowlist is built from this list."""
        self.assertEqual(owned_subtrees(["sso", "web"]), ["web", "auth", "sso"])

    def test_unknown_products_are_dropped_not_raised(self) -> None:
        """A board can outlive the config line that named it, and this runs inside a
        PM's turn."""
        self.assertEqual(owned_subtrees(["billing", "gone"]), ["billing"])

    def test_session_owns_its_pinned_product_plus_adoptions(self) -> None:
        session = _session(product="billing", adopted_products=["auth"])
        self.assertEqual(session.owned_products(), ["auth", "sso", "billing"])

    def test_an_initiative_session_starts_owning_nothing(self) -> None:
        session = _session(initiative_id="i1")
        self.assertEqual(session.owned_products(), [])


class AgentScopeTest(unittest.TestCase):
    """What the PM is told, and what it is actually allowed to do, at each scope."""

    def setUp(self) -> None:
        self._orig_products = roadmap_module.PRODUCTS
        self._orig_parents = roadmap_module.PRODUCT_PARENTS
        roadmap_module.PRODUCTS = {
            "web": "Web App",
            "auth": "Auth & Identity",
            "billing": "Billing",
        }
        roadmap_module.PRODUCT_PARENTS = {"auth": "web"}
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        roadmap_module.PRODUCTS = self._orig_products
        roadmap_module.PRODUCT_PARENTS = self._orig_parents
        self._tmp.cleanup()

    def _agent(self, product=None, initiative_id=None, adopted=None, mode="build"):
        session = dataclasses.make_dataclass(
            "StubSession",
            ["id", "product", "initiative_id", "adopted_products", "model", "mode", "worktree_path"],
        )(
            id="s1",
            product=product,
            initiative_id=initiative_id,
            adopted_products=list(adopted or []),
            model="claude-opus-5",
            mode=mode,
            worktree_path=str(self.tmp),
        )
        return agent_module.PMAgent(session, threading.Lock())

    def _patch(self, product: str) -> str:
        return f"Bash(curl -s -X PATCH {agent_module.ROADMAP_BASE_URL}/{product}/*)"

    def test_initiative_session_can_write_to_no_board_yet(self) -> None:
        """The starting state, and the one most likely to be misread as a bug: broad
        context, zero write authority."""
        pm = self._agent(initiative_id="i1")
        for product in ("web", "auth", "billing"):
            self.assertNotIn(self._patch(product), pm.allowed_tools)
        # Suggesting anywhere is still granted - that is the cross-product handoff.
        self.assertIn(
            f"Bash(curl -s -X POST {agent_module.ROADMAP_BASE_URL}/*)", pm.allowed_tools
        )
        self.assertIn("own NO product board", pm.system_prompt)

    def test_adoption_grants_the_board_and_its_children(self) -> None:
        pm = self._agent(initiative_id="i1")
        pm.set_scope(initiative_id="i1", adopted_products=["web"])
        self.assertIn(self._patch("web"), pm.allowed_tools)
        self.assertIn(self._patch("auth"), pm.allowed_tools)
        # The boundary holds: an unadopted board stays read-only.
        self.assertNotIn(self._patch("billing"), pm.allowed_tools)

    def test_adoption_is_reflected_in_the_prompt_not_just_the_allowlist(self) -> None:
        """The two halves of one rule. A PM told it owns a board it cannot write to (or
        handed one it was never told about) is the failure this pairing prevents."""
        pm = self._agent(initiative_id="i1")
        pm.set_scope(initiative_id="i1", adopted_products=["billing"])
        self.assertIn("Boards you have adopted so far", pm.system_prompt)
        self.assertIn("Billing", pm.system_prompt)
        self.assertNotIn("own NO product board", pm.system_prompt)

    def test_release_takes_the_board_back(self) -> None:
        pm = self._agent(initiative_id="i1", adopted=["billing"])
        self.assertIn(self._patch("billing"), pm.allowed_tools)
        pm.set_scope(initiative_id="i1", adopted_products=[])
        self.assertNotIn(self._patch("billing"), pm.allowed_tools)

    def test_only_an_initiative_session_may_change_its_own_scope(self) -> None:
        """A product-pinned session's scope is the stakeholder's to set, so the PM has no
        curl for it at all - not a validation error, an absent capability."""
        scoped = self._agent(initiative_id="i1")
        self.assertIn(f"Bash(curl -s -X POST {scoped.scope_url}*)", scoped.allowed_tools)
        pinned = self._agent(product="billing")
        self.assertNotIn(f"Bash(curl -s -X POST {pinned.scope_url}*)", pinned.allowed_tools)

    def test_a_product_pinned_session_is_told_nothing_about_initiatives(self) -> None:
        """The existing prompt is unchanged for the deployments not using this."""
        pm = self._agent(product="billing")
        self.assertNotIn("working IN an initiative", pm.system_prompt)
        self.assertIn(self._patch("billing"), pm.allowed_tools)

    def test_both_axes_at_once(self) -> None:
        """A session on one board, in service of a wider initiative: it keeps its own
        board's guidance and gains the initiative framing on top."""
        pm = self._agent(product="billing", initiative_id="i1")
        self.assertIn("working IN an initiative", pm.system_prompt)
        self.assertIn(self._patch("billing"), pm.allowed_tools)
        self.assertNotIn(self._patch("web"), pm.allowed_tools)

    def test_the_prompt_never_denies_a_board_it_granted(self) -> None:
        """A session can own a subtree AND an unrelated adopted board. The product
        guidance must not then claim nothing outside that family is writable - the
        allowlist says otherwise, and a prompt arguing with itself is worse than either
        statement alone."""
        pm = self._agent(initiative_id="i1", adopted=["web", "billing"])
        self.assertIn(self._patch("web"), pm.allowed_tools)
        self.assertIn(self._patch("billing"), pm.allowed_tools)
        self.assertNotIn("Nothing outside this family is yours", pm.system_prompt)
        # Both owned boards are named as writable somewhere in the prompt.
        self.assertIn("Billing", pm.system_prompt)
        self.assertIn("Web App", pm.system_prompt)

    def test_an_adopted_board_gets_full_product_guidance(self) -> None:
        """An initiative session with no pinned product still needs to be told how to run
        a board once it owns one - the first adoption becomes its home."""
        pm = self._agent(initiative_id="i1", adopted=["web"])
        self.assertIn("You are the PM for the", pm.system_prompt)
        self.assertIn("has sub-products", pm.system_prompt)


class InitiativeCatchAllTest(unittest.TestCase):
    """The per-initiative catch-all: what stops an initiative-scoped session's cost from
    being billed to maintenance."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.store = PortfolioStore(self.root / "portfolio.json")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_it_is_not_the_global_catch_all(self) -> None:
        """The bug this exists to prevent: the global catch-all hangs off MAINTENANCE, so
        reusing it would attribute another initiative's whole spend there."""
        scaffold = self.store.ensure_maintenance_scaffold()
        initiative = self.store.create_initiative("Unified identity")
        scoped = self.store.ensure_initiative_catch_all(initiative.id)
        self.assertNotEqual(scoped.id, scaffold["project_id"])
        self.assertEqual(scoped.initiative_id, initiative.id)

    def test_it_is_idempotent(self) -> None:
        initiative = self.store.create_initiative("Unified identity")
        first = self.store.ensure_initiative_catch_all(initiative.id)
        second = self.store.ensure_initiative_catch_all(initiative.id)
        self.assertEqual(first.id, second.id)
        self.assertEqual(len(self.store.list_projects()), 1)

    def test_lookup_is_a_pure_read(self) -> None:
        """Called on every recorded activity signal, so it must never create anything."""
        initiative = self.store.create_initiative("Unified identity")
        self.assertIsNone(self.store.catch_all_project_for_initiative(initiative.id))
        self.assertEqual(self.store.list_projects(), [])
        created = self.store.ensure_initiative_catch_all(initiative.id)
        self.assertEqual(
            self.store.catch_all_project_for_initiative(initiative.id), created.id
        )

    def test_unknown_initiative_is_refused(self) -> None:
        with self.assertRaises(PortfolioError):
            self.store.ensure_initiative_catch_all("nope")

    def test_it_survives_a_rename(self) -> None:
        """Identified by its field, not by its title, so renaming the initiative cannot
        orphan the attribution."""
        initiative = self.store.create_initiative("Unified identity")
        created = self.store.ensure_initiative_catch_all(initiative.id)
        self.store.update_initiative(initiative.id, title="Identity, unified")
        self.assertEqual(
            self.store.catch_all_project_for_initiative(initiative.id), created.id
        )

    def test_it_cannot_be_deleted_on_its_own(self) -> None:
        initiative = self.store.create_initiative("Unified identity")
        created = self.store.ensure_initiative_catch_all(initiative.id)
        with self.assertRaises(PortfolioError):
            self.store.delete_project(created.id)

    def test_deleting_the_initiative_takes_it_along(self) -> None:
        """Otherwise every initiative that ever hosted a scoped session would be
        undeletable, blocked by a project nobody created."""
        initiative = self.store.create_initiative("Unified identity")
        self.store.ensure_initiative_catch_all(initiative.id)
        self.store.delete_initiative(initiative.id)
        self.assertEqual(self.store.list_initiatives(), [])
        self.assertEqual(self.store.list_projects(), [])

    def test_a_real_project_still_blocks_deletion(self) -> None:
        """The auto catch-all is the only exemption - a declared project is still a
        reason to refuse."""
        initiative = self.store.create_initiative("Unified identity")
        self.store.ensure_initiative_catch_all(initiative.id)
        self.store.create_project("Real work", initiative_id=initiative.id)
        with self.assertRaises(PortfolioError):
            self.store.delete_initiative(initiative.id)

    def test_cost_rolls_up_to_its_own_initiative(self) -> None:
        """End to end on the number that matters: an initiative-scoped session's spend
        appears under that initiative and NOT under maintenance."""
        week = current_week()
        mid = week_bounds(week)[0] + 3600
        costing = CostingStore(
            path=self.root / "costing.json",
            activity_path=self.root / "activity.jsonl",
            blended_rate=100.0,
        )
        scaffold = self.store.ensure_maintenance_scaffold()
        initiative = self.store.create_initiative("Unified identity")
        scoped = self.store.ensure_initiative_catch_all(initiative.id)

        costing.record("dana", KIND_PM_TURN, project_id=scoped.id, at=mid)
        report = costing.distribute_week(week)
        rollup = costing.rollup_to_initiatives(report["by_project"], self.store)

        self.assertIn(initiative.id, rollup["initiatives"])
        self.assertGreater(rollup["initiatives"][initiative.id]["hours"], 0)
        maintenance_id = self.store.get_project(scaffold["project_id"]).initiative_id
        self.assertNotIn(maintenance_id, rollup["initiatives"])


class InitiativeScopePayloadTest(unittest.TestCase):
    """`initiative_scope` - the join fodder the PM's context block is built from."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = PortfolioStore(Path(self._tmp.name) / "portfolio.json")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_it_carries_the_initiative_its_goals_and_its_projects(self) -> None:
        goal = self.store.create_goal("Grow revenue")
        initiative = self.store.create_initiative("Unified identity", goal_ids=[goal.id])
        project = self.store.create_project("SSO rollout", initiative_id=initiative.id)
        other = self.store.create_initiative("Something else")
        self.store.create_project("Not mine", initiative_id=other.id)

        scope = self.store.initiative_scope(initiative.id)
        self.assertEqual(scope["initiative"]["title"], "Unified identity")
        self.assertEqual([g["title"] for g in scope["goals"]], ["Grow revenue"])
        self.assertEqual([p["id"] for p in scope["projects"]], [project.id])

    def test_an_unknown_initiative_is_none_not_an_error(self) -> None:
        """A session can outlive the initiative it names; every reader treats that as
        unscoped rather than failing every turn."""
        self.assertIsNone(self.store.initiative_scope("gone"))


class InitiativeContextRenderTest(unittest.TestCase):
    """The rendered block, from `describe_initiative`: grouped by project, and every
    change naming its own board - because in this view they differ line to line."""

    def setUp(self) -> None:
        self._orig_dir = roadmap_module.ROADMAP_DIR
        self._orig_products = roadmap_module.PRODUCTS
        self._orig_parents = roadmap_module.PRODUCT_PARENTS
        self._tmp = tempfile.TemporaryDirectory()
        roadmap_module.ROADMAP_DIR = Path(self._tmp.name)
        roadmap_module.PRODUCTS = {"billing": "Billing", "mobile": "Mobile"}
        roadmap_module.PRODUCT_PARENTS = {}
        self.store = roadmap_module.RoadmapStore()

    def tearDown(self) -> None:
        roadmap_module.ROADMAP_DIR = self._orig_dir
        roadmap_module.PRODUCTS = self._orig_products
        roadmap_module.PRODUCT_PARENTS = self._orig_parents
        self._tmp.cleanup()

    def test_changes_from_several_boards_read_under_one_project(self) -> None:
        here = self.store.create("billing", "Invoice API", "Server side")
        there = self.store.create("mobile", "Invoice screen", "Client side")
        block = self.store.describe_initiative(
            "INITIATIVE: \"Unified billing\"",
            [('Project "Invoices"', [here.to_public_dict(), there.to_public_dict()])],
        )
        self.assertIn("Unified billing", block)
        self.assertIn("Invoice API", block)
        self.assertIn("Invoice screen", block)
        # The point of show_product: two changes in one project, two different boards.
        self.assertIn("[on Billing's board]", block)
        self.assertIn("[on Mobile's board]", block)

    def test_an_empty_project_is_stated_not_dropped(self) -> None:
        block = self.store.describe_initiative(
            "INITIATIVE: \"Unified billing\"", [('Project "Invoices"', [])]
        )
        self.assertIn("no open changes", block)

    def test_done_changes_are_left_out(self) -> None:
        shipped = self.store.create("billing", "Old thing", "Done already")
        self.store.update(shipped.id, status="done")
        block = self.store.describe_initiative(
            "INITIATIVE: \"Unified billing\"",
            [('Project "Invoices"', [self.store.get(shipped.id).to_public_dict()])],
        )
        self.assertNotIn("Old thing", block)


class DigestExclusionTest(unittest.TestCase):
    """A board read at full depth must not also appear in the awareness digest - now that
    "full depth" can mean several unrelated boards."""

    def setUp(self) -> None:
        self._orig_dir = roadmap_module.ROADMAP_DIR
        self._orig_products = roadmap_module.PRODUCTS
        self._orig_parents = roadmap_module.PRODUCT_PARENTS
        self._tmp = tempfile.TemporaryDirectory()
        roadmap_module.ROADMAP_DIR = Path(self._tmp.name)
        roadmap_module.PRODUCTS = {
            "web": "Web App",
            "auth": "Auth & Identity",
            "billing": "Billing",
            "mobile": "Mobile",
        }
        roadmap_module.PRODUCT_PARENTS = {"auth": "web"}
        self.store = roadmap_module.RoadmapStore()
        for product in roadmap_module.PRODUCTS:
            self.store.create(product, f"{product} thing", "...")

    def tearDown(self) -> None:
        roadmap_module.ROADMAP_DIR = self._orig_dir
        roadmap_module.PRODUCTS = self._orig_products
        roadmap_module.PRODUCT_PARENTS = self._orig_parents
        self._tmp.cleanup()

    def test_several_owned_boards_are_all_excluded(self) -> None:
        digest = self.store.describe_other_products(["billing", "web"])
        self.assertNotIn("billing thing", digest)
        self.assertNotIn("web thing", digest)
        # A child of an owned board is covered at full depth too, so it stays out.
        self.assertNotIn("auth thing", digest)
        self.assertIn("mobile thing", digest)

    def test_one_product_string_behaves_exactly_as_before(self) -> None:
        digest = self.store.describe_other_products("web")
        self.assertNotIn("web thing", digest)
        self.assertNotIn("auth thing", digest)
        self.assertIn("billing thing", digest)
        self.assertIn("mobile thing", digest)

    def test_owning_nothing_shows_everything(self) -> None:
        digest = self.store.describe_other_products("")
        for product in roadmap_module.PRODUCTS:
            self.assertIn(f"{product} thing", digest)

    def test_changes_already_shown_at_full_depth_are_not_repeated(self) -> None:
        """An initiative's changes are read at full depth wherever they live, including on
        boards the session doesn't own - so they must not come straight back as digest
        one-liners."""
        shown = [i["id"] for i in self.store.list_product("billing")]
        digest = self.store.describe_other_products("", exclude_item_ids=shown)
        self.assertNotIn("billing thing", digest)
        self.assertIn("mobile thing", digest)

    def test_a_board_with_nothing_left_to_report_drops_out(self) -> None:
        every_id = [
            i["id"]
            for product in roadmap_module.PRODUCTS
            for i in self.store.list_product(product)
        ]
        self.assertEqual(self.store.describe_other_products("", exclude_item_ids=every_id), "")


if __name__ == "__main__":
    unittest.main()
