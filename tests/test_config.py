"""Tests for pm_studio_local/ config loading: defaults with nothing present, a full
config.toml, the append-only local instruction fragments, and the [enterprise]/[smtp]
tables that decide the operating mode."""

import os
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from pm_studio.config import (
    DEFAULT_REPO_LAYOUT,
    MODE_ENTERPRISE,
    MODE_PERSONAL,
    load_config,
)


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


class OperatingModeTest(unittest.TestCase):
    """`[enterprise]` decides whether the whole instance requires a login, so its
    parsing is deliberately strict and its default deliberately permissive: an existing
    deployment that upgrades the package must not suddenly demand accounts."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.local = self.root / "pm_studio_local"
        self.local.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write(self, body: str) -> None:
        (self.local / "config.toml").write_text(textwrap.dedent(body))

    def test_personal_is_the_default(self) -> None:
        cfg = load_config(self.root)
        self.assertEqual(cfg.mode, MODE_PERSONAL)
        self.assertFalse(cfg.is_enterprise)
        self.assertIsNone(cfg.smtp)

    def test_explicit_mode(self) -> None:
        self._write("""\
            [enterprise]
            mode = "enterprise"
        """)
        self.assertTrue(load_config(self.root).is_enterprise)

    def test_enabled_shorthand(self) -> None:
        self._write("""\
            [enterprise]
            enabled = true
        """)
        cfg = load_config(self.root)
        self.assertEqual(cfg.mode, MODE_ENTERPRISE)

    def test_unknown_mode_is_fatal(self) -> None:
        """A typo must not silently leave the instance wide open."""
        self._write("""\
            [enterprise]
            mode = "entrprise"
        """)
        with self.assertRaises(SystemExit):
            load_config(self.root)


class SmtpConfigTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.local = self.root / "pm_studio_local"
        self.local.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write(self, body: str) -> None:
        (self.local / "config.toml").write_text(textwrap.dedent(body))

    def test_full_smtp_table(self) -> None:
        self._write("""\
            [smtp]
            host = "smtp.example.com"
            port = 2525
            from_address = "studio@example.com"
            username = "studio"
            password = "hunter2hunter2"
            use_tls = false
        """)
        smtp = load_config(self.root).smtp
        self.assertEqual(smtp.host, "smtp.example.com")
        self.assertEqual(smtp.port, 2525)
        self.assertFalse(smtp.use_tls)
        self.assertTrue(smtp.is_usable)

    def test_password_env_is_preferred_over_inline(self) -> None:
        """So the secret never has to sit in a file the operator might commit."""
        self._write("""\
            [smtp]
            host = "smtp.example.com"
            from_address = "studio@example.com"
            password = "in-the-file"
            password_env = "PM_STUDIO_TEST_SMTP_PASSWORD"
        """)
        with mock.patch.dict(os.environ, {"PM_STUDIO_TEST_SMTP_PASSWORD": "from-the-env"}):
            self.assertEqual(load_config(self.root).smtp.password, "from-the-env")

    def test_incomplete_smtp_is_not_usable(self) -> None:
        """A half-filled table must degrade to copyable invite links, not crash."""
        self._write("""\
            [smtp]
            port = 587
        """)
        smtp = load_config(self.root).smtp
        self.assertIsNotNone(smtp)
        self.assertFalse(smtp.is_usable)


if __name__ == "__main__":
    unittest.main()
