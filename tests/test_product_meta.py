"""Tests for product metadata: owner/team/stage/description on a [products] entry.

Three contracts worth pinning: metadata is opt-in per product (nothing declared means
no entry, and every consumer treats that as normal); `stage` is a closed vocabulary
that refuses to boot on a typo rather than silently reading as the steady state; and
the facts reach the one consumer that acts on them - the PM's prompt - only when
declared, so an empty deployment's prompt is byte-for-byte what it was before.
"""

import dataclasses
import tempfile
import textwrap
import threading
import unittest
from pathlib import Path

from pm_studio import agent as agent_module
from pm_studio import roadmap as roadmap_module
from pm_studio.config import ProductMeta, load_config


class ProductMetaConfigTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "pm_studio_local").mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write(self, config: str):
        (self.root / "pm_studio_local" / "config.toml").write_text(textwrap.dedent(config))
        return load_config(self.root)

    def test_flat_entry_declares_nothing(self) -> None:
        """The historical spelling stays metadata-free - and so does a table entry that
        only has structure. product_meta holds only products that said something."""
        cfg = self._write("""\
            [products]
            web = "Web App"
            auth = { label = "Auth", parent = "web" }
        """)
        self.assertEqual(cfg.product_meta, {})

    def test_full_metadata(self) -> None:
        cfg = self._write("""\
            [products.checkout]
            label = "Checkout"
            description = "Guest checkout"
            owner = "jane.doe"
            team = "Payments"
            stage = "development"
        """)
        self.assertEqual(
            cfg.product_meta["checkout"],
            ProductMeta(description="Guest checkout", owner="jane.doe",
                        team="Payments", stage="development"),
        )

    def test_partial_metadata_defaults_the_rest(self) -> None:
        cfg = self._write("""\
            [products.checkout]
            label = "Checkout"
            owner = "jane.doe"
        """)
        meta = cfg.product_meta["checkout"]
        self.assertEqual((meta.owner, meta.team, meta.stage), ("jane.doe", "", "ga"))

    def test_explicit_ga_alone_is_still_no_entry(self) -> None:
        """stage = "ga" restates the default; a product saying only that has declared
        no fact anyone would show."""
        cfg = self._write("""\
            [products.checkout]
            label = "Checkout"
            stage = "ga"
        """)
        self.assertEqual(cfg.product_meta, {})

    def test_unknown_stage_is_fatal(self) -> None:
        """Same contract as [enterprise] mode: a typo must not silently mean ga."""
        with self.assertRaises(SystemExit) as caught:
            self._write("""\
                [products.checkout]
                label = "Checkout"
                stage = "generally-available"
            """)
        self.assertEqual(caught.exception.code, 2)

    def test_metadata_composes_with_structure(self) -> None:
        """owner/stage next to parent and systems on one entry - the normal case."""
        cfg = self._write("""\
            [products]
            web = "Web App"

            [products.checkout]
            label = "Checkout"
            parent = "web"
            systems = ["claims"]
            owner = "jane.doe"
            stage = "sunset"

            [systems]
            claims = "Claims Processor"
        """)
        self.assertEqual(cfg.product_parents["checkout"], "web")
        self.assertEqual(cfg.product_systems["checkout"], ("claims",))
        self.assertEqual(cfg.product_meta["checkout"].stage, "sunset")


class ProductFactsPromptTest(unittest.TestCase):
    """The prompt line - the consumer the feature exists for."""

    def setUp(self) -> None:
        self._orig = (roadmap_module.PRODUCTS, roadmap_module.PRODUCT_META)
        roadmap_module.PRODUCTS = {"checkout": "Checkout"}
        roadmap_module.PRODUCT_META = {}

    def tearDown(self) -> None:
        roadmap_module.PRODUCTS, roadmap_module.PRODUCT_META = self._orig

    def _agent(self, tmp: Path):
        session = dataclasses.make_dataclass(
            "StubSession",
            ["id", "product", "initiative_id", "adopted_products", "model", "mode", "worktree_path"],
        )("s1", "checkout", None, [], "claude-opus-5", "build", str(tmp))
        return agent_module.PMAgent(session, threading.Lock())

    def test_declared_facts_reach_the_prompt(self) -> None:
        roadmap_module.PRODUCT_META = {
            "checkout": ProductMeta(owner="jane.doe", team="Payments", stage="development")
        }
        with tempfile.TemporaryDirectory() as tmp:
            prompt = self._agent(Path(tmp)).system_prompt
        self.assertIn("owner jane.doe", prompt)
        self.assertIn("built by Payments", prompt)
        self.assertIn("stage: development", prompt)
        self.assertIn("context, not permissions", prompt)

    def test_ga_stage_is_not_narrated(self) -> None:
        """Steady state is not a fact worth a line-item; only the owner shows."""
        roadmap_module.PRODUCT_META = {"checkout": ProductMeta(owner="jane.doe")}
        with tempfile.TemporaryDirectory() as tmp:
            prompt = self._agent(Path(tmp)).system_prompt
        self.assertIn("owner jane.doe", prompt)
        self.assertNotIn("stage:", prompt)

    def test_no_metadata_means_no_line(self) -> None:
        """The dormant guarantee, at the prompt level."""
        with tempfile.TemporaryDirectory() as tmp:
            prompt = self._agent(Path(tmp)).system_prompt
        self.assertNotIn("Facts about", prompt)


if __name__ == "__main__":
    unittest.main()
