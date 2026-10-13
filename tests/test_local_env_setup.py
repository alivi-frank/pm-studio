"""Tests for a local service's optional `setup` command: what a service needs installed
(npm dependencies, typically) run automatically before every launch, so nobody has to
remember to run it by hand.

What these pin down:

1. **Order.** setup -> session checkout (prepared, or its links refreshed so what the
   setup just installed in the primary checkout reaches the session) -> dependencies ->
   command. Setup runs from the primary checkout, with the service's params, {session},
   {worktree}, {repo_root} and the `env` entries that need nothing launch-time.
2. **Its outcome decides the launch.** A non-zero exit or a timeout fails the start
   with a message saying why (the setup's last output line, or the limit) and the
   command never runs; its output is in the service's logs either way.
3. **Stop really stops it.** A stop (or shutdown) while setup runs kills the setup's
   process tree and nothing is launched afterwards.
4. **Config mistakes are loud.** `setup` may not use what does not exist yet when it
   runs ({port}, a dependency's port or a resource's output), `setup_timeout` must be a
   positive number, and unknown keys are still refused.

Like test_local_env.py, these run REAL short-lived Python processes.
"""

import io
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest import mock

from pm_studio.config import LocalEnvSpec, LocalServiceSpec, SystemSpec, load_config
from pm_studio.local_env import LocalEnvManager

from tests.test_local_env import SessionEnvTestBase, _quote


# Records that it ran (appends `name` to the events file), prints what a test checks,
# then behaves by mode: ok (exit 0), fail (exit 7 after a recognisable last line), hang
# (append a tick to `<events>.ticks` every 0.1s, forever), idle (never exits: a service
# with no probe), serve (idle, listening on the port given as its 4th argument).
EVENT_SCRIPT = """\
import os, sys, time
events, name, mode = sys.argv[1], sys.argv[2], sys.argv[3]
with open(events, "a") as fh:
    fh.write(name + "\\n")
print("cwd " + os.getcwd().replace(os.sep, "/"), flush=True)
print("env SETUP_ENV=" + os.environ.get("SETUP_ENV", "<unset>"), flush=True)
print("env NEEDS_PORT=" + os.environ.get("NEEDS_PORT", "<unset>"), flush=True)
print("args " + " ".join(sys.argv[4:]), flush=True)
if mode == "ok":
    sys.exit(0)
if mode == "fail":
    print("npm ERR! missing package left-pad", flush=True)
    print("", flush=True)
    sys.exit(7)
if mode == "hang":
    while True:
        with open(events + ".ticks", "a") as fh:
            fh.write(".")
        time.sleep(0.1)
if mode == "serve":
    import socket
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", int(sys.argv[4])))
    srv.listen(5)
while True:
    time.sleep(1)
"""


