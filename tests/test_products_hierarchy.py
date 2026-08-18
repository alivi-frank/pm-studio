"""Tests for hierarchical products: a product can declare a `parent`, making it a
sub-product of it.

Three things are worth pinning down, and they are the three that would silently rot:

1. **A flat taxonomy behaves exactly as it did before hierarchy existed.** Every
   deployment has one, so every helper here has to answer the old answer when nothing
   declares a parent - that is what makes this feature additive rather than a migration.
2. **A bad `parent` refuses to boot.** A child that quietly became a top-level product is
   a working-looking deployment with one board too many and nothing pointing at the typo.
3. **Ownership is the subtree.** A parent's PM sees and may write its children's boards;
   a child sees its own. The prompt and the Bash allowlist are two statements of that one
   rule, so they are checked against each other rather than separately.
"""

import dataclasses
import tempfile
import textwrap
import unittest
from pathlib import Path

from pm_studio import agent as agent_module
from pm_studio import roadmap as roadmap_module
from pm_studio.config import load_config
from pm_studio.roadmap import RoadmapStore


class ProductConfigTest(unittest.TestCase):
    """[products] parsing - the operator-facing contract."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "pm_studio_local").mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write(self, products: str):
        (self.root / "pm_studio_local" / "config.toml").write_text(
            textwrap.dedent(products)
        )
        return load_config(self.root)

    def test_flat_table_is_unchanged(self) -> None:
        """The historical spelling, and the only one most deployments will ever use."""
        cfg = self._write("""\
            [products]
            web = "Web App"
            platform = "Platform"
        """)
        self.assertEqual(cfg.products, {"web": "Web App", "platform": "Platform"})
        self.assertEqual(cfg.product_parents, {})

    def test_both_child_spellings_and_display_order(self) -> None:
        """Inline table and [products.x] block mean the same thing, and children are
        ordered directly after their own parent however they were declared - every
        consumer reads iteration order as display order."""
        cfg = self._write("""\
            [products]
            web = "Web App"
            platform = "Platform"
            auth = { label = "Auth & Identity", parent = "web" }

            [products.billing]
            label = "Billing"
            parent = "web"

            [products.sdk]
            label = "SDK"
            parent = "platform"
        """)
        self.assertEqual(
            list(cfg.products), ["web", "auth", "billing", "platform", "sdk"]
        )
        self.assertEqual(cfg.products["auth"], "Auth & Identity")
        self.assertEqual(
            cfg.product_parents,
            {"auth": "web", "billing": "web", "sdk": "platform"},
        )

    def test_child_without_label_falls_back_to_its_id(self) -> None:
        """Cosmetic, so it is not fatal: the id showing up on the board is a mistake you
        see immediately, and refusing to start over a display string would be worse."""
        cfg = self._write("""\
            [products]
            web = "Web App"
            auth = { parent = "web" }
        """)
        self.assertEqual(cfg.products["auth"], "auth")
        self.assertEqual(cfg.product_parents["auth"], "web")

    def test_unknown_parent_is_fatal(self) -> None:
        with self.assertRaises(SystemExit) as caught:
            self._write("""\
                [products]
                web = "Web App"
                auth = { label = "Auth", parent = "wbe" }
            """)
        self.assertEqual(caught.exception.code, 2)

    def test_self_parent_is_fatal(self) -> None:
        with self.assertRaises(SystemExit):
            self._write("""\
                [products]
                web = { label = "Web App", parent = "web" }
            """)

    def test_three_levels_and_deeper(self) -> None:
        """Depth is the operator's decision: a child can itself be a parent, and the
        ordering stays depth first so a whole branch reads together."""
        cfg = self._write("""\
            [products]
            web = "Web App"
            auth = { label = "Auth", parent = "web" }
            sso = { label = "SSO", parent = "auth" }
            passkeys = { label = "Passkeys", parent = "auth" }
            billing = { label = "Billing", parent = "web" }
            platform = "Platform"
        """)
        self.assertEqual(
            list(cfg.products),
            ["web", "auth", "sso", "passkeys", "billing", "platform"],
        )
        self.assertEqual(cfg.product_parents["sso"], "auth")
        self.assertEqual(cfg.product_parents["auth"], "web")

    def test_a_child_declared_before_its_parent_still_resolves(self) -> None:
        """`parent` is a pointer, not a nesting syntax, so declaration order is free."""
        cfg = self._write("""\
            [products]
            sso = { label = "SSO", parent = "auth" }
            auth = { label = "Auth", parent = "web" }
            web = "Web App"
        """)
        self.assertEqual(list(cfg.products), ["web", "auth", "sso"])

    def test_cycle_is_fatal(self) -> None:
        """The products in a cycle hang off no top-level product at all, so they would
        drop out of the taxonomy entirely while their boards sat on disk holding items."""
        with self.assertRaises(SystemExit) as caught:
            self._write("""\
                [products]
                a = { label = "A", parent = "b" }
                b = { label = "B", parent = "c" }
                c = { label = "C", parent = "a" }
            """)
        self.assertEqual(caught.exception.code, 2)

    def test_non_table_product_value_is_fatal(self) -> None:
        with self.assertRaises(SystemExit):
            self._write("""\
                [products]
                web = 42
            """)


class ProductTreeHelpersTest(unittest.TestCase):
    def setUp(self) -> None:
        self._orig_products = roadmap_module.PRODUCTS
        self._orig_parents = roadmap_module.PRODUCT_PARENTS
        roadmap_module.PRODUCTS = {
            "web": "Web App",
            "auth": "Auth & Identity",
            "billing": "Billing",
            "platform": "Platform",
        }
        roadmap_module.PRODUCT_PARENTS = {"auth": "web", "billing": "web"}

    def tearDown(self) -> None:
        roadmap_module.PRODUCTS = self._orig_products
        roadmap_module.PRODUCT_PARENTS = self._orig_parents

    def test_navigation(self) -> None:
        self.assertEqual(roadmap_module.parent_of("auth"), "web")
        self.assertIsNone(roadmap_module.parent_of("web"))
        self.assertEqual(roadmap_module.children_of("web"), ["auth", "billing"])
        self.assertEqual(roadmap_module.children_of("auth"), [])
        self.assertEqual(roadmap_module.top_level_products(), ["web", "platform"])

    def test_subtree_is_the_unit_of_ownership(self) -> None:
        self.assertEqual(
            roadmap_module.subtree_products("web"), ["web", "auth", "billing"]
        )
        # A leaf owns exactly itself - which is why nothing had to change shape for a
        # flat deployment.
        self.assertEqual(roadmap_module.subtree_products("auth"), ["auth"])
        self.assertEqual(roadmap_module.subtree_products("platform"), ["platform"])

    def test_path_label_names_the_parent(self) -> None:
        self.assertEqual(roadmap_module.product_path_label("web"), "Web App")
        self.assertEqual(
            roadmap_module.product_path_label("billing"), "Web App / Billing"
        )
        # An item can outlive the config line that declared its board.
        self.assertEqual(roadmap_module.product_path_label("gone"), "gone")


class DeepProductTreeTest(unittest.TestCase):
    """Three levels. Every helper is written to recurse rather than to count levels, so
    what this pins down is that none of them silently stops after one hop."""

    def setUp(self) -> None:
        self._orig_products = roadmap_module.PRODUCTS
        self._orig_parents = roadmap_module.PRODUCT_PARENTS
        roadmap_module.PRODUCTS = {
            "web": "Web App",
            "auth": "Auth & Identity",
            "sso": "SSO",
            "passkeys": "Passkeys",
            "billing": "Billing",
            "platform": "Platform",
        }
        roadmap_module.PRODUCT_PARENTS = {
            "auth": "web",
            "sso": "auth",
            "passkeys": "auth",
            "billing": "web",
        }

    def tearDown(self) -> None:
        roadmap_module.PRODUCTS = self._orig_products
        roadmap_module.PRODUCT_PARENTS = self._orig_parents

    def test_subtree_reaches_grandchildren(self) -> None:
        self.assertEqual(
            roadmap_module.subtree_products("web"),
            ["web", "auth", "sso", "passkeys", "billing"],
        )
        self.assertEqual(
            roadmap_module.subtree_products("auth"), ["auth", "sso", "passkeys"]
        )
        self.assertEqual(roadmap_module.subtree_products("sso"), ["sso"])

    def test_children_stays_one_hop(self) -> None:
        """The counterpart: `children_of` is direct children only, which is what makes it
        the right tool for building the nested board sections."""
        self.assertEqual(roadmap_module.children_of("web"), ["auth", "billing"])
        self.assertEqual(roadmap_module.children_of("auth"), ["sso", "passkeys"])

    def test_ancestors_and_full_path(self) -> None:
        self.assertEqual(roadmap_module.ancestors_of("sso"), ["auth", "web"])
        self.assertEqual(roadmap_module.ancestors_of("web"), [])
        self.assertEqual(
            roadmap_module.product_path_label("sso"), "Web App / Auth & Identity / SSO"
        )

    def test_walks_terminate_on_a_cycle(self) -> None:
        """Config refuses a cycle, but a store can be handed a patched taxonomy, and both
        walks run inside a PM's turn and the board's render - a hang there is a far worse
        failure than a short answer. What is guaranteed is termination and no repeats; in a
        cycle every product is genuinely both above and below the others, so which of them
        a walk reports is not a meaningful thing to pin down."""
        roadmap_module.PRODUCT_PARENTS = {"a": "b", "b": "a"}
        roadmap_module.PRODUCTS = {"a": "A", "b": "B"}
        for walk in (roadmap_module.ancestors_of, roadmap_module.subtree_products):
            result = walk("a")
            self.assertEqual(len(result), len(set(result)), f"{walk.__name__} repeated")
            self.assertLessEqual(len(result), len(roadmap_module.PRODUCTS))
        # And the label built on top of the upward walk still comes out usable.
        self.assertEqual(roadmap_module.product_path_label("a"), "B / A")

    def test_flat_taxonomy_answers_the_old_answers(self) -> None:
        roadmap_module.PRODUCT_PARENTS = {}
        self.assertEqual(
            roadmap_module.top_level_products(), list(roadmap_module.PRODUCTS)
        )
        for product in roadmap_module.PRODUCTS:
            self.assertEqual(roadmap_module.subtree_products(product), [product])
            self.assertEqual(roadmap_module.children_of(product), [])
            self.assertEqual(
                roadmap_module.product_path_label(product),
                roadmap_module.PRODUCTS[product],
            )


class SubtreeContextTest(unittest.TestCase):
    """What a pinned PM's turn actually opens with (see server._roadmap_context_for)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_dir = roadmap_module.ROADMAP_DIR
        self._orig_products = roadmap_module.PRODUCTS
        self._orig_parents = roadmap_module.PRODUCT_PARENTS
        roadmap_module.ROADMAP_DIR = Path(self._tmp.name)
        roadmap_module.PRODUCTS = {
            "web": "Web App",
            "auth": "Auth & Identity",
            "billing": "Billing",
            "platform": "Platform",
        }
        roadmap_module.PRODUCT_PARENTS = {"auth": "web", "billing": "web"}
        self.store = RoadmapStore()
        self.store.create("web", "Web shell refresh", description="the frame")
        self.store.create("auth", "SSO rollout", description="okta first")
        self.store.create("billing", "Dunning emails", description="retries")
        self.store.create("platform", "Log pipeline", description="ship it")

    def tearDown(self) -> None:
        roadmap_module.ROADMAP_DIR = self._orig_dir
        roadmap_module.PRODUCTS = self._orig_products
        roadmap_module.PRODUCT_PARENTS = self._orig_parents
        self._tmp.cleanup()

    def test_a_child_gets_its_own_board_on_its_own_file(self) -> None:
        """A sub-product is a product: its own board file, nothing shared with the
        parent - which is what keeps re-parenting a config edit."""
        self.assertTrue((Path(self._tmp.name) / "auth.json").exists())
        self.assertEqual([i["title"] for i in self.store.list_product("auth")],
                         ["SSO rollout"])
        self.assertEqual([i["title"] for i in self.store.list_product("web")],
                         ["Web shell refresh"])

    def test_parent_sees_its_whole_family_at_full_depth(self) -> None:
        deep = self.store.describe_own_product("web")
        for title, description in [
            ("Web shell refresh", "the frame"),
            ("SSO rollout", "okta first"),
            ("Dunning emails", "retries"),
        ]:
            self.assertIn(title, deep)
            self.assertIn(description, deep)  # full depth, not a digest
        # Each board is named, so the PM knows which id to write to.
        self.assertIn("id `auth`", deep)
        self.assertIn("id `billing`", deep)
        self.assertNotIn("Log pipeline", deep)

    def test_parent_digest_excludes_the_whole_subtree(self) -> None:
        """The half that would double-report: children are already covered at full
        depth, so they must not come back as one-liners too."""
        others = self.store.describe_other_products("web")
        self.assertIn("Log pipeline", others)
        self.assertNotIn("SSO rollout", others)
        self.assertNotIn("Dunning emails", others)
        self.assertNotIn("Web shell refresh", others)

    def test_child_sees_only_its_own_board_deeply(self) -> None:
        deep = self.store.describe_own_product("auth")
        self.assertIn("SSO rollout", deep)
        self.assertNotIn("Dunning emails", deep)
        self.assertNotIn("Web shell refresh", deep)
        # Its parent and its siblings reach it the way any other product does.
        others = self.store.describe_other_products("auth")
        self.assertIn("Web App: ", others)
        self.assertIn("Web App / Billing: ", others)
        self.assertIn("Platform: ", others)

    def test_unpinned_session_still_digests_everything(self) -> None:
        others = self.store.describe_other_products("")
        for title in ["Web shell refresh", "SSO rollout", "Dunning emails", "Log pipeline"]:
            self.assertIn(title, others)

    def test_empty_family_says_so_once_and_names_the_children(self) -> None:
        """An empty parent reports the family, not just itself - "Web App has no open
        items" would read as complete while two sub-product boards sit underneath it."""
        with tempfile.TemporaryDirectory() as fresh:
            roadmap_module.ROADMAP_DIR = Path(fresh)
            text = RoadmapStore().describe_own_product("web")
        self.assertIn("no open items", text)
        self.assertIn("sub-products", text)
        self.assertIn("Auth & Identity", text)
        self.assertIn("Billing", text)


