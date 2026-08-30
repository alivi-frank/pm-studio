"""Tests for `[server] landing`: which page the front door serves.

The setting is deployment configuration, so the contract is config-shaped: a missing
or blank value is the unchanged default, a bad value is fatal at load (a typo would
otherwise silently land every visitor on the wrong page), and the sessions list keeps
a stable address either way.
"""

import tempfile
import textwrap
import unittest
from pathlib import Path

from pm_studio.config import load_config


def write_config(root: Path, body: str) -> None:
    local = root / "pm_studio_local"
    local.mkdir(parents=True, exist_ok=True)
    (local / "config.toml").write_text(textwrap.dedent(body))


class LandingConfigTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_default_is_sessions(self) -> None:
        self.assertEqual(load_config(self.root).landing, "sessions")

    def test_blank_is_the_default(self) -> None:
        write_config(self.root, '[server]\nlanding = ""\n')
        self.assertEqual(load_config(self.root).landing, "sessions")

    def test_overview_is_accepted(self) -> None:
        write_config(self.root, '[server]\nlanding = "overview"\n')
        self.assertEqual(load_config(self.root).landing, "overview")

    def test_unknown_value_is_fatal(self) -> None:
        write_config(self.root, '[server]\nlanding = "dashbord"\n')
        with self.assertRaises(SystemExit):
            load_config(self.root)


if __name__ == "__main__":
    unittest.main()
