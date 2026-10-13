"""Tests for the local-env supervisor's three answers to "who is using this machine":
waiting for readiness, stopping abandoned session environments, and making room at
the session-environment limit.

What these pin down:

1. **wait() answers with the outcome, not after a fixed sleep.** It returns as soon as
   the service is running or has failed, and says when it gave up instead.
2. **Idle stop only takes what nobody uses.** A session's own services stop after
   `idle_stop_minutes` without a local-env call - never shared ones, never a session
   with a dev task running - and everything provisioned (the temporary database, the
   prepared checkout) stays, so starting again is only process startup. The stopped
   service says why.
3. **The limit makes room before it refuses.** The least recently active other session
   idle for a minute gives up its slot; a busy or recently active one never does, and
   then the 409 is exactly what it was.

Like test_local_env.py, these run REAL short-lived Python processes; only the clock the
idle logic reads is fake (the injected `clock`), so no test waits minutes.
"""

import io
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest import mock

from fastapi import HTTPException

from pm_studio import server as server_module
from pm_studio.config import LocalEnvSpec, LocalResourceSpec, LocalServiceSpec, SystemSpec, load_config
from pm_studio.local_env import InvalidParams, LimitReached, LocalEnvManager

from tests.test_local_env import SessionEnvTestBase, _port_open


class IdleTestBase(SessionEnvTestBase):
    SESSIONS = ("s1", "s2", "s3")

    def setUp(self) -> None:
        super().setUp()
        self.now = [1_000_000.0]
        self.busy: set[str] = set()

    def _clock(self) -> float:
        return self.now[0]

    def _advance(self, seconds: float) -> None:
        self.now[0] += seconds

    def _manager_idle(self, services, resources=(), max_envs: int = 3, idle: int = 5) -> LocalEnvManager:
        spec = SystemSpec(
            label="System",
            path="src/sys",
            local=LocalEnvSpec(services=tuple(services), resources=tuple(resources)),
        )
        manager = LocalEnvManager(
            {"sys": spec},
            self.root,
            log_dir=self.root / "logs",
            session_lookup=self._lookup,
            state_path=self.state_path,
            max_session_envs=max_envs,
            idle_stop_minutes=idle,
            session_busy=lambda session_id: session_id in self.busy,
            prepare_checkout=self.prepare,
            clock=self._clock,
        )
        self.managers.append(manager)
        return manager

    def _api(self, **extra) -> LocalServiceSpec:
        return LocalServiceSpec(id="api", command=self._cmd("{port}", "serve"), port="auto", **extra)

    def _closed(self, port: int) -> bool:
        deadline = time.monotonic() + 10
        while _port_open(port) and time.monotonic() < deadline:
            time.sleep(0.1)
        return not _port_open(port)


# ---- wait ----


