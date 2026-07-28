"""Tests for pm_studio_local/ config loading: defaults with nothing present, a full
config.toml, and the append-only local instruction fragments."""

import tempfile
import textwrap
import unittest
from pathlib import Path

from pm_studio.config import DEFAULT_REPO_LAYOUT, load_config


class ConfigLoadingTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_defaults_with_no_local_dir(self) -> None:
        cfg = load_config(self.root)
        self.assertEqual(cfg.project_name, self.root.name)
        self.assertEqual(cfg.default_session_name, "Main")
        self.assertEqual(cfg.workspace_root, "pm_studio")
        self.assertEqual(cfg.port, 8000)
        self.assertEqual(cfg.products, {})
        self.assertEqual(cfg.repo_layout, DEFAULT_REPO_LAYOUT)
        self.assertEqual(cfg.pm_instructions, "")
        self.assertEqual(cfg.dev_instructions, "")
        self.assertEqual(cfg.knowledge_files, ())
        self.assertEqual(cfg.base_url, "http://127.0.0.1:8000")
        self.assertEqual(cfg.workspace_rel, "pm_studio/workspace")
        self.assertEqual(cfg.archive_rel, "pm_studio/workspace/archive")

    def test_full_config_and_fragments(self) -> None:
        local = self.root / "pm_studio_local"
        (local / "knowledge").mkdir(parents=True)
        (local / "config.toml").write_text(textwrap.dedent("""\
            [project]
            name = "Acme Intranet"
            default_session_name = "Monolith"
            workspace_root = "pm_agent"
            layout = '''
            - `web/` - the web app
            '''

            [server]
            port = 8123

            [products]
            web = "Web App"
            platform = "Platform"

            [models]
            default = "sonnet"
            sonnet = "Sonnet"
        """))
        (local / "PM_INSTRUCTIONS.md").write_text("Never discuss internal pricing.\n")
        (local / "DEV_INSTRUCTIONS.md").write_text("Always run make test.\n")
        (local / "knowledge" / "compliance.md").write_text("SOC2 notes\n")

        cfg = load_config(self.root)
        self.assertEqual(cfg.project_name, "Acme Intranet")
        self.assertEqual(cfg.default_session_name, "Monolith")
        # A migrated deployment keeps its data where it already lives.
        self.assertEqual(cfg.workspace_rel, "pm_agent/workspace")
        self.assertEqual(cfg.base_url, "http://127.0.0.1:8123")
        self.assertEqual(list(cfg.products), ["web", "platform"])
        self.assertEqual(cfg.models, {"sonnet": "Sonnet"})
        self.assertEqual(cfg.default_model, "sonnet")
        self.assertIn("`web/` - the web app", cfg.repo_layout)
        self.assertEqual(cfg.pm_instructions, "Never discuss internal pricing.")
        self.assertEqual(cfg.dev_instructions, "Always run make test.")
        self.assertEqual(cfg.knowledge_files, ("pm_studio_local/knowledge/compliance.md",))

    def test_invalid_toml_is_fatal(self) -> None:
        local = self.root / "pm_studio_local"
        local.mkdir()
        (local / "config.toml").write_text("not [valid toml")
        with self.assertRaises(SystemExit):
            load_config(self.root)


if __name__ == "__main__":
    unittest.main()
