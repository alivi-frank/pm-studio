"""Tests for the system layer: the bounded piece of technology a change is contained
within, and the many-to-many edge between products and systems.

The system layer exists because `[products]` was carrying two different kinds of thing.
A product is business-facing and owns a roadmap board; a system is code and owns none.
Four things here would silently rot, and they are what these tests pin down:

1. **A deployment that declares no [systems] behaves exactly as it did before the layer
   existed.** That is the whole back-compat promise, and it is one `systems_declared()`
   switch away from being broken in a dozen places.
2. **Attribution is enforced where it is claimed to be.** Required on create, constrained
   to what the product touches, never clearable, and carried correctly through a move -
   the one operation that can invalidate an attribution that was valid a moment ago.
3. **A product declaring no systems is not deadlocked.** Enforcing an empty list would
   reject every possible value and make that board unusable, so it widens instead.
4. **Reclassifying a product into a system loses nothing.** The id may sit in both tables
   while its board is drained, because dropping the [products] entry first would orphan
   the board file - board files load only for declared product ids.
"""

import tempfile
import textwrap
import unittest
from pathlib import Path

from pm_studio import agent as agent_module
from pm_studio import roadmap as roadmap_module
from pm_studio.config import SystemSpec, load_config
from pm_studio.roadmap import RoadmapStore


class SystemConfigTest(unittest.TestCase):
    """[systems] parsing and the product -> systems edge - the operator-facing contract."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "pm_studio_local").mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write(self, config: str):
        (self.root / "pm_studio_local" / "config.toml").write_text(textwrap.dedent(config))
        return load_config(self.root)

    def test_absent_table_leaves_the_layer_dormant(self) -> None:
        """The pre-system deployment, which is every existing one."""
        cfg = self._write("""\
            [products]
            web = "Web App"
        """)
        self.assertEqual(cfg.systems, {})
        self.assertEqual(cfg.product_systems, {})
        self.assertEqual(cfg.transitional_ids, ())

    def test_both_spellings_and_full_metadata(self) -> None:
        """A bare label and a table mean the same thing, as in [products]."""
        cfg = self._write("""\
            [systems]
            claims = "Claims Processor"

            [systems.rides]
            label = "Rides & Logistics"
            path = "services/rides"
            repo = "github.com/org/rides"
            guidance = "docs/rides.md"
            pipelines = ["rides-ci", "rides-nightly"]
        """)
        self.assertEqual(cfg.systems["claims"], SystemSpec(label="Claims Processor"))
        self.assertEqual(
            cfg.systems["rides"],
            SystemSpec(
                label="Rides & Logistics",
                path="services/rides",
                repo="github.com/org/rides",
                guidance="docs/rides.md",
                pipelines=("rides-ci", "rides-nightly"),
            ),
        )
        # Declaration order is display order, same convention as products.
        self.assertEqual(list(cfg.systems), ["claims", "rides"])

    def test_missing_label_falls_back_to_the_id(self) -> None:
        """Cosmetic, not structural - the same call [products] makes."""
        cfg = self._write("""\
            [systems.rides]
            path = "services/rides"
        """)
        self.assertEqual(cfg.systems["rides"].label, "rides")

    def test_product_declares_the_systems_it_touches(self) -> None:
        cfg = self._write("""\
            [products.checkout]
            label = "Checkout"
            systems = ["claims", "rides"]

            [systems]
            claims = "Claims Processor"
            rides = "Rides"
        """)
        self.assertEqual(cfg.product_systems["checkout"], ("claims", "rides"))

    def test_duplicate_reference_is_deduped_keeping_first_position(self) -> None:
        """A repeated id is a typo, not a structural error - order is the operator's."""
        cfg = self._write("""\
            [products.checkout]
            label = "Checkout"
            systems = ["rides", "claims", "rides"]

            [systems]
            claims = "Claims Processor"
            rides = "Rides"
        """)
        self.assertEqual(cfg.product_systems["checkout"], ("rides", "claims"))

    def test_many_to_many_in_both_directions(self) -> None:
        """The point of the layer: one system serving two products, one product built on
        two systems. No association records - each side declares, the reverse derives."""
        cfg = self._write("""\
            [products.checkout]
            label = "Checkout"
            systems = ["claims", "rides"]

            [products.portal]
            label = "Portal"
            systems = ["claims"]

            [systems]
            claims = "Claims Processor"
            rides = "Rides"
        """)
        self.assertEqual(cfg.product_systems["checkout"], ("claims", "rides"))
        self.assertEqual(cfg.product_systems["portal"], ("claims",))

    def test_unknown_system_reference_is_fatal(self) -> None:
        """Same reason a bad `parent` is: a product silently touching nothing is a
        working-looking deployment with an unenforceable constraint."""
        with self.assertRaises(SystemExit) as caught:
            self._write("""\
                [products.checkout]
                label = "Checkout"
                systems = ["clams"]

                [systems]
                claims = "Claims Processor"
            """)
        self.assertEqual(caught.exception.code, 2)

    def test_systems_as_a_string_is_fatal(self) -> None:
        with self.assertRaises(SystemExit):
            self._write("""\
                [products.checkout]
                label = "Checkout"
                systems = "claims"

                [systems]
                claims = "Claims Processor"
            """)

    def test_pipelines_must_be_an_array(self) -> None:
        with self.assertRaises(SystemExit):
            self._write("""\
                [systems.rides]
                label = "Rides"
                pipelines = "rides-ci"
            """)

    def test_system_parent_is_fatal_rather_than_ignored(self) -> None:
        """`parent` is meaningful in the sibling table, so accepting and dropping it would
        silently discard a relationship the operator thought they declared."""
        with self.assertRaises(SystemExit):
            self._write("""\
                [systems]
                claims = "Claims Processor"
                sub = { label = "Sub", parent = "claims" }
            """)

    def test_product_parent_pointing_at_a_system_is_fatal(self) -> None:
        """The predictable confusion once both tables exist."""
        with self.assertRaises(SystemExit):
            self._write("""\
                [products]
                checkout = { label = "Checkout", parent = "claims" }

                [systems]
                claims = "Claims Processor"
            """)

    def test_an_id_in_both_tables_is_transitional_not_fatal(self) -> None:
        """Reclassifying a product into a system. Legal on purpose: its board must keep
        loading while its changes are re-homed."""
        cfg = self._write("""\
            [products]
            web = "Web App"
            claims = "Claims Processor"

            [systems]
            claims = "Claims Processor"
        """)
        self.assertEqual(cfg.transitional_ids, ("claims",))
        # And it is fully both: still a board-owning product, already a system.
        self.assertIn("claims", cfg.products)
        self.assertIn("claims", cfg.systems)