class OwnedBoardsTest(unittest.TestCase):
    """The prompt tells a PM which boards are its own; the Bash allowlist enforces it.
    Both are built from subtree_products, and this is the test that says so."""

    def setUp(self) -> None:
        self._orig_products = roadmap_module.PRODUCTS
        self._orig_parents = roadmap_module.PRODUCT_PARENTS
        roadmap_module.PRODUCTS = {
            "web": "Web App",
            "auth": "Auth & Identity",
            "platform": "Platform",
        }
        roadmap_module.PRODUCT_PARENTS = {"auth": "web"}

    def tearDown(self) -> None:
        roadmap_module.PRODUCTS = self._orig_products
        roadmap_module.PRODUCT_PARENTS = self._orig_parents

    def _agent(self, product: str, tmp: Path):
        """A PMAgent built far enough to hold its prompt and allowlist. Only the fields
        __init__ reads are needed, so a stub session keeps this off the session store."""
        session = dataclasses.make_dataclass(
            "StubSession",
            ["id", "product", "initiative_id", "adopted_products", "model", "mode", "worktree_path"],
        )(
            id="s1",
            product=product,
            initiative_id=None,
            adopted_products=[],
            model="claude-opus-5",
            mode="build",
            worktree_path=str(tmp),
        )
        import threading

        return agent_module.PMAgent(session, threading.Lock())

    def test_parent_may_patch_its_children(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pm = self._agent("web", Path(tmp))
        base = agent_module.ROADMAP_BASE_URL
        self.assertIn(f"Bash(curl -s -X PATCH {base}/web/*)", pm.allowed_tools)
        self.assertIn(f"Bash(curl -s -X PATCH {base}/auth/*)", pm.allowed_tools)
        # The boundary: another family's board is not reachable for edits, only for
        # suggestions (the broad POST).
        self.assertNotIn(f"Bash(curl -s -X PATCH {base}/platform/*)", pm.allowed_tools)
        # And the prompt says the same thing the allowlist just enforced.
        self.assertIn("has sub-products", pm.system_prompt)
        self.assertIn("id `auth`", pm.system_prompt)

    def test_child_may_patch_only_itself(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pm = self._agent("auth", Path(tmp))
        base = agent_module.ROADMAP_BASE_URL
        self.assertIn(f"Bash(curl -s -X PATCH {base}/auth/*)", pm.allowed_tools)
        self.assertNotIn(f"Bash(curl -s -X PATCH {base}/web/*)", pm.allowed_tools)
        self.assertIn("is a sub-product of", pm.system_prompt)

    def test_a_grandparent_owns_the_whole_subtree(self) -> None:
        """The allowlist grants every board below, at any depth - and the prompt has to
        name every one of them, or the PM holds write access to a board nobody told it
        about."""
        roadmap_module.PRODUCTS = {
            "web": "Web App",
            "auth": "Auth & Identity",
            "sso": "SSO",
            "platform": "Platform",
        }
        roadmap_module.PRODUCT_PARENTS = {"auth": "web", "sso": "auth"}
        with tempfile.TemporaryDirectory() as tmp:
            pm = self._agent("web", Path(tmp))
        base = agent_module.ROADMAP_BASE_URL
        for owned in ("web", "auth", "sso"):
            self.assertIn(f"Bash(curl -s -X PATCH {base}/{owned}/*)", pm.allowed_tools)
        self.assertNotIn(f"Bash(curl -s -X PATCH {base}/platform/*)", pm.allowed_tools)
        # Named by path, so a grandchild is not mistaken for another top-level board.
        self.assertIn("Web App / Auth & Identity / SSO (id `sso`)", pm.system_prompt)

    def test_a_middle_product_is_told_it_is_both(self) -> None:
        """A product in the middle of a three-level family is a parent AND a child, and
        the two facts say different things: what it may write, and who already reads it."""
        roadmap_module.PRODUCTS = {"web": "Web App", "auth": "Auth", "sso": "SSO"}
        roadmap_module.PRODUCT_PARENTS = {"auth": "web", "sso": "auth"}
        with tempfile.TemporaryDirectory() as tmp:
            pm = self._agent("auth", Path(tmp))
        base = agent_module.ROADMAP_BASE_URL
        self.assertIn(f"Bash(curl -s -X PATCH {base}/auth/*)", pm.allowed_tools)
        self.assertIn(f"Bash(curl -s -X PATCH {base}/sso/*)", pm.allowed_tools)
        # Upwards is still off limits for edits.
        self.assertNotIn(f"Bash(curl -s -X PATCH {base}/web/*)", pm.allowed_tools)
        self.assertIn("has sub-products", pm.system_prompt)
        self.assertIn("is a sub-product of", pm.system_prompt)

    def test_flat_product_allowlist_is_unchanged(self) -> None:
        roadmap_module.PRODUCT_PARENTS = {}
        with tempfile.TemporaryDirectory() as tmp:
            pm = self._agent("web", Path(tmp))
        base = agent_module.ROADMAP_BASE_URL
        self.assertEqual(
            pm.allowed_tools.count(f"Bash(curl -s -X PATCH {base}/"), 1
        )
        # Neither hierarchy block is in a flat deployment's prompt.
        self.assertNotIn("has sub-products", pm.system_prompt)
        self.assertNotIn("is a sub-product of", pm.system_prompt)


if __name__ == "__main__":
    unittest.main()