class WaitTest(IdleTestBase):
    def test_wait_returns_once_running(self) -> None:
        manager = self._manager_idle([self._api()])
        self.assertEqual(manager.start("sys", "api", session="s1")["state"], "starting")
        result = manager.wait("sys", "api", session="s1", timeout=30)
        self.assertEqual(result["state"], "running")
        self.assertFalse(result["timed_out"])
        self.assertIsInstance(result["waited_seconds"], float)
        self.assertLess(result["waited_seconds"], 30)
        self.assertEqual(result["session"], "s1")
        # Not starting: answered at once.
        stopped = manager.wait("sys", "api", session="s2", timeout=30)
        self.assertEqual((stopped["state"], stopped["timed_out"], stopped["waited_seconds"]), ("stopped", False, 0.0))

    def test_wait_returns_on_failure(self) -> None:
        manager = self._manager_idle([LocalServiceSpec(id="api", command=self._cmd("{port}", "fail"), port="auto")])
        manager.start("sys", "api")
        result = manager.wait("sys", "api", timeout=30)
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["exit_code"], 3)
        self.assertFalse(result["timed_out"])

    def test_wait_times_out(self) -> None:
        manager = self._manager_idle([
            LocalServiceSpec(id="api", command=self._cmd("{port}", "idle"), port="auto", ready_timeout=120),
        ])
        manager.start("sys", "api")
        result = manager.wait("sys", "api", timeout=1)
        self.assertEqual(result["state"], "starting")
        self.assertTrue(result["timed_out"])
        self.assertGreaterEqual(result["waited_seconds"], 0.9)
        self.assertLess(result["waited_seconds"], 10)

    def test_wait_validates_timeout(self) -> None:
        manager = self._manager_idle([self._api()])
        for bad in ("soon", None, float("nan")):
            with self.subTest(timeout=bad), self.assertRaises(InvalidParams):
                manager.wait("sys", "api", timeout=bad)
        # Negative is "do not wait", not an error.
        self.assertFalse(manager.wait("sys", "api", timeout=-5)["timed_out"])

    def test_wait_route(self) -> None:
        manager = self._manager_idle([self._api()])
        for name, value in (("local_env", manager), ("audit_log", mock.Mock())):
            patcher = mock.patch.object(server_module, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        request = mock.Mock()
        server_module.local_env_start("sys", "api", request, None, session="s1")
        result = server_module.local_env_wait("sys", "api", request, timeout=30, session="s1")
        self.assertEqual((result["state"], result["timed_out"]), ("running", False))
        for call in (
            lambda: server_module.local_env_wait("sys", "nope", request, timeout=1, session="s1"),
            lambda: server_module.local_env_wait("sys", "api", request, timeout=1, session="nope"),
        ):
            with self.assertRaises(HTTPException) as caught:
                call()
            self.assertEqual(caught.exception.status_code, 404)


# ---- idle stop ----


class IdleStopTest(IdleTestBase):
    def _system(self, manager_kwargs=None) -> LocalEnvManager:
        db = LocalResourceSpec(id="db", create=self._res_cmd(), destroy=self._drop_cmd(), timeout=60.0)
        return self._manager_idle(
            [
                LocalServiceSpec(id="secrets", command=self._cmd("{port}", "serve"), port="auto", scope="shared"),
                self._api(depends_on=("secrets", "db")),
            ],
            [db],
            **(manager_kwargs or {}),
        )

    def _up(self, manager, session: str) -> dict:
        manager.start("sys", "api", session=session)
        status = manager.wait("sys", "api", session=session, timeout=30)
        self.assertEqual(status["state"], "running", status["message"])
        return status

    def test_only_idle_session_runs_stop_and_keep_what_they_provisioned(self) -> None:
        manager = self._system()
        s1 = self._up(manager, "s1")
        s2 = self._up(manager, "s2")
        secrets = manager.service_status("sys", "secrets", session="s1")
        self.assertEqual(self._created(), 2)
        # One checkout per session (later starts only refresh its links).
        self.assertEqual(len({c.args[1] for c in self.prepare.call_args_list}), 2)

        self._advance(4 * 60)
        self.assertEqual(manager.reap_idle(), [])
        # s2 does something; s1 does not.
        manager.logs("sys", "api", session="s2")
        self._advance(61)
        err = io.StringIO()
        with mock.patch("sys.stderr", err):
            report = manager.reap_idle()
        self.assertEqual(report, [{"session": "s1", "stopped": ["sys/api"], "idle_minutes": 5}])
        self.assertIn("stopped session Chat s1 (s1)'s environment after 5 min without use: sys/api", err.getvalue())

        api = manager.service_status("sys", "api", session="s1")
        self.assertEqual(api["state"], "stopped")
        self.assertIsNone(api["pid"])
        self.assertEqual(api["message"], "Stopped after 5 min without use; start it again when needed.")
        self.assertTrue(self._closed(s1["port"]), "the idle service's process survived")
        # The other session, and the shared service, are untouched.
        self.assertEqual(manager.service_status("sys", "api", session="s2")["state"], "running")
        self.assertTrue(_port_open(s2["port"]))
        self.assertEqual(manager.service_status("sys", "secrets", session="s1")["state"], "running")
        self.assertTrue(_port_open(secrets["port"]))
        # Nothing provisioned was given back.
        self.assertEqual(manager.resource_status("sys", "db", session="s1")["state"], "ready")
        self.assertFalse(self.dropped.exists())
        self.assertEqual(manager.system_status("sys", session="s1")["checkout"]["state"], "ready")
        self.assertEqual(manager.overview()["active_session_envs"], 1)

        # Starting again is just process startup: no new database, and the checkout stays
        # prepared (a start only refreshes its links).
        self._up(manager, "s1")
        self.assertEqual(self._created(), 2)
        self.assertEqual(manager.system_status("sys", session="s1")["checkout"]["state"], "ready")
        self.assertEqual(manager.service_status("sys", "api", session="s1")["message"], "")

    def test_activity_refreshes_the_timer(self) -> None:
        manager = self._system()
        self._up(manager, "s1")
        for call in (
            lambda: manager.status(session="s1"),
            lambda: manager.system_status("sys", session="s1"),
            lambda: manager.service_status("sys", "api", session="s1"),
            lambda: manager.logs("sys", "api", session="s1"),
            lambda: manager.wait("sys", "api", session="s1", timeout=0),
            lambda: manager.start("sys", "api", session="s1"),
        ):
            self._advance(4 * 60)
            call()
            self.assertEqual(manager.reap_idle(), [])
        self._advance(5 * 60)
        with mock.patch("sys.stderr"):
            self.assertEqual([r["session"] for r in manager.reap_idle()], ["s1"])

    def test_service_output_counts_as_activity(self) -> None:
        # Someone using the app the environment serves makes no local-env call; the
        # requests it logs are what keep the environment up.
        manager = self._system()
        self._up(manager, "s1")
        log = manager.log_path("sys", "api", "s1")
        self.assertEqual(manager.reap_idle(), [])  # baseline: starting already counted
        for _ in range(3):
            self._advance(4 * 60)
            with log.open("a", encoding="utf-8") as handle:
                handle.write("[gateway] GET /auth/claims -> 200\n")
            self.assertEqual(manager.reap_idle(), [])
        # Silence afterwards: stopped five minutes after the last line.
        self._advance(4 * 60)
        self.assertEqual(manager.reap_idle(), [])
        self._advance(61)
        with mock.patch("sys.stderr"):
            self.assertEqual([r["session"] for r in manager.reap_idle()], ["s1"])

    def test_busy_session_is_never_stopped_and_busy_counts_as_activity(self) -> None:
        manager = self._system()
        self._up(manager, "s1")
        self.busy.add("s1")
        self._advance(60 * 60)
        self.assertEqual(manager.reap_idle(), [])
        # The task ended just now: the countdown starts from the last time it was busy.
        self.busy.clear()
        self.assertEqual(manager.reap_idle(), [])
        self._advance(4 * 60)
        self.assertEqual(manager.reap_idle(), [])
        self._advance(61)
        with mock.patch("sys.stderr"):
            self.assertEqual([r["session"] for r in manager.reap_idle()], ["s1"])

    def test_a_busy_hook_that_raises_counts_as_busy(self) -> None:
        manager = self._system()
        manager._session_busy = mock.Mock(side_effect=RuntimeError("no runtime"))
        self._up(manager, "s1")
        self._advance(60 * 60)
        self.assertEqual(manager.reap_idle(), [])
        self.assertEqual(manager.service_status("sys", "api", session="s1")["state"], "running")

    def test_shared_services_alone_are_never_idle_stopped(self) -> None:
        manager = self._system()
        manager.start("sys", "secrets", session="s1")
        self.assertEqual(manager.wait("sys", "secrets", session="s1", timeout=30)["state"], "running")
        self._advance(60 * 60)
        self.assertEqual(manager.reap_idle(), [])
        self.assertEqual(manager.service_status("sys", "secrets", session="s1")["state"], "running")

    def test_off_by_default(self) -> None:
        manager = self._system({"idle": 0})
        self._up(manager, "s1")
        self._advance(24 * 3600)
        self.assertEqual(manager.reap_idle(), [])
        self.assertFalse(manager.start_idle_reaper())
        self.assertEqual(manager.service_status("sys", "api", session="s1")["state"], "running")

    def test_reaper_thread_stops_idle_sessions_until_shutdown(self) -> None:
        manager = self._system({"idle": 1})
        self._up(manager, "s1")
        self._advance(2 * 60)
        with mock.patch("sys.stderr"):
            self.assertTrue(manager.start_idle_reaper(interval=0.05))
            self.assertTrue(manager.start_idle_reaper(interval=0.05))  # idempotent
            deadline = time.monotonic() + 10
            # Watched through overview(), which is no session's activity: polling
            # service_status(session="s1") would itself keep s1 alive.
            while manager.overview()["active_session_envs"]:
                self.assertLess(time.monotonic(), deadline, "the reaper never stopped the idle session")
                time.sleep(0.05)
        self.assertIn("without use", manager.service_status("sys", "api", session="s1")["message"])
        reaper = manager._reaper
        manager.shutdown()
        reaper.join(timeout=5)
        self.assertFalse(reaper.is_alive())

    def test_release_forgets_the_activity(self) -> None:
        manager = self._system()
        self._up(manager, "s1")
        manager.release_session("s1")
        self.assertNotIn("s1", manager._last_activity)


# ---- making room at the limit ----


class MakeRoomTest(IdleTestBase):
    def _up(self, manager, session: str) -> None:
        manager.start("sys", "api", session=session)
        self.assertEqual(manager.wait("sys", "api", session=session, timeout=30)["state"], "running")

    def test_least_recently_active_idle_session_makes_room(self) -> None:
        manager = self._manager_idle([self._api()], max_envs=2, idle=0)
        self._up(manager, "s1")
        self._advance(30)
        self._up(manager, "s2")
        port_s1 = manager.service_status("sys", "api", session="s1")["port"]
        self._advance(70)  # s1 idle 100 s, s2 70 s: both eligible, s1 is older
        err = io.StringIO()
        with mock.patch("sys.stderr", err):
            started = manager.start("sys", "api", session="s3")
        self.assertEqual(started["state"], "starting")
        self.assertEqual(manager.wait("sys", "api", session="s3", timeout=30)["state"], "running")
        evicted = manager.service_status("sys", "api", session="s1")
        self.assertEqual(evicted["state"], "stopped")
        self.assertEqual(
            evicted["message"],
            "Stopped to make room for session Chat s3 (limit of 2 session environments reached) "
            "after 1 min without use; start it again when needed.",
        )
        self.assertTrue(self._closed(port_s1))
        self.assertIn("to make room for session s3", err.getvalue())
        self.assertEqual(manager.service_status("sys", "api", session="s2")["state"], "running")

    def test_busy_or_recently_active_sessions_are_never_evicted(self) -> None:
        manager = self._manager_idle([self._api()], max_envs=2, idle=0)
        self._up(manager, "s1")
        self._up(manager, "s2")
        self.busy.add("s1")
        self._advance(50)
        # s1 is busy, s2 was active 50 s ago: the refusal is the one it always was.
        with self.assertRaises(LimitReached) as caught:
            manager.start("sys", "api", session="s3")
        self.assertIn("Chat s1", str(caught.exception))
        self.assertIn("Chat s2", str(caught.exception))
        with self.assertRaises(LimitReached):
            manager.start_all("sys", session="s3")
        self.assertEqual(manager.service_status("sys", "api", session="s3")["state"], "stopped")
        for sid in ("s1", "s2"):
            self.assertEqual(manager.service_status("sys", "api", session=sid)["state"], "running")

        # Later s2 qualifies - and is the one to go, although s1 is older: s1 is busy.
        self._advance(100)
        with mock.patch("sys.stderr"):
            manager.restart("sys", "api", session="s3")
        self.assertEqual(manager.service_status("sys", "api", session="s1")["state"], "running")
        self.assertEqual(manager.service_status("sys", "api", session="s2")["state"], "stopped")
        self.assertEqual(manager.wait("sys", "api", session="s3", timeout=30)["state"], "running")

    def test_a_refused_request_evicts_nobody(self) -> None:
        manager = self._manager_idle(
            [LocalServiceSpec(id="api", command=self._cmd("{port}", "serve", "{greeting}"), port="auto",
                              params=(("greeting", "hi"),))],
            max_envs=1, idle=0,
        )
        self._up(manager, "s1")
        self._advance(600)
        with self.assertRaises(InvalidParams):
            manager.start("sys", "api", {"greeting": "a;b"}, session="s2")
        self.assertEqual(manager.service_status("sys", "api", session="s1")["state"], "running")


# ---- config ----


class IdleStopConfigTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "pm_studio_local").mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write(self, config: str):
        (self.root / "pm_studio_local" / "config.toml").write_text(textwrap.dedent(config))
        return load_config(self.root)

    def test_default_is_never(self) -> None:
        self.assertEqual(self._write("").local_env.idle_stop_minutes, 0)
        self.assertEqual(self._write("[local_env]\nmax_session_envs = 3\n").local_env.idle_stop_minutes, 0)

    def test_valid_values(self) -> None:
        for value in (0, 1, 30):
            with self.subTest(value=value):
                cfg = self._write(f"[local_env]\nidle_stop_minutes = {value}\n")
                self.assertEqual(cfg.local_env.idle_stop_minutes, value)
                self.assertEqual(cfg.local_env.max_session_envs, 2)

    def test_invalid_values_are_fatal(self) -> None:
        for value in ("-1", '"5"', "true", "1.5", "[5]"):
            with self.subTest(value=value):
                err = io.StringIO()
                with mock.patch("sys.stderr", err), self.assertRaises(SystemExit):
                    self._write(f"[local_env]\nidle_stop_minutes = {value}\n")
                self.assertIn("idle_stop_minutes", err.getvalue())


if __name__ == "__main__":
    unittest.main()


class StablePortTest(unittest.TestCase):
    """A relaunched auto-port service takes its previous port again when it is free -
    dependents were rendered with it (a web app proxying to the API's port)."""

    def test_prefer_returns_the_previous_port_when_free(self) -> None:
        from pm_studio import local_env as le

        manager = le.LocalEnvManager({}, Path(tempfile.gettempdir()))
        free = le._free_port()
        self.assertEqual(manager._allocate_port_locked(prefer=free), free)

    def test_prefer_is_skipped_when_the_port_is_taken(self) -> None:
        import socket
        from pm_studio import local_env as le

        manager = le.LocalEnvManager({}, Path(tempfile.gettempdir()))
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            sock.listen()
            busy = sock.getsockname()[1]
            self.assertNotEqual(manager._allocate_port_locked(prefer=busy), busy)