class SystemHelpersTest(unittest.TestCase):
    """The module-level taxonomy helpers."""

    def setUp(self) -> None:
        self._orig = (
            roadmap_module.PRODUCTS,
            roadmap_module.SYSTEMS,
            roadmap_module.PRODUCT_SYSTEMS,
        )
        roadmap_module.PRODUCTS = {"checkout": "Checkout", "portal": "Portal", "ops": "Ops"}
        roadmap_module.SYSTEMS = {
            "claims": SystemSpec(label="Claims Processor", path="services/claims"),
            "rides": SystemSpec(label="Rides"),
        }
        roadmap_module.PRODUCT_SYSTEMS = {
            "checkout": ("claims", "rides"),
            "portal": ("claims",),
        }

    def tearDown(self) -> None:
        (
            roadmap_module.PRODUCTS,
            roadmap_module.SYSTEMS,
            roadmap_module.PRODUCT_SYSTEMS,
        ) = self._orig

    def test_declared_switch(self) -> None:
        self.assertTrue(roadmap_module.systems_declared())

    def test_label_falls_back_to_the_id(self) -> None:
        """A change can outlive the config line that declared its system."""
        self.assertEqual(roadmap_module.system_label("claims"), "Claims Processor")
        self.assertEqual(roadmap_module.system_label("gone"), "gone")

    def test_reverse_edge_is_derived_in_product_display_order(self) -> None:
        self.assertEqual(roadmap_module.products_of_system("claims"), ["checkout", "portal"])
        self.assertEqual(roadmap_module.products_of_system("rides"), ["checkout"])
        self.assertEqual(roadmap_module.products_of_system("gone"), [])

    def test_systems_of_product_is_not_inherited(self) -> None:
        self.assertEqual(roadmap_module.systems_of_product("checkout"), ["claims", "rides"])
        self.assertEqual(roadmap_module.systems_of_product("ops"), [])

    def test_products_missing_systems_is_the_config_gap(self) -> None:
        self.assertEqual(roadmap_module.products_missing_systems(), ["ops"])