class SetupTestBase(SessionEnvTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.events = self.root / "events.txt"
        self.ticks = Path(str(self.events) + ".ticks")
        self.event_script = self.root / "event.py"
        self.event_script.write_text(EVENT_SCRIPT)
        # The checkout preparation hook records when it ran, in the same event stream.
        self.prepare = mock.Mock(side_effect=self._prepared)

    def _prepared(self, *args):
        with open(self.events, "a") as fh:
            fh.write("prepare\n")
        return ["linked node_modules"]

    def _ev(self, name: str, mode: str, *extra: str) -> str:
        return " ".join([
            _quote(sys.executable), _quote(str(self.event_script)), _quote(str(self.events)),
            name, mode, *extra,
        ])

    def _events(self) -> list[str]:
        return self.events.read_text().split() if self.events.exists() else []

    def _ticks(self) -> int:
        return len(self.ticks.read_text()) if self.ticks.exists() else 0

    def _wait_for(self, predicate, what: str, timeout: float = 20.0) -> None:
        deadline = time.monotonic() + timeout
        while not predicate():
            if time.monotonic() > deadline:
                self.fail(f"timed out waiting for {what}")
            time.sleep(0.05)


class SetupRunTest(SetupTestBase):
    def test_setup_runs_first_with_its_values_and_its_output_is_logged(self) -> None:
        service = LocalServiceSpec(
            id="api",
            command=self._ev("command", "serve", "{port}"),
            port="auto",
            setup=self._ev("setup", "ok", "apis={apis}", "session={session}", "wt={worktree}", "root={repo_root}"),
            params=(("apis", "a,b"),),
            env=(("SETUP_ENV", "hello-{session}"), ("NEEDS_PORT", "{port}")),
        )
        manager = self._session_manager([service])
        first = manager.start("sys", "api", session="s1")
        self.assertEqual(first["state"], "starting")
        self.assertEqual(first["setup"], service.setup)
        self.assertIsNone(first["pid"])
        status = self._wait_s(manager, "api", {"running"}, session="s1")
        self.assertEqual(status["setup"], service.setup)

        # setup -> checkout preparation -> command.
        self.assertEqual(self._events(), ["setup", "prepare", "command"])

        logs = manager.logs("sys", "api", session="s1")
        root = self.root.resolve().as_posix()
        worktree = self.worktrees["s1"].resolve().as_posix()
        header = next(i for i, line in enumerate(logs) if "] " in line and " setup in " in line)
        self.assertTrue(logs[header].startswith("[pm-studio] "), logs)
        self.assertIn(f"args apis=a,b session=s1 wt={worktree} root={root}", "\n".join(logs))
        setup_lines = [line for line in logs if line.startswith("[setup] ")]
        # From the primary checkout, with the env entries that need nothing launch-time.
        cwd = next(line[len("[setup] cwd "):] for line in setup_lines if line.startswith("[setup] cwd "))
        self.assertEqual(Path(cwd).resolve(), self.root.resolve())
        self.assertIn("[setup] env SETUP_ENV=hello-s1", setup_lines)
        self.assertIn("[setup] env NEEDS_PORT=<unset>", setup_lines)
        self.assertTrue(any("setup exited with code 0" in line for line in logs), logs)
        # The service's own output comes after the setup's, and its env is complete.
        starting = next(i for i, line in enumerate(logs) if " starting in " in line)
        self.assertGreater(starting, header)
        self.assertIn(f"env NEEDS_PORT={status['port']}", logs[starting:])
        # And the log file has the setup part too.
        text = Path(status["log_path"]).read_text(encoding="utf-8")
        self.assertIn("[setup] env SETUP_ENV=hello-s1", text)
        self.assertLess(text.index(" setup in "), text.index(" starting in "))

    def test_setup_runs_again_on_every_launch_and_refreshes_the_checkout(self) -> None:
        manager = self._session_manager([
            LocalServiceSpec(id="api", command=self._ev("command", "idle"), setup=self._ev("setup", "ok")),
        ])
        manager.start("sys", "api", session="s1")
        self._wait_s(manager, "api", {"running"}, session="s1")
        manager.restart("sys", "api", session="s1")
        self._wait_s(manager, "api", {"running"}, session="s1")
        # The second launch's checkout is already prepared: it is refreshed after the
        # setup anyway, so what the setup installed gets linked into the session.
        self.assertEqual(self._events(), ["setup", "prepare", "command"] * 2)

    def test_default_session_and_shared_service_run_setup_without_preparing(self) -> None:
        manager = self._session_manager([
            LocalServiceSpec(
                id="secrets", command=self._ev("secrets", "idle"), scope="shared",
                setup=self._ev("secrets-setup", "ok"),
            ),
            LocalServiceSpec(
                id="api", command=self._ev("command", "idle"), depends_on=("secrets",),
                setup=self._ev("setup", "ok"),
            ),
        ])
        manager.start("sys", "api")
        self._wait_s(manager, "api", {"running"})
        events = self._events()
        self.assertNotIn("prepare", events)
        self.assertLess(events.index("secrets-setup"), events.index("secrets"))
        self.assertLess(events.index("setup"), events.index("command"))
        # The dependent's command only ran once its dependency was up.
        self.assertLess(events.index("secrets"), events.index("command"))
        self.assertTrue(any(line.startswith("[setup] ") for line in manager.logs("sys", "secrets")))

    def test_failing_setup_fails_the_start_and_never_launches(self) -> None:
        manager = self._session_manager([
            LocalServiceSpec(id="api", command=self._ev("command", "idle"), port="auto", setup=self._ev("setup", "fail")),
        ])
        manager.start("sys", "api", session="s1")
        status = self._wait_s(manager, "api", {"failed"}, session="s1")
        self.assertEqual(status["message"], "Setup failed (exit 7): npm ERR! missing package left-pad")
        self.assertIsNone(status["pid"])
        self.assertIsNone(status["port"])
        time.sleep(0.5)
        # Neither the command nor the checkout preparation ran.
        self.assertEqual(self._events(), ["setup"])
        logs = manager.logs("sys", "api", session="s1")
        self.assertIn("[setup] npm ERR! missing package left-pad", logs)
        self.assertTrue(any("setup exited with code 7" in line for line in logs), logs)

    def test_setup_timeout_fails_the_start_and_kills_it(self) -> None:
        manager = self._session_manager([
            LocalServiceSpec(
                id="api", command=self._ev("command", "idle"), setup=self._ev("setup", "hang"),
                setup_timeout=1.0,
            ),
        ])
        manager.start("sys", "api")
        status = self._wait_s(manager, "api", {"failed"})
        self.assertEqual(status["message"], "Setup did not finish within 1s; stopped it.")
        ticks = self._ticks()
        time.sleep(0.6)
        self.assertEqual(self._ticks(), ticks, "the setup's process survived its timeout")
        self.assertEqual(self._events(), ["setup"])

    def test_stop_during_setup_kills_it_and_nothing_launches(self) -> None:
        manager = self._session_manager([
            LocalServiceSpec(id="api", command=self._ev("command", "idle"), setup=self._ev("setup", "hang")),
        ])
        manager.start("sys", "api", session="s1")
        self._wait_for(lambda: self._ticks() > 2, "the setup to be running")
        during = manager.service_status("sys", "api", session="s1")
        self.assertEqual(during["state"], "starting")
        self.assertEqual(during["message"], "Running setup...")
        self.assertIsNone(during["pid"])

        stopped = manager.stop("sys", "api", session="s1")
        self.assertEqual(stopped["state"], "stopped")
        ticks = self._ticks()
        time.sleep(0.6)
        self.assertEqual(self._ticks(), ticks, "the setup's process survived stop()")
        time.sleep(0.5)
        status = manager.service_status("sys", "api", session="s1")
        self.assertEqual(status["state"], "stopped")
        self.assertEqual(self._events(), ["setup"])

    def test_shutdown_during_setup_kills_it(self) -> None:
        manager = self._session_manager([
            LocalServiceSpec(id="api", command=self._ev("command", "idle"), setup=self._ev("setup", "hang")),
        ])
        manager.start("sys", "api")
        self._wait_for(lambda: self._ticks() > 2, "the setup to be running")
        manager.shutdown()
        ticks = self._ticks()
        time.sleep(0.6)
        self.assertEqual(self._ticks(), ticks, "the setup's process survived shutdown()")
        self.assertEqual(manager.service_status("sys", "api")["state"], "stopped")
        self.assertEqual(self._events(), ["setup"])

    def test_two_sessions_never_run_the_same_setup_at_once(self) -> None:
        # The same rendered command (no session-specific placeholder) from two sessions:
        # the second waits for the first, reported in its message.
        manager = self._session_manager([
            LocalServiceSpec(
                id="api", command=self._ev("command", "idle"), setup=self._ev("setup", "hang"),
                setup_timeout=3.0,
            ),
        ])
        manager.start("sys", "api", session="s1")
        self._wait_for(lambda: self._ticks() > 2, "the first setup to be running")
        manager.start("sys", "api", session="s2")
        self._wait_for(
            lambda: "waiting for the same setup" in manager.service_status("sys", "api", session="s2")["message"],
            "the second setup to wait",
        )
        self.assertEqual(self._events(), ["setup"])
        self._wait_s(manager, "api", {"failed"}, session="s1")
        self._wait_s(manager, "api", {"failed"}, session="s2")
        self.assertEqual(self._events(), ["setup", "setup"])


# ---- config ----


class SetupConfigTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "pm_studio_local").mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write(self, services: str):
        config = "[systems.s.local]\n\n" + textwrap.dedent(services)
        (self.root / "pm_studio_local" / "config.toml").write_text(config)
        return load_config(self.root)

    def _fatal(self, services: str) -> str:
        err = io.StringIO()
        with mock.patch("sys.stderr", err), self.assertRaises(SystemExit):
            self._write(services)
        return err.getvalue()

    def test_valid_setup_and_defaults(self) -> None:
        cfg = self._write("""\
            [[systems.s.local.services]]
            id = "dep"
            command = "x"
            port = 1234

            [[systems.s.local.services]]
            id = "api"
            command = "npm start -- --port {port}"
            port = "auto"
            depends_on = ["dep"]
            params = { apis = "" }
            setup = "  make -C {repo_root} epicride-backend-install APIS={apis} S={session} W={worktree} ${HOME}  "
            setup_timeout = 60
        """)
        dep, api = cfg.systems["s"].local.services
        self.assertEqual(dep.setup, "")
        self.assertEqual(dep.setup_timeout, 900.0)
        self.assertEqual(
            api.setup,
            "make -C {repo_root} epicride-backend-install APIS={apis} S={session} W={worktree} ${HOME}",
        )
        self.assertEqual(api.setup_timeout, 60.0)
        hash(cfg.systems["s"])

    def test_setup_using_launch_time_values_is_fatal(self) -> None:
        base = """\
            [[systems.s.local.resources]]
            id = "db"
            create = "c"

            [[systems.s.local.services]]
            id = "dep"
            command = "x"
            port = 1234

            [[systems.s.local.services]]
            id = "api"
            command = "y {port}"
            port = "auto"
            depends_on = ["dep", "db"]
            setup = "install {placeholder}"
        """
        for token in ("port", "dep.port", "db.name"):
            with self.subTest(token=token):
                message = self._fatal(base.replace("{placeholder}", "{" + token + "}"))
                self.assertIn("setup", message)
                self.assertIn("{" + token + "} does not exist yet when `setup` runs", message)

    def test_setup_undeclared_param_or_shared_session_is_fatal(self) -> None:
        message = self._fatal("""\
            [[systems.s.local.services]]
            id = "api"
            command = "y"
            setup = "install {apis}"
        """)
        self.assertIn("{apis} is not declared in `params`", message)
        message = self._fatal("""\
            [[systems.s.local.services]]
            id = "api"
            command = "y"
            scope = "shared"
            setup = "install {session}"
        """)
        self.assertIn("{session} is not available", message)

    def test_bad_setup_or_setup_timeout_is_fatal(self) -> None:
        for line in ("setup = 5", "setup = [\"a\"]", "setup_timeout = 0", "setup_timeout = -1",
                     'setup_timeout = "60"', "setup_timeout = true"):
            with self.subTest(line=line):
                message = self._fatal(f"""\
                    [[systems.s.local.services]]
                    id = "api"
                    command = "y"
                    {line}
                """)
                self.assertIn(line.split(" ")[0], message)

    def test_unknown_service_key_is_still_fatal(self) -> None:
        message = self._fatal("""\
            [[systems.s.local.services]]
            id = "api"
            command = "y"
            set_up = "install"
        """)
        self.assertIn("set_up", message)
        self.assertIn("setup_timeout", message)  # listed among the allowed keys


if __name__ == "__main__":
    unittest.main()
