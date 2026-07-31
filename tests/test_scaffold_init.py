"""Tests for `python -m pm_studio init` - the scaffolder that writes pm_studio_local/.

The templates are run through `str.format()`, which makes every literal `{` in them a
format field. That bites specifically when an example needs braces of its own - a TOML
inline table, `{ label = "...", parent = "..." }` - and the failure is a KeyError that
takes the whole `init` command down before it writes anything. Reading the template text
(as test_no_deployment_data does) cannot catch it, so this formats them the way run_init
does and then loads the result back through the real config parser: a template that
scaffolds a config PM Studio itself rejects is the other half of the same bug.
"""

import tempfile
import unittest
from pathlib import Path

from pm_studio.config import CONFIG_FILE_NAME, LOCAL_DIR_NAME, load_config
from pm_studio.scaffold import run_init


class ScaffoldInitTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    @property
    def config_path(self) -> Path:
        return self.root / LOCAL_DIR_NAME / CONFIG_FILE_NAME

    def test_init_writes_a_config_the_parser_accepts(self) -> None:
        run_init(self.root)
        self.assertTrue(self.config_path.is_file())
        cfg = load_config(self.root)
        # Everything is commented out in the template, so a fresh scaffold is generic.
        self.assertEqual(cfg.products, {})
        self.assertEqual(cfg.product_parents, {})

    def test_the_commented_examples_are_valid_once_uncommented(self) -> None:
        """The samples are the documentation most operators actually read, so they have
        to be copy-paste correct - including the sub-product ones, whose braces are
        exactly what `str.format()` would have eaten."""
        run_init(self.root)
        # Only the [products] block: the other sample tables ([models], [smtp],
        # [[trackers]], [enterprise]) are commented out header and all, and turning those
        # on is a different question from whether these product lines are well formed.
        lines = self.config_path.read_text().splitlines()
        start = lines.index("[products]")
        # The template separates its tables with a blank line, and every later table is
        # commented out header and all - so the blank line is the end of this block.
        end = next(
            (i for i in range(start + 1, len(lines)) if not lines[i].strip()),
            len(lines),
        )
        block = [
            line[2:] if line.startswith("# ") else line for line in lines[start:end]
        ]
        self.config_path.write_text("\n".join(block) + "\n")
        cfg = load_config(self.root)
        self.assertEqual(cfg.products["web"], "Web App")
        self.assertEqual(cfg.products["auth"], "Auth & Identity")
        # Including the three-level sample: `sso` hangs off `auth`, which is itself a child.
        self.assertEqual(
            cfg.product_parents, {"auth": "web", "billing": "web", "sso": "auth"}
        )
        # Depth first: each product is followed by its own descendants, so a branch reads
        # together even though the template declares platform before auth.
        self.assertEqual(
            list(cfg.products), ["web", "auth", "sso", "billing", "platform"]
        )

    def test_init_is_idempotent(self) -> None:
        """It is documented as safe to re-run on an initialized repo - a second pass must
        not overwrite an operator's filled-in config."""
        run_init(self.root)
        self.config_path.write_text('[products]\nweb = "My Web App"\n')
        run_init(self.root)
        self.assertEqual(load_config(self.root).products, {"web": "My Web App"})


if __name__ == "__main__":
    unittest.main()