class DormantLayerTest(unittest.TestCase):
    """No [systems] at all: every behaviour must be the pre-system one."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = (
            roadmap_module.ROADMAP_DIR,
            roadmap_module.PRODUCTS,
            roadmap_module.SYSTEMS,
            roadmap_module.PRODUCT_SYSTEMS,
        )
        roadmap_module.ROADMAP_DIR = Path(self._tmp.name)
        roadmap_module.PRODUCTS = {"web": "Web App"}
        roadmap_module.SYSTEMS = {}
        roadmap_module.PRODUCT_SYSTEMS = {}
        self.store = RoadmapStore()

    def tearDown(self) -> None:
        (
            roadmap_module.ROADMAP_DIR,
            roadmap_module.PRODUCTS,
            roadmap_module.SYSTEMS,
            roadmap_module.PRODUCT_SYSTEMS,
        ) = self._orig
        self._tmp.cleanup()

    def test_create_needs_no_system(self) -> None:
        item = self.store.create("web", "Ship it")
        self.assertIsNone(item.system)

    def test_naming_a_system_is_refused(self) -> None:
        """Honest rather than quietly ignored: there is no taxonomy to attribute to."""
        with self.assertRaises(ValueError):
            self.store.create("web", "Ship it", system="claims")

    def test_nothing_is_reported_as_unattributed(self) -> None:
        self.store.create("web", "Ship it")
        report = self.store.unattributed_report()
        self.assertEqual(report["count"], 0)
        self.assertEqual(report["changes"], [])

    def test_context_block_says_nothing_about_systems(self) -> None:
        self.store.create("web", "Ship it")
        self.assertNotIn("system", self.store.describe_own_product("web").lower())


class AttributionTest(unittest.TestCase):
    """Attribution enforced on the store, which is where every caller goes through."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = (
            roadmap_module.ROADMAP_DIR,
            roadmap_module.PRODUCTS,
            roadmap_module.SYSTEMS,
            roadmap_module.PRODUCT_SYSTEMS,
        )
        roadmap_module.ROADMAP_DIR = Path(self._tmp.name)
        roadmap_module.PRODUCTS = {"checkout": "Checkout", "portal": "Portal", "ops": "Ops"}
        roadmap_module.SYSTEMS = {
            "claims": SystemSpec(label="Claims Processor"),
            "rides": SystemSpec(label="Rides"),
        }
        roadmap_module.PRODUCT_SYSTEMS = {
            "checkout": ("claims", "rides"),
            "portal": ("claims",),
        }
        self.store = RoadmapStore()

    def tearDown(self) -> None:
        (
            roadmap_module.ROADMAP_DIR,
            roadmap_module.PRODUCTS,
            roadmap_module.SYSTEMS,
            roadmap_module.PRODUCT_SYSTEMS,
        ) = self._orig
        self._tmp.cleanup()

    def test_create_requires_a_system(self) -> None:
        with self.assertRaises(ValueError) as caught:
            self.store.create("checkout", "Ship it")
        # The message has to name the valid ids, or the PM retries the same payload.
        self.assertIn("claims", str(caught.exception))

    def test_create_with_a_valid_system(self) -> None:
        item = self.store.create("checkout", "Ship it", system="claims")
        self.assertEqual(item.system, "claims")

    def test_undeclared_system_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            self.store.create("checkout", "Ship it", system="nope")

    def test_system_the_product_does_not_touch_is_refused(self) -> None:
        """The edge earns its keep here: `rides` exists, but Portal is not built on it."""
        with self.assertRaises(ValueError) as caught:
            self.store.create("portal", "Ship it", system="rides")
        self.assertIn("claims", str(caught.exception))

    def test_a_product_declaring_no_systems_is_out_of_scope(self) -> None:
        """Attribution is scoped per product, which is what makes the layer adoptable one
        product at a time. Ops declares nothing, so Ops requires nothing - the alternative
        (requiring it everywhere the moment any system exists) forces every board to
        attribute to whichever systems happen to be declared yet, inventing wrong data."""
        self.assertFalse(roadmap_module.requires_system("ops"))
        item = self.store.create("ops", "Ship it")
        self.assertIsNone(item.system)

    def test_an_out_of_scope_product_still_accepts_an_explicit_system(self) -> None:
        """Permissive rather than refused: attributing while the edge is undeclared is
        useful and harmless, and this codebase reports gaps instead of blocking work."""
        item = self.store.create("ops", "Ship it", system="rides")
        self.assertEqual(item.system, "rides")

    def test_scope_follows_the_declaration_not_the_deployment(self) -> None:
        self.assertTrue(roadmap_module.requires_system("checkout"))
        self.assertTrue(roadmap_module.requires_system("portal"))
        self.assertFalse(roadmap_module.requires_system("ops"))

    def test_update_reattributes(self) -> None:
        item = self.store.create("checkout", "Ship it", system="claims")
        self.assertEqual(self.store.update(item.id, system="rides").system, "rides")

    def test_update_cannot_clear_the_attribution(self) -> None:
        """No ""-clears convention, unlike owner/project_id: clearing would manufacture
        the very inconsistency the layer exists to remove."""
        item = self.store.create("checkout", "Ship it", system="claims")
        with self.assertRaises(ValueError):
            self.store.update(item.id, system="")
        self.assertEqual(self.store.get(item.id).system, "claims")

    def test_update_leaves_it_alone_when_omitted(self) -> None:
        item = self.store.create("checkout", "Ship it", system="claims")
        self.assertEqual(self.store.update(item.id, status="done").system, "claims")

    def test_a_rejected_update_applies_nothing(self) -> None:
        """Resolved before any write, like the dates."""
        item = self.store.create("checkout", "Ship it", system="claims")
        with self.assertRaises(ValueError):
            self.store.update(item.id, bucket="now", system="nope")
        stored = self.store.get(item.id)
        self.assertEqual(stored.system, "claims")
        self.assertEqual(stored.bucket, "later")

    def test_list_by_system_slices_across_boards(self) -> None:
        """The retrieval half of a reclassification: one system's work, wherever the
        changes now live. Both boards contribute; each record still names its board."""
        a = self.store.create("checkout", "On checkout", system="claims")
        b = self.store.create("portal", "On portal", system="claims")
        self.store.create("checkout", "Other system", system="rides")
        ids = [c["id"] for c in self.store.list_by_system("claims")]
        self.assertEqual(sorted(ids), sorted([a.id, b.id]))
        products = {c["product"] for c in self.store.list_by_system("claims")}
        self.assertEqual(products, {"checkout", "portal"})

    def test_origin_product_does_not_decide_the_constraint(self) -> None:
        """A suggestion is validated against the OWNING board's product: the change is
        contained within the system that board's product is built on, whoever raised it."""
        item = self.store.create(
            "portal", "Ship it", origin_product="checkout", system="claims"
        )
        self.assertEqual(item.system, "claims")
        with self.assertRaises(ValueError):
            self.store.create(
                "portal", "Nope", origin_product="checkout", system="rides"
            )


