"""Tests for the `python -m pm_studio` CLI run path: the `--port`/`--host` overrides,
precedence over config.toml, and hard-failure on bad values. `_serve()` is patched out
so no server is started; each test restores the module-level `config.CONFIG`."""

import contextlib
import io
import sys
import unittest
from unittest import mock

import pm_studio.config as config
from pm_studio import __main__ as cli


class MainCliTest(unittest.TestCase):
    def setUp(self) -> None:
        self._orig = config.CONFIG

    def tearDown(self) -> None:
        # Overrides mutate the module-level CONFIG; always put it back.
        config.CONFIG = self._orig

    def test_port_flag_overrides_config(self) -> None:
        with mock.patch.object(cli, "_serve") as serve, mock.patch.object(
            sys, "argv", ["pm-studio", "--port", "8005"]
        ):
            cli.main()
        serve.assert_called_once()
        self.assertEqual(config.CONFIG.port, 8005)
        # base_url is derived, so it must reflect the override too.
        self.assertTrue(config.CONFIG.base_url.endswith(":8005"))

    def test_host_flag_overrides_config(self) -> None:
        with mock.patch.object(cli, "_serve"), mock.patch.object(
            sys, "argv", ["pm-studio", "--host", "0.0.0.0", "--port", "8005"]
        ):
            cli.main()
        self.assertEqual(config.CONFIG.host, "0.0.0.0")
        self.assertEqual(config.CONFIG.port, 8005)

    def test_no_port_keeps_configured_default(self) -> None:
        with mock.patch.object(cli, "_serve") as serve, mock.patch.object(
            sys, "argv", ["pm-studio"]
        ):
            cli.main()
        serve.assert_called_once()
        self.assertEqual(config.CONFIG.port, self._orig.port)

    def test_non_integer_port_exits_non_zero(self) -> None:
        with mock.patch.object(sys, "argv", ["pm-studio", "--port", "notanint"]):
            with contextlib.redirect_stderr(io.StringIO()) as err:
                with self.assertRaises(SystemExit) as cm:
                    cli.main()
        self.assertNotEqual(cm.exception.code, 0)
        self.assertIn("port", err.getvalue().lower())
        # No silent fallback: config is untouched.
        self.assertEqual(config.CONFIG.port, self._orig.port)

    def test_missing_port_value_exits_non_zero(self) -> None:
        with mock.patch.object(sys, "argv", ["pm-studio", "--port"]):
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as cm:
                    cli.main()
        self.assertNotEqual(cm.exception.code, 0)

    def test_version_flag_does_not_serve(self) -> None:
        with mock.patch.object(cli, "_serve") as serve, mock.patch.object(
            sys, "argv", ["pm-studio", "--version"]
        ):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                cli.main()
        serve.assert_not_called()
        self.assertIn("pm-studio", out.getvalue())

    def test_version_subcommand_does_not_serve(self) -> None:
        with mock.patch.object(cli, "_serve") as serve, mock.patch.object(
            sys, "argv", ["pm-studio", "version"]
        ):
            with contextlib.redirect_stdout(io.StringIO()):
                cli.main()
        serve.assert_not_called()

    def test_init_does_not_serve(self) -> None:
        with mock.patch.object(cli, "_serve") as serve, mock.patch(
            "pm_studio.scaffold.run_init"
        ) as run_init, mock.patch.object(sys, "argv", ["pm-studio", "init"]):
            cli.main()
        serve.assert_not_called()
        run_init.assert_called_once()


if __name__ == "__main__":
    unittest.main()