class MoveAttributionTest(unittest.TestCase):
    """A move is the one operation that can invalidate an attribution that was valid a
    moment ago, because the destination may not touch that system."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = (
            roadmap_module.ROADMAP_DIR,
            roadmap_module.PRODUCTS,
            roadmap_module.SYSTEMS,
            roadmap_module.PRODUCT_SYSTEMS,
        )
        roadmap_module.ROADMAP_DIR = Path(self._tmp.name)
        roadmap_module.PRODUCTS = {"checkout": "Checkout", "portal": "Portal"}
        roadmap_module.SYSTEMS = {
            "claims": SystemSpec(label="Claims Processor"),
            "rides": SystemSpec(label="Rides"),
        }
        roadmap_module.PRODUCT_SYSTEMS = {
            "checkout": ("claims", "rides"),
            "portal": ("claims",),
        }
        self.store = RoadmapStore()

    def tearDown(self) -> None:
        (
            roadmap_module.ROADMAP_DIR,
            roadmap_module.PRODUCTS,
            roadmap_module.SYSTEMS,
            roadmap_module.PRODUCT_SYSTEMS,
        ) = self._orig
        self._tmp.cleanup()

    def test_a_still_valid_attribution_carries_over(self) -> None:
        item = self.store.create("checkout", "Ship it", system="claims")
        moved = self.store.move(item.id, to_product="portal")
        self.assertEqual(moved.system, "claims")

    def test_a_move_the_destination_cannot_hold_is_refused(self) -> None:
        """Silently keeping an invalid attribution would be worse than an error."""
        item = self.store.create("checkout", "Ship it", system="rides")
        with self.assertRaises(ValueError):
            self.store.move(item.id, to_product="portal")
        self.assertEqual(self.store.get(item.id).product, "checkout")

    def test_re_attributing_as_part_of_the_move(self) -> None:
        item = self.store.create("checkout", "Ship it", system="rides")
        moved = self.store.move(item.id, to_product="portal", system="claims")
        self.assertEqual((moved.product, moved.system), ("portal", "claims"))

    def test_an_unattributed_change_can_still_move(self) -> None:
        """Moving must not be gated on finishing the restructure first."""
        item = self.store.create("checkout", "Legacy", system="claims")
        item.system = None
        moved = self.store.move(item.id, to_product="portal")
        self.assertIsNone(moved.system)


class RestructureTest(unittest.TestCase):
    """Changes predating the layer: reported everywhere, blocking nothing."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = (
            roadmap_module.ROADMAP_DIR,
            roadmap_module.PRODUCTS,
            roadmap_module.SYSTEMS,
            roadmap_module.PRODUCT_SYSTEMS,
            roadmap_module.TRANSITIONAL_IDS,
        )
        roadmap_module.ROADMAP_DIR = Path(self._tmp.name)
        roadmap_module.PRODUCTS = {"checkout": "Checkout", "ops": "Ops"}
        roadmap_module.SYSTEMS = {"claims": SystemSpec(label="Claims Processor")}
        roadmap_module.PRODUCT_SYSTEMS = {"checkout": ("claims",)}
        roadmap_module.TRANSITIONAL_IDS = ()
        self.store = RoadmapStore()
        self.attributed = self.store.create("checkout", "Attributed", system="claims")
        self.legacy = self.store.create("checkout", "Legacy", system="claims")
        # What a board written before the layer looks like on disk.
        self.legacy.system = None

    def tearDown(self) -> None:
        (
            roadmap_module.ROADMAP_DIR,
            roadmap_module.PRODUCTS,
            roadmap_module.SYSTEMS,
            roadmap_module.PRODUCT_SYSTEMS,
            roadmap_module.TRANSITIONAL_IDS,
        ) = self._orig
        self._tmp.cleanup()

    def test_pre_layer_records_load_without_a_system_key(self) -> None:
        """The back-compat guarantee at the data layer: `from_dict` is `cls(**data)`, so a
        record written before the field existed must still construct."""
        stored = self.attributed.to_dict()
        del stored["system"]
        revived = roadmap_module.RoadmapItem.from_dict(stored)
        self.assertIsNone(revived.system)

    def test_report_counts_only_open_unattributed_changes(self) -> None:
        report = self.store.unattributed_report()
        self.assertEqual(report["count"], 1)
        self.assertEqual(report["changes"][0]["id"], self.legacy.id)
        # The config half of the same gap.
        self.assertEqual(report["products_missing_systems"], ["ops"])

    def test_out_of_scope_changes_are_counted_apart_from_debt(self) -> None:
        """The load-bearing scoping rule. Ops declares no systems, so a system-less change
        there measures missing CONFIG, not owed attribution - no amount of attributing
        would bring it down, so it must not inflate the number the banner shouts."""
        self.store.create("ops", "Nothing owed here")
        report = self.store.unattributed_report()
        self.assertEqual(report["count"], 1)  # still just the in-scope one
        self.assertEqual(report["not_yet_in_scope"], 1)
        self.assertNotIn("ops", [c["product"] for c in report["changes"]])

    def test_context_block_stays_quiet_on_an_out_of_scope_board(self) -> None:
        """A PM on a board whose edge is undeclared is not told to attribute (see
        agent.py), so its context block must not shout about it either."""
        self.store.create("ops", "Nothing owed here")
        self.assertNotIn("[NO SYSTEM", self.store.describe_own_product("ops"))

    def test_shipped_work_is_not_restructure_debt(self) -> None:
        self.store.update(self.legacy.id, status="done")
        self.assertEqual(self.store.unattributed_report()["count"], 0)

    def test_attributing_closes_the_gap(self) -> None:
        self.store.update(self.legacy.id, system="claims")
        self.assertEqual(self.store.unattributed_report()["count"], 0)

    def test_context_block_shouts_about_a_missing_attribution(self) -> None:
        """The PM can only act on what its context block shows it."""
        block = self.store.describe_own_product("checkout")
        self.assertIn("[NO SYSTEM", block)
        self.assertIn("[system: Claims Processor]", block)

    def test_rollup_counts_each_change_once(self) -> None:
        rows = {row["id"]: row for row in self.store.system_rollup()}
        self.assertEqual(rows["claims"]["changes"], 1)
        self.assertEqual(rows["claims"]["open_changes"], 1)
        self.assertEqual([p["id"] for p in rows["claims"]["products"]], ["checkout"])
        self.assertFalse(rows["claims"]["transitional"])


class TransitionalIdTest(unittest.TestCase):
    """An id declared as both a product and a system, mid-reclassification. The board must
    stay live: board files load only for declared PRODUCT ids, so removing the [products]
    entry while items sit on it is the data-loss case this state exists to avoid."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = (
            roadmap_module.ROADMAP_DIR,
            roadmap_module.PRODUCTS,
            roadmap_module.SYSTEMS,
            roadmap_module.PRODUCT_SYSTEMS,
            roadmap_module.TRANSITIONAL_IDS,
        )
        roadmap_module.ROADMAP_DIR = Path(self._tmp.name)
        # `claims` is both: still a board, already a system.
        roadmap_module.PRODUCTS = {"checkout": "Checkout", "claims": "Claims Processor"}
        roadmap_module.SYSTEMS = {"claims": SystemSpec(label="Claims Processor")}
        roadmap_module.PRODUCT_SYSTEMS = {"checkout": ("claims",)}
        roadmap_module.TRANSITIONAL_IDS = ("claims",)
        self.store = RoadmapStore()

    def tearDown(self) -> None:
        (
            roadmap_module.ROADMAP_DIR,
            roadmap_module.PRODUCTS,
            roadmap_module.SYSTEMS,
            roadmap_module.PRODUCT_SYSTEMS,
            roadmap_module.TRANSITIONAL_IDS,
        ) = self._orig
        self._tmp.cleanup()

    def test_its_board_still_takes_changes(self) -> None:
        item = self.store.create("claims", "Still here", system="claims")
        self.assertEqual(item.product, "claims")
        self.assertTrue((Path(self._tmp.name) / "claims.json").exists())

    def test_the_rollup_flags_it(self) -> None:
        rows = {row["id"]: row for row in self.store.system_rollup()}
        self.assertTrue(rows["claims"]["transitional"])

    def test_draining_the_board_is_a_move_plus_an_attribution(self) -> None:
        """The whole reclassification, end to end: everything off the old board, attributed
        to the system it was really about, ids and history intact."""
        item = self.store.create("claims", "Really a system's work", system="claims")
        moved = self.store.move(item.id, to_product="checkout", system="claims", triaged=True)
        self.assertEqual((moved.id, moved.product, moved.system), (item.id, "checkout", "claims"))
        self.assertEqual(self.store.list_product("claims"), [])


class SystemPromptTest(unittest.TestCase):
    """The PM files most changes by curl, so the prompt is the enforcement's other half:
    without `system` in its examples, everything it creates is rejected."""

    def setUp(self) -> None:
        self._orig = (
            roadmap_module.PRODUCTS,
            roadmap_module.SYSTEMS,
            roadmap_module.PRODUCT_SYSTEMS,
            agent_module.SYSTEMS,
        )
        roadmap_module.PRODUCTS = {"checkout": "Checkout"}
        roadmap_module.SYSTEMS = agent_module.SYSTEMS = {
            "claims": SystemSpec(label="Claims Processor", path="services/claims"),
            "rides": SystemSpec(label="Rides", repo="github.com/org/rides"),
        }
        roadmap_module.PRODUCT_SYSTEMS = {"checkout": ("claims", "rides")}

    def tearDown(self) -> None:
        (
            roadmap_module.PRODUCTS,
            roadmap_module.SYSTEMS,
            roadmap_module.PRODUCT_SYSTEMS,
            agent_module.SYSTEMS,
        ) = self._orig

    def test_describe_system_names_id_label_and_location(self) -> None:
        described = agent_module._describe_system("claims")
        self.assertIn("Claims Processor", described)
        self.assertIn("`claims`", described)
        # Where the code is, because the PM's next move is often a dev task into that tree.
        self.assertIn("services/claims", described)

    def test_repo_stands_in_when_there_is_no_path(self) -> None:
        self.assertIn("github.com/org/rides", agent_module._describe_system("rides"))

    def test_the_guidance_block_carries_system_in_its_create_example(self) -> None:
        block = agent_module.SYSTEM_GUIDANCE_TEMPLATE.format(
            product="checkout",
            product_label="Checkout",
            roadmap_base_url="http://127.0.0.1:8000/roadmap",
            auth_header="",
            system_summary="Claims Processor (id `claims`)",
        )
        self.assertIn('"system": "<system_id>"', block)
        self.assertIn("Claims Processor", block)

    def _agent(self, product: str, tmp: Path):
        """A PMAgent built far enough to hold its assembled prompt. Only the fields
        __init__ reads are needed, so a stub session keeps this off the session store -
        same approach as test_products_hierarchy.OwnedBoardsTest."""
        import dataclasses
        import threading

        session = dataclasses.make_dataclass(
            "StubSession",
            ["id", "product", "initiative_id", "adopted_products", "model", "worktree_path"],
        )(
            id="s1",
            product=product,
            initiative_id=None,
            adopted_products=[],
            model="claude-opus-5",
            worktree_path=str(tmp),
        )
        return agent_module.PMAgent(session, threading.Lock())

    def test_a_pinned_pm_is_told_which_systems_its_product_touches(self) -> None:
        """The assembled prompt, not just the template: this is what actually reaches the
        PM, and if the block is dropped here every change it files is rejected."""
        with tempfile.TemporaryDirectory() as tmp:
            prompt = self._agent("checkout", Path(tmp)).system_prompt
        self.assertIn('"system": "<system_id>"', prompt)
        self.assertIn("`claims`", prompt)
        self.assertIn("`rides`", prompt)
        self.assertIn("services/claims", prompt)

    def test_the_block_is_absent_when_no_systems_are_declared(self) -> None:
        """Dormant means dormant: a PM on a deployment with no [systems] must not be told
        to attribute anything, or it will invent ids or refuse to create changes."""
        roadmap_module.SYSTEMS = agent_module.SYSTEMS = {}
        roadmap_module.PRODUCT_SYSTEMS = {}
        with tempfile.TemporaryDirectory() as tmp:
            prompt = self._agent("checkout", Path(tmp)).system_prompt
        self.assertNotIn('"system"', prompt)

    def test_the_block_is_absent_on_an_out_of_scope_product(self) -> None:
        """Systems exist, but this product declares none. Telling its PM to attribute
        CHANGES would make it pick from whatever systems happen to exist - wrong data,
        not missing data. The prompt and roadmap.requires_system are two statements of
        one rule. DEV-TASK attribution is deliberately different: once [systems] is
        declared, the dispatch endpoint requires a system on every dispatch (it is what
        routes gitflow rules to the dev agent, deployment-wide, not per product - see
        tasks.validate_dispatch_system), so the dispatch note must stay or this PM
        would just hit unexplained 400s."""
        roadmap_module.PRODUCTS = {"checkout": "Checkout", "ops": "Ops"}
        roadmap_module.PRODUCT_SYSTEMS = {"checkout": ("claims",)}
        with tempfile.TemporaryDirectory() as tmp:
            prompt = self._agent("ops", Path(tmp)).system_prompt
        self.assertNotIn("attributed to a SYSTEM", prompt)
        self.assertIn("Every dev task must name", prompt)
        # ...while the in-scope sibling on the same deployment still gets the
        # roadmap-attribution block.
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIn(
                "attributed to a SYSTEM", self._agent("checkout", Path(tmp)).system_prompt
            )


if __name__ == "__main__":
    unittest.main()
