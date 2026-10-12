"""Tests for per-system local environments: the `[systems.<id>.local]` config table,
the LocalEnvManager supervisor, and the thin /local-env routes over it.

What these pin down, roughly in order of how much damage getting it wrong does:

1. **Only declared holes are fillable, and only with safe characters.** The command
   runs through the shell, so a param value that slipped a `;` past validation is
   arbitrary code execution. Rejection happens before anything starts.
2. **A port someone else holds is refused, not raced.** Launching a second copy would
   fail to bind at best, and at worst leave the developer talking to a stale process
   while PM Studio reports its own as "running".
3. **Dependencies come up first, and a dependent waits for them to be READY**, not
   merely launched - a backend booted before its secrets emulator listens just crashes.
4. **Stop really stops.** The command is a shell running the real server, so stopping
   only the shell would orphan the process actually holding the port.
5. **Config mistakes are loud.** A misspelt key or a dependency cycle in something
   PM Studio executes must refuse to start, like every other config error - and so must
   a placeholder nothing would fill.
6. **One environment per chat session.** Two sessions run the same system side by side
   on their own ports and resources, shared services exist once, the number of session
   environments is capped, and a session's environment (services AND provisioned
   resources) is given back when the session ends - never on a mere server restart.

The supervisor tests run REAL short-lived Python processes (a tiny TCP server written
to a temp dir) so the Windows and POSIX process-group/kill paths are both exercised on
whichever platform runs the suite.
"""

import dataclasses
import io
import json
import os
import shlex
import socket
import subprocess
import sys
import tempfile
import textwrap
import time
import types
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from tests._claude_stub import patch_claude
from zoneinfo import ZoneInfo

from fastapi import HTTPException

from pm_studio import local_env as local_env_module
from pm_studio import server as server_module
from pm_studio.accounts import User, agent_principal
from pm_studio.config import LocalEnvSpec, LocalResourceSpec, LocalServiceSpec, SystemSpec, load_config
from pm_studio.local_env import (
    Busy,
    InvalidParams,
    LimitReached,
    LocalEnvManager,
    PortInUse,
    SessionContext,
    UnknownSession,
    UnknownTarget,
    last_expiry_boundary,
    next_expiry_boundary,
)


# ---- config ----


class LocalEnvConfigTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "pm_studio_local").mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write(self, config: str):
        (self.root / "pm_studio_local" / "config.toml").write_text(textwrap.dedent(config))
        return load_config(self.root)

    def _fatal(self, config: str) -> None:
        with mock.patch("sys.stderr"), self.assertRaises(SystemExit):
            self._write(config)

    def test_system_without_local_is_untouched(self) -> None:
        cfg = self._write("""\
            [systems.rides]
            label = "Rides"
        """)
        self.assertIsNone(cfg.systems["rides"].local)

    def test_full_example(self) -> None:
        cfg = self._write("""\
            [systems.epicride]
            label = "EpicRide"
            path = "src/epicride"

            [systems.epicride.local]
            aws_profile = "epicride-dev"
            test_users_secret = "alivi-os/epicride/dev/test-users"

            [[systems.epicride.local.services]]
            id = "secrets"
            label = "Lambda secrets extension"
            command = "make epicride-secrets"
            port = 2773

            [[systems.epicride.local.services]]
            id = "api"
            command = "npm run start -- --apis {apis} ${HOME}"
            cwd = "src/epicride/backend"
            ready_url = "http://localhost:3001/health"
            url = "http://localhost:3001"
            depends_on = ["secrets"]
            params = { apis = "" }
            env = { NODE_ENV = "development" }
            ready_timeout = 60
        """)
        local = cfg.systems["epicride"].local
        self.assertEqual(local.aws_profile, "epicride-dev")
        self.assertEqual(local.test_users_secret, "alivi-os/epicride/dev/test-users")
        self.assertEqual([s.id for s in local.services], ["secrets", "api"])
        secrets, api = local.services
        self.assertEqual(secrets, LocalServiceSpec(
            id="secrets", command="make epicride-secrets",
            label="Lambda secrets extension", port=2773,
        ))
        self.assertEqual(api.cwd, "src/epicride/backend")
        self.assertIsNone(api.port)
        self.assertEqual(api.depends_on, ("secrets",))
        self.assertEqual(api.param_defaults, {"apis": ""})
        self.assertEqual(api.env_vars, {"NODE_ENV": "development"})
        self.assertEqual(api.ready_timeout, 60.0)
        # Still a hashable frozen value like every other spec.
        hash(cfg.systems["epicride"])

    def _service(self, body: str, extra: str = "") -> str:
        return f"""\
            [systems.s.local]
            {extra}

            [[systems.s.local.services]]
            {body}
        """

    def test_unknown_service_key_is_fatal(self) -> None:
        self._fatal(self._service('id = "a"\n            command = "x"\n            depend_on = ["b"]'))

    def test_unknown_local_key_is_fatal(self) -> None:
        self._fatal(self._service('id = "a"\n            command = "x"', extra='aws = "p"'))

    def test_missing_id_or_command_is_fatal(self) -> None:
        self._fatal(self._service('command = "x"'))
        self._fatal(self._service('id = "a"'))

    def test_bad_id_charset_is_fatal(self) -> None:
        self._fatal(self._service('id = "API_1"\n            command = "x"'))

    def test_duplicate_ids_are_fatal(self) -> None:
        self._fatal("""\
            [[systems.s.local.services]]
            id = "a"
            command = "x"

            [[systems.s.local.services]]
            id = "a"
            command = "y"
        """)

    def test_unknown_dependency_is_fatal(self) -> None:
        self._fatal(self._service('id = "a"\n            command = "x"\n            depends_on = ["nope"]'))

    def test_dependency_cycle_is_fatal(self) -> None:
        self._fatal("""\
            [[systems.s.local.services]]
            id = "a"
            command = "x"
            depends_on = ["c"]

            [[systems.s.local.services]]
            id = "b"
            command = "y"
            depends_on = ["a"]

            [[systems.s.local.services]]
            id = "c"
            command = "z"
            depends_on = ["b"]
        """)

    def test_bad_ports_are_fatal(self) -> None:
        for port in ("0", "70000", '"2773"', "true", "1.5"):
            with self.subTest(port=port):
                self._fatal(self._service(f'id = "a"\n            command = "x"\n            port = {port}'))

    def test_params_and_env_must_be_tables_of_strings(self) -> None:
        for line in ('params = { n = 1 }', 'params = ["n"]', 'env = { K = true }', 'env = "K=v"'):
            with self.subTest(line=line):
                self._fatal(self._service(f'id = "a"\n            command = "x"\n            {line}'))

    def test_undeclared_placeholder_is_fatal(self) -> None:
        self._fatal(self._service('id = "a"\n            command = "run {apis}"'))

    def test_shell_variable_braces_are_not_placeholders(self) -> None:
        cfg = self._write(self._service('id = "a"\n            command = "echo ${HOME}"'))
        self.assertEqual(cfg.systems["s"].local.services[0].command, "echo ${HOME}")

    def test_absolute_cwd_is_fatal(self) -> None:
        self._fatal(self._service('id = "a"\n            command = "x"\n            cwd = "/etc"'))


# ---- supervisor ----

# A tiny TCP server: prints its args, then (by mode) listens on the port, exits non-zero,
# listens briefly then exits, or never listens at all.
SERVER_SCRIPT = """\
import socket, sys, time
port, mode = int(sys.argv[1]), sys.argv[2]
print("hello " + " ".join(sys.argv[3:]), flush=True)
if mode == "fail":
    print("boom", flush=True)
    sys.exit(3)
if mode == "idle":
    while True:
        time.sleep(1)
srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
srv.bind(("127.0.0.1", port))
srv.listen(5)
print("listening on %d" % port, flush=True)
srv.settimeout(0.2)
deadline = time.time() + (1.5 if mode == "brief" else 600)
while time.time() < deadline:
    try:
        conn, _ = srv.accept()
        conn.close()
    except socket.timeout:
        pass
srv.close()
"""


def _quote(value: str) -> str:
    return subprocess.list2cmdline([value]) if os.name == "nt" else shlex.quote(value)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


class SupervisorTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.script = self.root / "srv.py"
        self.script.write_text(SERVER_SCRIPT)
        self.managers: list[LocalEnvManager] = []

    def tearDown(self) -> None:
        for manager in self.managers:
            manager.shutdown()
        self._tmp.cleanup()

    def _cmd(self, port: int, mode: str, *extra: str) -> str:
        parts = [_quote(sys.executable), _quote(str(self.script)), str(port), mode, *extra]
        return " ".join(parts)

    def _manager(self, *services: LocalServiceSpec, system: str = "sys") -> LocalEnvManager:
        spec = SystemSpec(
            label="System",
            local=LocalEnvSpec(aws_profile="dev-profile", services=tuple(services)),
        )
        manager = LocalEnvManager({system: spec, "plain": SystemSpec(label="Plain")}, self.root, log_dir=self.root / "logs")
        self.managers.append(manager)
        return manager

    def _wait(self, manager, service_id, states, timeout=20.0, system="sys") -> dict:
        deadline = time.monotonic() + timeout
        while True:
            status = manager.service_status(system, service_id)
            if status["state"] in states:
                return status
            if time.monotonic() > deadline:
                self.fail(f"{service_id} stuck in {status['state']} ({status['message']}); logs: {manager.logs(system, service_id)}")
            time.sleep(0.1)


class SupervisorTest(SupervisorTestBase):
    def test_start_ready_by_port_logs_and_stop(self) -> None:
        port = _free_port()
        manager = self._manager(LocalServiceSpec(id="web", command=self._cmd(port, "serve"), port=port, url="http://x"))
        first = manager.start("sys", "web")
        self.assertEqual(first["state"], "starting")
        status = self._wait(manager, "web", {"running"})
        self.assertIsNotNone(status["pid"])
        self.assertIsNotNone(status["started_at"])
        self.assertTrue(_port_open(port))
        logs = manager.logs("sys", "web")
        self.assertTrue(any(f"listening on {port}" in line for line in logs), logs)
        self.assertTrue(Path(status["log_path"]).is_file())

        # Starting again while running reuses it rather than launching a second copy.
        again = manager.start("sys", "web")
        self.assertEqual(again["pid"], status["pid"])

        stopped = manager.stop("sys", "web")
        self.assertEqual(stopped["state"], "stopped")
        self.assertIsNone(stopped["pid"])
        deadline = time.monotonic() + 10
        while _port_open(port) and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertFalse(_port_open(port), "the process tree survived stop()")

    def test_status_lists_only_systems_with_local(self) -> None:
        manager = self._manager(LocalServiceSpec(id="web", command="x", depends_on=()))
        status = manager.status()
        self.assertEqual([s["id"] for s in status], ["sys"])
        self.assertEqual(status[0]["aws_profile"], "dev-profile")
        self.assertEqual(status[0]["services"][0]["state"], "stopped")

    def test_foreign_port_is_refused(self) -> None:
        holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        holder.bind(("127.0.0.1", 0))
        holder.listen(1)
        port = holder.getsockname()[1]
        try:
            manager = self._manager(LocalServiceSpec(id="web", command=self._cmd(port, "serve"), port=port))
            with self.assertRaises(PortInUse) as caught:
                manager.start("sys", "web")
            self.assertIn(str(port), str(caught.exception))
            status = manager.service_status("sys", "web")
            self.assertEqual(status["state"], "port_in_use")
            self.assertIn(str(port), status["message"])
            self.assertIsNone(status["pid"])
        finally:
            holder.close()

    def test_exit_before_ready_is_failed(self) -> None:
        port = _free_port()
        manager = self._manager(LocalServiceSpec(id="web", command=self._cmd(port, "fail"), port=port))
        manager.start("sys", "web")
        status = self._wait(manager, "web", {"failed"})
        self.assertEqual(status["exit_code"], 3)
        self.assertTrue(any("boom" in line for line in manager.logs("sys", "web")))

    def test_not_ready_in_time_is_failed_and_killed(self) -> None:
        port = _free_port()
        manager = self._manager(LocalServiceSpec(id="web", command=self._cmd(port, "idle"), port=port, ready_timeout=1.5))
        manager.start("sys", "web")
        status = self._wait(manager, "web", {"failed"})
        self.assertIn("Not ready", status["message"])
        self.assertIsNone(status["pid"])

    def test_no_probe_counts_as_running_once_alive(self) -> None:
        manager = self._manager(LocalServiceSpec(id="worker", command=self._cmd(0, "idle")))
        manager.start("sys", "worker")
        self._wait(manager, "worker", {"running"})

    def test_death_after_ready_is_exited(self) -> None:
        port = _free_port()
        manager = self._manager(LocalServiceSpec(id="web", command=self._cmd(port, "brief"), port=port))
        manager.start("sys", "web")
        self._wait(manager, "web", {"running", "exited"})
        status = self._wait(manager, "web", {"exited"})
        self.assertEqual(status["exit_code"], 0)

    def test_dependency_starts_first_and_dependent_waits_for_it(self) -> None:
        port_a, port_b = _free_port(), _free_port()
        manager = self._manager(
            LocalServiceSpec(id="b", command=self._cmd(port_b, "serve"), port=port_b, depends_on=("a",)),
            LocalServiceSpec(id="a", command=self._cmd(port_a, "serve"), port=port_a),
        )
        b_now = manager.start("sys", "b")
        # A was launched but cannot be ready yet, so B is queued behind it, not launched.
        self.assertEqual(b_now["state"], "starting")
        self.assertIn("Waiting for a", b_now["message"])
        self.assertIsNone(b_now["pid"])
        a = self._wait(manager, "a", {"running"})
        b = self._wait(manager, "b", {"running"})
        self.assertLess(a["started_at"], b["started_at"])

        # Stopping the dependent leaves its dependency up; stop_all takes both down.
        manager.stop("sys", "b")
        self.assertEqual(manager.service_status("sys", "a")["state"], "running")
        result = manager.stop_all("sys")
        self.assertEqual({s["state"] for s in result["services"]}, {"stopped"})

    def test_failed_dependency_fails_the_dependent(self) -> None:
        port_a, port_b = _free_port(), _free_port()
        manager = self._manager(
            LocalServiceSpec(id="a", command=self._cmd(port_a, "fail"), port=port_a),
            LocalServiceSpec(id="b", command=self._cmd(port_b, "serve"), port=port_b, depends_on=("a",)),
        )
        manager.start("sys", "b")
        status = self._wait(manager, "b", {"failed"})
        self.assertIn("dependency a", status["message"])
        self.assertIsNone(status["pid"])

    def test_param_substitution(self) -> None:
        port = _free_port()
        service = LocalServiceSpec(
            id="web",
            command=self._cmd(port, "serve", "{greeting}"),
            port=port,
            params=(("greeting", "default-value"),),
        )
        manager = self._manager(service)
        manager.start("sys", "web", {"greeting": "api-one,api-two"})
        status = self._wait(manager, "web", {"running"})
        self.assertEqual(status["params"], {"greeting": "api-one,api-two"})
        self.assertTrue(any("hello api-one,api-two" in line for line in manager.logs("sys", "web")))

        # A restart without params repeats the last launch.
        manager.restart("sys", "web")
        self._wait(manager, "web", {"running"})
        self.assertTrue(any("hello api-one,api-two" in line for line in manager.logs("sys", "web")))

    def test_param_validation(self) -> None:
        service = LocalServiceSpec(id="web", command="echo {greeting}", params=(("greeting", "hi"),))
        manager = self._manager(service)
        for bad in ("x; rm -rf /", "a && b", "$(id)", "`id`", "a|b", "a\nb", 'q"', "a>b", "x" * 300):
            with self.subTest(value=bad), self.assertRaises(InvalidParams):
                manager.start("sys", "web", {"greeting": bad})
        with self.assertRaises(InvalidParams):
            manager.start("sys", "web", {"other": "x"})
        with self.assertRaises(InvalidParams):
            manager.start("sys", "web", {"greeting": True})
        with self.assertRaises(InvalidParams):
            manager.start_all("sys", {"web": {"greeting": "a;b"}})
        with self.assertRaises(InvalidParams):
            manager.start_all("sys", {"nope": {}})
        # Nothing was launched by any of the refusals.
        self.assertEqual(manager.service_status("sys", "web")["state"], "stopped")
        self.assertEqual(
            LocalEnvManager.effective_params(service, {"greeting": "user@example.com:8080/a=b c-d_e.f"}),
            {"greeting": "user@example.com:8080/a=b c-d_e.f"},
        )

    def test_unknown_targets(self) -> None:
        manager = self._manager(LocalServiceSpec(id="web", command="x"))
        with self.assertRaises(UnknownTarget):
            manager.start("nope", "web")
        with self.assertRaises(UnknownTarget):
            manager.start("plain", "web")  # declared system, but no `local`
        with self.assertRaises(UnknownTarget):
            manager.stop("sys", "nope")
        with self.assertRaises(UnknownTarget):
            manager.logs("sys", "nope")

    def test_log_dir_defaults_outside_the_repo(self) -> None:
        manager = LocalEnvManager({}, self.root)
        path = manager.log_path("sys", "web")
        self.assertEqual(path.parent, local_env_module.LOG_DIR)
        self.assertEqual(path.parent.parent, Path(tempfile.gettempdir()))


# ---- routes ----


class LocalEnvRoutesTest(SupervisorTestBase):
    """The handlers called directly (the suite has no HTTP client), with the module's
    manager swapped for one over a test system."""

    def setUp(self) -> None:
        super().setUp()
        self.port = _free_port()
        self.manager = self._manager(
            LocalServiceSpec(
                id="web", command=self._cmd(self.port, "serve", "{greeting}"),
                port=self.port, params=(("greeting", "hi"),),
            )
        )
        # audit_log too: in enterprise mode the routes audit, and the real log lives in
        # this repo's own workspace.
        for name, value in (("local_env", self.manager), ("audit_log", mock.Mock())):
            patcher = mock.patch.object(server_module, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.request = mock.Mock()

    def test_status_start_logs_stop(self) -> None:
        listing = server_module.local_env_status(self.request)
        self.assertEqual([s["id"] for s in listing["systems"]], ["sys"])

        started = server_module.local_env_start("sys", "web", self.request, {"params": {"greeting": "there"}})
        self.assertEqual(started["state"], "starting")
        self._wait(self.manager, "web", {"running"})
        logs = server_module.local_env_logs("sys", "web", self.request, lines=50)
        self.assertTrue(any("hello there" in line for line in logs["lines"]))

        stopped = server_module.local_env_stop_all("sys", self.request)
        self.assertEqual(stopped["services"][0]["state"], "stopped")

    def test_error_codes(self) -> None:
        with self.assertRaises(HTTPException) as caught:
            server_module.local_env_start("sys", "nope", self.request, None)
        self.assertEqual(caught.exception.status_code, 404)
        with self.assertRaises(HTTPException) as caught:
            server_module.local_env_start("sys", "web", self.request, {"params": {"greeting": "a;b"}})
        self.assertEqual(caught.exception.status_code, 400)
        with self.assertRaises(HTTPException) as caught:
            server_module.local_env_start_all("sys", self.request, {"params": "nope"})
        self.assertEqual(caught.exception.status_code, 400)

        holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        holder.bind(("127.0.0.1", self.port))
        holder.listen(1)
        try:
            with self.assertRaises(HTTPException) as caught:
                server_module.local_env_start("sys", "web", self.request, None)
            self.assertEqual(caught.exception.status_code, 409)
        finally:
            holder.close()

    def test_enterprise_authz_admits_the_agent_and_refuses_a_viewer(self) -> None:
        enterprise = dataclasses.replace(server_module.CONFIG, mode="enterprise")
        with mock.patch.object(server_module, "CONFIG", enterprise):
            agent_request = mock.Mock()
            agent_request.state.user = agent_principal()
            result = server_module.local_env_stop("sys", "web", agent_request)
            self.assertEqual(result["state"], "stopped")

            viewer_request = mock.Mock()
            viewer_request.state.user = User(
                id="v", email="v@x", name="V", role="viewer", status="active",
                created_at=0.0, updated_at=0.0,
            )
            with self.assertRaises(HTTPException) as caught:
                server_module.local_env_start("sys", "web", viewer_request, None)
            self.assertEqual(caught.exception.status_code, 403)
            # Reads stay open to every role.
            self.assertIn("systems", server_module.local_env_status(viewer_request))


# ---- per-session environments: config ----


class SessionEnvConfigTest(unittest.TestCase):
    """Scopes, auto ports, resources, link_from_primary, [local_env] and the placeholder
    rules - every one of which is checked at load, not at the first start."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "pm_studio_local").mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write(self, config: str):
        (self.root / "pm_studio_local" / "config.toml").write_text(textwrap.dedent(config))
        return load_config(self.root)

    def _fatal(self, config: str) -> str:
        err = io.StringIO()
        with mock.patch("sys.stderr", err), self.assertRaises(SystemExit):
            self._write(config)
        return err.getvalue()

    FULL = """\
        [local_env]
        max_session_envs = 3

        [systems.epicride]
        label = "EpicRide"
        path = "src/epicride"

        [systems.epicride.local]
        link_from_primary = ["src/epicride/frontend/node_modules", "src/epicride/backend/src/**/node_modules"]

        [[systems.epicride.local.resources]]
        id = "db"
        label = "Temporary database"
        create = "make db-create SESSION={session} ROOT={worktree}"
        destroy = "make db-drop NAME={db.name} SESSION={session}"
        timeout = 600
        outputs = ["name"]

        [[systems.epicride.local.services]]
        id = "secrets"
        scope = "shared"
        command = "make secrets"
        port = 2773

        [[systems.epicride.local.services]]
        id = "api"
        command = "npm start -- --port {port} --db {db.name} --secrets {secrets.port}"
        port = "auto"
        ready_url = "http://localhost:{port}/health"
        url = "http://localhost:{port}"
        depends_on = ["secrets", "db"]
        env = { DB_NAME = "{db.name}", WORKTREE = "{worktree}", SESSION = "{session}" }

        [[systems.epicride.local.services]]
        id = "web"
        command = "ng serve --port {port} --api {api.port} --db {db.name}"
        port = "auto"
        depends_on = ["api"]
    """

    def test_full_per_session_example(self) -> None:
        cfg = self._write(self.FULL)
        self.assertEqual(cfg.local_env.max_session_envs, 3)
        local = cfg.systems["epicride"].local
        self.assertEqual(
            local.link_from_primary,
            ("src/epicride/frontend/node_modules", "src/epicride/backend/src/**/node_modules"),
        )
        db = local.resource("db")
        self.assertEqual(db, LocalResourceSpec(
            id="db", label="Temporary database",
            create="make db-create SESSION={session} ROOT={worktree}",
            destroy="make db-drop NAME={db.name} SESSION={session}",
            timeout=600.0, outputs=("name",),
        ))
        self.assertEqual(db.scope, "session")
        secrets, api, web = local.services
        self.assertTrue(secrets.shared)
        self.assertEqual(secrets.fixed_port, 2773)
        self.assertEqual(api.scope, "session")
        self.assertTrue(api.auto_port)
        self.assertIsNone(api.fixed_port)
        # {db.name} reaches web through api: transitive dependencies count.
        self.assertEqual(local.closure("web"), ["api", "secrets", "db"])
        hash(cfg.systems["epicride"])

    def test_local_env_defaults(self) -> None:
        cfg = self._write("")
        self.assertEqual(cfg.local_env.max_session_envs, 2)

    def test_local_env_table_errors(self) -> None:
        for body in ("max_session_envs = 0", 'max_session_envs = "2"', "max_session_envs = true", "max_envs = 2"):
            with self.subTest(body=body):
                self._fatal(f"[local_env]\n{body}\n")

    def _svc(self, body: str, before: str = "") -> str:
        return f"""\
            [systems.s.local]

            {before}

            [[systems.s.local.services]]
            {body}
        """

    def test_structural_errors(self) -> None:
        api = '[[systems.s.local.services]]\n            id = "api"\n            command = "x"\n            port = "auto"'
        db = '[[systems.s.local.resources]]\n            id = "db"\n            create = "mk"'
        cases = {
            "bad scope": self._svc('id = "a"\n            command = "x"\n            scope = "global"'),
            "bad port word": self._svc('id = "a"\n            command = "x"\n            port = "random"'),
            "builtin param name": self._svc('id = "a"\n            command = "x {port}"\n            port = 9\n            params = { port = "1" }'),
            "resource without create": self._svc('id = "a"\n            command = "x"', before='[[systems.s.local.resources]]\n            id = "db"'),
            "resource unknown key": self._svc('id = "a"\n            command = "x"', before=db + '\n            cwd = "x"'),
            "resource bad outputs": self._svc('id = "a"\n            command = "x"', before=db + '\n            outputs = ["a-b"]'),
            "resource bad timeout": self._svc('id = "a"\n            command = "x"', before=db + "\n            timeout = 0"),
            "id shared by service and resource": self._svc('id = "db"\n            command = "x"', before=db),
            "shared on session resource": self._svc('id = "a"\n            command = "x"\n            scope = "shared"\n            depends_on = ["db"]', before=db),
            "shared on session service": self._svc('id = "a"\n            command = "x"\n            scope = "shared"\n            depends_on = ["api"]', before=api),
            "absolute link_from_primary": '[systems.s.local]\nlink_from_primary = ["/abs/node_modules"]\n',
            "link_from_primary not a list": '[systems.s.local]\nlink_from_primary = "node_modules"\n',
        }
        for name, config in cases.items():
            with self.subTest(name):
                self._fatal(config)

    def test_placeholder_errors(self) -> None:
        api = '[[systems.s.local.services]]\n            id = "api"\n            command = "x"\n            port = "auto"'
        noport = '[[systems.s.local.services]]\n            id = "worker"\n            command = "x"'
        db = '[[systems.s.local.resources]]\n            id = "db"\n            create = "mk"\n            destroy = "rm {db.name}"\n            outputs = ["name"]'
        cases = {
            "{port} without a port": (self._svc('id = "a"\n            command = "run {port}"'), "{port}"),
            "{session} in a shared service": (self._svc('id = "a"\n            command = "run {session}"\n            scope = "shared"'), "{session}"),
            "dependency not declared": (self._svc('id = "a"\n            command = "run {api.port}"', before=api), "neither depends on"),
            "dependency without port": (self._svc('id = "a"\n            command = "run {worker.port}"\n            depends_on = ["worker"]', before=noport), "declares no `port`"),
            "service key other than port": (self._svc('id = "a"\n            command = "run {api.host}"\n            depends_on = ["api"]', before=api), "only provides"),
            "undeclared resource output": (self._svc('id = "a"\n            command = "run {db.user}"\n            depends_on = ["db"]', before=db), "not 'user'"),
            "placeholder in env": (self._svc('id = "a"\n            command = "x"\n            env = { K = "{nope}" }'), "env.K"),
            "placeholder in url": (self._svc('id = "a"\n            command = "x"\n            url = "http://localhost:{port}"'), "url"),
            "worktree in destroy": (self._svc('id = "a"\n            command = "x"', before='[[systems.s.local.resources]]\n            id = "db"\n            create = "mk"\n            destroy = "rm {worktree}"'), "destroy"),
            "other resource in destroy": (self._svc('id = "a"\n            command = "x"', before=db + '\n\n            [[systems.s.local.resources]]\n            id = "db2"\n            create = "mk"\n            destroy = "rm {db.name}"'), "destroy"),
            "session in shared resource create": (self._svc('id = "a"\n            command = "x"', before='[[systems.s.local.resources]]\n            id = "db"\n            scope = "shared"\n            create = "mk {session}"'), "{session}"),
        }
        for name, (config, needle) in cases.items():
            with self.subTest(name):
                self.assertIn(needle, self._fatal(config))

    def test_repo_root_is_available_everywhere(self) -> None:
        cfg = self._write("""\
            [systems.s.local]

            [[systems.s.local.resources]]
            id = "db"
            create = "make -C {repo_root} db-create ROOT={worktree} S={session}"
            destroy = "make -C {repo_root} db-drop NAME={db.name} S={session}"

            [[systems.s.local.resources]]
            id = "cache"
            scope = "shared"
            create = "make -C {repo_root} cache"
            destroy = "make -C {repo_root} cache-drop {cache.name}"

            [[systems.s.local.services]]
            id = "secrets"
            scope = "shared"
            command = "make -C {repo_root} secrets ROOT={worktree}"
            url = "file://{repo_root}"
            depends_on = ["cache"]

            [[systems.s.local.services]]
            id = "api"
            command = "make -C {repo_root} api ROOT={worktree} PORT={port}"
            port = "auto"
            ready_url = "http://localhost:{port}/{repo_root}"
            env = { TOOLS = "{repo_root}/scripts" }
            depends_on = ["db", "secrets"]
        """)
        self.assertEqual(len(cfg.systems["s"].local.services), 2)
        # ...and reserved, like the other builtins.
        self.assertIn("repo_root", self._fatal(self._svc(
            'id = "a"\n            command = "x {repo_root}"\n            params = { repo_root = "." }'
        )))

    def test_valid_placeholders_and_shell_braces(self) -> None:
        cfg = self._write(self._svc(
            'id = "a"\n            command = "run {port} {session} {worktree} {greeting} ${HOME}"'
            '\n            port = 4100\n            params = { greeting = "hi" }',
        ))
        self.assertEqual(cfg.systems["s"].local.services[0].fixed_port, 4100)


# ---- per-session environments: supervisor ----

# A resource's create/drop. `create <session> <counter file> <behaviour>` counts its
# runs, writes noise to stdout and stderr, then (by behaviour) reports outputs as JSON,
# prints no JSON, exits non-zero, or reports a value with shell metacharacters.
RESOURCE_SCRIPT = """\
import json, sys
mode = sys.argv[1]
if mode == "create":
    session, counter, behaviour = sys.argv[2], sys.argv[3], sys.argv[4]
    try:
        with open(counter) as fh:
            n = int(fh.read() or 0) + 1
    except OSError:
        n = 1
    with open(counter, "w") as fh:
        fh.write(str(n))
    print("provisioning for " + session, flush=True)
    if len(sys.argv) > 5:
        print("root " + sys.argv[5], flush=True)
    print("a warning on stderr", file=sys.stderr, flush=True)
    if behaviour == "fail":
        sys.exit(2)
    if behaviour == "badjson":
        print("done, but no json", flush=True)
    elif behaviour == "evil":
        print(json.dumps({"name": "x; rm -rf /"}), flush=True)
    else:
        print(json.dumps({"name": "db_%s_%d" % (session, n)}), flush=True)
    print("", flush=True)
elif mode == "drop":
    with open(sys.argv[3], "a") as fh:
        fh.write(sys.argv[2] + "\\n")
    print("dropped " + sys.argv[2], flush=True)
    if len(sys.argv) > 4:
        print("root " + sys.argv[4], flush=True)
"""

# Prints one environment variable, then idles (no probe: running after the grace).
ENV_SCRIPT = """\
import os, sys, time
print("env " + sys.argv[1] + "=" + os.environ.get(sys.argv[1], "<unset>"), flush=True)
while True:
    time.sleep(1)
"""


class SessionEnvTestBase(SupervisorTestBase):
    """Managers over two real worktree-like directories (s1, s2) next to the default
    session, with the checkout preparation hook mocked."""

    SESSIONS = ("s1", "s2")

    def setUp(self) -> None:
        super().setUp()
        self.res_script = self.root / "res.py"
        self.res_script.write_text(RESOURCE_SCRIPT)
        self.env_script = self.root / "envprint.py"
        self.env_script.write_text(ENV_SCRIPT)
        self.counter = self.root / "created.txt"
        self.dropped = self.root / "dropped.txt"
        self.state_path = self.root / "ws" / "local-env" / "resources.json"
        self.worktrees = {sid: self.root / "wt" / sid for sid in self.SESSIONS}
        for path in self.worktrees.values():
            path.mkdir(parents=True)
        self.prepare = mock.Mock(return_value=["linked node_modules"])

    def _lookup(self, session_id: str):
        if session_id == "default":
            return SessionContext("default", "Main", self.root, primary=True)
        if session_id in self.worktrees:
            return SessionContext(session_id, f"Chat {session_id}", self.worktrees[session_id])
        return None

    def _session_manager(
        self,
        services,
        resources=(),
        max_envs: int = 2,
        lookup=None,
        prepare="default",
        link=("node_modules",),
    ) -> LocalEnvManager:
        spec = SystemSpec(
            label="System",
            path="src/sys",
            local=LocalEnvSpec(
                services=tuple(services), resources=tuple(resources), link_from_primary=tuple(link)
            ),
        )
        kwargs = {} if prepare is None else {"prepare_checkout": self.prepare if prepare == "default" else prepare}
        manager = LocalEnvManager(
            {"sys": spec},
            self.root,
            log_dir=self.root / "logs",
            session_lookup=lookup or self._lookup,
            state_path=self.state_path,
            max_session_envs=max_envs,
            **kwargs,
        )
        self.managers.append(manager)
        return manager

    def _wait_s(self, manager, service_id, states, session="default", timeout=30.0) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            status = manager.service_status("sys", service_id, session=session)
            if status["state"] in states:
                return status
            if time.monotonic() > deadline:
                self.fail(
                    f"{session}/{service_id} stuck in {status['state']} ({status['message']}); "
                    f"logs: {manager.logs('sys', service_id, session=session)}"
                )
            time.sleep(0.1)

    def _wait_r(self, manager, resource_id, states, session="default", timeout=30.0) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            status = manager.resource_status("sys", resource_id, session=session)
            if status["state"] in states:
                return status
            if time.monotonic() > deadline:
                self.fail(
                    f"{session}/{resource_id} stuck in {status['state']} ({status['message']}); "
                    f"logs: {manager.logs('sys', resource_id, session=session)}"
                )
            time.sleep(0.1)

    def _res_cmd(self, behaviour: str = "ok", shared: bool = False) -> str:
        session = "shared" if shared else "{session}"
        return " ".join([
            _quote(sys.executable), _quote(str(self.res_script)), "create", session,
            _quote(str(self.counter)), behaviour,
        ])

    def _drop_cmd(self, resource_id: str = "db") -> str:
        return " ".join([
            _quote(sys.executable), _quote(str(self.res_script)), "drop",
            "{" + resource_id + ".name}", _quote(str(self.dropped)),
        ])

    def _db(self, behaviour: str = "ok", destroy: bool = True, **extra) -> LocalResourceSpec:
        return LocalResourceSpec(
            id="db", create=self._res_cmd(behaviour), destroy=self._drop_cmd() if destroy else "",
            timeout=60.0, **extra,
        )

    def _created(self) -> int:
        return int(self.counter.read_text()) if self.counter.exists() else 0


class SessionEnvTest(SessionEnvTestBase):
    def test_two_sessions_run_the_same_system_on_their_own_auto_ports(self) -> None:
        manager = self._session_manager([
            LocalServiceSpec(id="api", command=self._cmd("{port}", "serve", "session={session}"), port="auto"),
            LocalServiceSpec(
                id="web", command=self._cmd("{port}", "serve", "api={api.port}"), port="auto",
                depends_on=("api",), url="http://127.0.0.1:{port}/",
            ),
        ])
        for sid in self.SESSIONS:
            first = manager.start("sys", "web", session=sid)
            self.assertEqual(first["state"], "starting")
            self.assertEqual(first["session"], sid)
        ports = {}
        for sid in self.SESSIONS:
            api = self._wait_s(manager, "api", {"running"}, session=sid)
            web = self._wait_s(manager, "web", {"running"}, session=sid)
            ports[sid] = (api["port"], web["port"])
            self.assertTrue(api["auto_port"])
            self.assertEqual(web["url"], f"http://127.0.0.1:{web['port']}/")
            self.assertTrue(_port_open(api["port"]) and _port_open(web["port"]))
            self.assertTrue(any(f"hello session={sid}" in line for line in manager.logs("sys", "api", session=sid)))
            self.assertTrue(
                any(f"hello api={api['port']}" in line for line in manager.logs("sys", "web", session=sid)),
                manager.logs("sys", "web", session=sid),
            )
            # Each session's log is its own file.
            self.assertIn(sid, Path(web["log_path"]).name)
        self.assertEqual(len({p for pair in ports.values() for p in pair}), 4, ports)

        # The default session has its own (stopped) copy, untouched by the others.
        self.assertEqual(manager.service_status("sys", "web")["state"], "stopped")
        # Each non-default session's checkout was prepared with the system's path and
        # links (again on later starts, to pick up new primary links); the default
        # session never is.
        self.assertEqual(
            sorted(set(c.args for c in self.prepare.call_args_list)),
            sorted(
                (self.root, self.worktrees[sid], "src/sys", ("node_modules",)) for sid in self.SESSIONS
            ),
        )
        status = manager.system_status("sys", session="s1")
        self.assertEqual(status["checkout"], {"state": "ready", "notes": ["linked node_modules"], "message": ""})
        self.assertEqual(manager.system_status("sys")["checkout"]["state"], "not_needed")

        overview = manager.overview()
        self.assertEqual(overview["active_session_envs"], 2)
        self.assertEqual(overview["max_session_envs"], 2)
        self.assertEqual(
            sorted((r["session"], r["session_name"], r["service"]) for r in overview["runs"]),
            [("s1", "Chat s1", "api"), ("s1", "Chat s1", "web"), ("s2", "Chat s2", "api"), ("s2", "Chat s2", "web")],
        )

        # Stopping one session's environment leaves the other's running.
        manager.stop_all("sys", session="s1")
        self.assertEqual(manager.service_status("sys", "web", session="s1")["state"], "stopped")
        self.assertEqual(manager.service_status("sys", "web", session="s2")["state"], "running")

    def test_shared_service_is_reused_by_every_session(self) -> None:
        manager = self._session_manager([
            LocalServiceSpec(id="secrets", command=self._cmd("{port}", "serve"), port="auto", scope="shared"),
            LocalServiceSpec(
                id="api", command=self._cmd("{port}", "serve", "secrets={secrets.port}"), port="auto",
                depends_on=("secrets",),
            ),
        ])
        manager.start("sys", "api")
        manager.start("sys", "api", session="s1")
        default_api = self._wait_s(manager, "api", {"running"})
        s1_api = self._wait_s(manager, "api", {"running"}, session="s1")
        shared_default = manager.service_status("sys", "secrets")
        shared_s1 = manager.service_status("sys", "secrets", session="s1")
        self.assertEqual(shared_default["session"], "shared")
        self.assertEqual(shared_default["pid"], shared_s1["pid"])
        self.assertEqual(shared_default["port"], shared_s1["port"])
        for sid in ("default", "s1"):
            self.assertTrue(any(
                f"hello secrets={shared_default['port']}" in line
                for line in manager.logs("sys", "api", session=sid)
            ))
        self.assertNotEqual(default_api["port"], s1_api["port"])
        overview = manager.overview()
        self.assertEqual([r["service"] for r in overview["runs"] if r["session"] == "shared"], ["secrets"])
        # The shared service is no session's environment.
        self.assertEqual(overview["active_session_envs"], 2)
        # Only s1's (session-scoped) api needed its checkout prepared; the shared
        # service and the default session never do.
        self.assertEqual(self.prepare.call_count, 1)
        self.assertEqual(self.prepare.call_args.args[1], self.worktrees["s1"])

        # s1 stopping its system leaves secrets up: the default session still uses it.
        manager.stop_all("sys", session="s1")
        self.assertEqual(manager.service_status("sys", "secrets")["state"], "running")
        manager.stop_all("sys")
        self.assertEqual(manager.service_status("sys", "secrets")["state"], "stopped")

    def test_max_session_envs_is_enforced(self) -> None:
        manager = self._session_manager(
            [
                LocalServiceSpec(id="secrets", command=self._cmd("{port}", "serve"), port="auto", scope="shared"),
                LocalServiceSpec(id="api", command=self._cmd("{port}", "serve"), port="auto"),
            ],
            max_envs=1,
        )
        manager.start("sys", "api", session="s1")
        with self.assertRaises(LimitReached) as caught:
            manager.start("sys", "api", session="s2")
        self.assertEqual(caught.exception.status_code, 409)
        self.assertIn("Chat s1", str(caught.exception))
        with self.assertRaises(LimitReached):
            manager.start_all("sys", session="s2")
        self.assertEqual(manager.service_status("sys", "api", session="s2")["state"], "stopped")
        # The same session may keep using its own environment, and a shared-only start
        # costs no session environment.
        manager.start("sys", "api", session="s1")
        manager.start("sys", "secrets", session="s2")
        self._wait_s(manager, "secrets", {"running"}, session="s2")
        # Once s1 stops, s2 may start.
        manager.stop_all("sys", session="s1")
        manager.start("sys", "api", session="s2")
        self._wait_s(manager, "api", {"running"}, session="s2")

    def test_fixed_port_serves_one_session_at_a_time(self) -> None:
        port = _free_port()
        manager = self._session_manager([LocalServiceSpec(id="api", command=self._cmd(port, "serve"), port=port)])
        manager.start("sys", "api", session="s1")
        with self.assertRaises(PortInUse) as caught:
            manager.start("sys", "api", session="s2")
        self.assertIn("Chat s1", str(caught.exception))
        self.assertEqual(manager.service_status("sys", "api", session="s2")["state"], "port_in_use")

    def test_repo_root_and_worktree_substitution(self) -> None:
        """{repo_root} is the primary checkout in every scope - tools from there, code
        from the session's worktree ({worktree})."""
        manager = self._session_manager([
            LocalServiceSpec(
                id="secrets", scope="shared", port="auto",
                command=self._cmd("{port}", "serve", "root={repo_root}", "wt={worktree}"),
            ),
            LocalServiceSpec(
                id="api", port="auto", depends_on=("secrets",),
                command=self._cmd("{port}", "serve", "root={repo_root}", "wt={worktree}"),
                url="http://127.0.0.1:{port}/?root={repo_root}",
            ),
        ])
        manager.start("sys", "api", session="s1")
        api = self._wait_s(manager, "api", {"running"}, session="s1")
        root = self.root.resolve().as_posix()
        wt = self.worktrees["s1"].resolve().as_posix()
        api_logs = manager.logs("sys", "api", session="s1")
        self.assertTrue(any(f"hello root={root} wt={wt}" in line for line in api_logs), api_logs)
        self.assertEqual(api["url"], f"http://127.0.0.1:{api['port']}/?root={root}")
        secrets_logs = manager.logs("sys", "secrets", session="s1")
        self.assertTrue(any(f"hello root={root} wt={root}" in line for line in secrets_logs), secrets_logs)

    def test_real_checkout_hook_with_no_submodules_starts(self) -> None:
        """The real session_checkout, over a repo with no submodules: nothing to check
        out is not a failure, so a non-default session's service starts."""
        manager = self._session_manager(
            [LocalServiceSpec(id="api", command=self._cmd("{port}", "serve"), port="auto")],
            prepare=None,
        )
        manager.start("sys", "api", session="s1")
        self._wait_s(manager, "api", {"running"}, session="s1")
        checkout = manager.system_status("sys", session="s1")["checkout"]
        self.assertEqual(checkout["state"], "ready")
        self.assertTrue(any("no submodules under 'src/sys'" in n for n in checkout["notes"]), checkout)

    def test_unknown_session(self) -> None:
        manager = self._session_manager([LocalServiceSpec(id="api", command="x")])
        for call in (
            lambda: manager.start("sys", "api", session="nope"),
            lambda: manager.status(session="nope"),
            lambda: manager.logs("sys", "api", session="nope"),
            lambda: manager.status(session="shared"),
        ):
            with self.assertRaises(UnknownSession) as caught:
                call()
            self.assertEqual(caught.exception.status_code, 404)

    def test_generated_values_pass_the_allowlist(self) -> None:
        def lookup(session_id):
            return SessionContext(session_id, "Evil", self.worktrees["s1"]) if session_id == "x;y" else self._lookup(session_id)

        manager = self._session_manager(
            [LocalServiceSpec(id="api", command=self._cmd(0, "idle", "{session}"))], lookup=lookup,
        )
        manager.start("sys", "api", session="x;y")
        status = self._wait_s(manager, "api", {"failed"}, session="x;y")
        self.assertIn("cannot be substituted", status["message"])
        self.assertIsNone(status["pid"])

    def test_checkout_failure_fails_the_start_and_is_retried(self) -> None:
        self.prepare.side_effect = RuntimeError("submodule worktree failed")
        manager = self._session_manager([LocalServiceSpec(id="api", command=self._cmd(0, "idle"))])
        manager.start("sys", "api", session="s1")
        status = self._wait_s(manager, "api", {"failed"}, session="s1")
        self.assertIn("submodule worktree failed", status["message"])
        checkout = manager.system_status("sys", session="s1")["checkout"]
        self.assertEqual(checkout["state"], "failed")
        self.prepare.side_effect = None
        manager.start("sys", "api", session="s1")
        self._wait_s(manager, "api", {"running"}, session="s1")
        self.assertEqual(self.prepare.call_count, 2)

    def test_default_hook_is_session_checkout_imported_lazily(self) -> None:
        fake = types.ModuleType("pm_studio.session_checkout")
        fake.CheckoutError = type("CheckoutError", (Exception,), {})
        fake.ensure_system_checkout = mock.Mock(return_value=["created worktree"])
        manager = self._session_manager(
            [LocalServiceSpec(id="api", command=self._cmd(0, "idle"))], prepare=None, max_envs=3,
        )
        with mock.patch.dict(sys.modules, {"pm_studio.session_checkout": fake}):
            manager.start("sys", "api")  # default session: never called
            self._wait_s(manager, "api", {"running"})
            fake.ensure_system_checkout.assert_not_called()
            manager.start("sys", "api", session="s1")
            self._wait_s(manager, "api", {"running"}, session="s1")
            fake.ensure_system_checkout.assert_called_once_with(
                self.root, self.worktrees["s1"], "src/sys", ("node_modules",)
            )
            # A CheckoutError reads as a failed start, not a crash.
            fake.ensure_system_checkout.side_effect = fake.CheckoutError("dirty worktree")
            manager.start("sys", "api", session="s2")
            status = self._wait_s(manager, "api", {"failed"}, session="s2")
            self.assertIn("dirty worktree", status["message"])


class ResourceTest(SessionEnvTestBase):
    def _api(self, **extra) -> LocalServiceSpec:
        return LocalServiceSpec(
            id="api", command=self._cmd("{port}", "serve", "db={db.name}"), port="auto",
            depends_on=("db",), **extra,
        )

    def test_created_on_first_start_reused_after_and_destroyed(self) -> None:
        envcheck = LocalServiceSpec(
            id="envcheck",
            command=" ".join([_quote(sys.executable), _quote(str(self.env_script)), "DB_NAME"]),
            env=(("DB_NAME", "{db.name}"),),
            depends_on=("db",),
        )
        manager = self._session_manager([self._api(), envcheck], [self._db()])
        self.assertEqual(manager.resource_status("sys", "db", session="s1")["state"], "absent")
        first = manager.start("sys", "api", session="s1")
        self.assertEqual(first["state"], "starting")
        self._wait_r(manager, "db", {"ready"}, session="s1")
        api = self._wait_s(manager, "api", {"running"}, session="s1")
        res = manager.resource_status("sys", "db", session="s1")
        self.assertEqual(res["outputs"], {"name": "db_s1_1"})
        self.assertEqual(res["session"], "s1")
        self.assertTrue(any("hello db=db_s1_1" in line for line in manager.logs("sys", "api", session="s1")))
        # Creation output goes to the same log mechanism, stdout and stderr both.
        creation = manager.logs("sys", "db", session="s1")
        self.assertTrue(any("provisioning for s1" in line for line in creation), creation)
        self.assertTrue(any("[stderr] a warning on stderr" in line for line in creation), creation)
        self.assertTrue(Path(res["log_path"]).is_file())
        # env values get outputs too.
        manager.start("sys", "envcheck", session="s1")
        self._wait_s(manager, "envcheck", {"running"}, session="s1")
        self.assertTrue(any("env DB_NAME=db_s1_1" in line for line in manager.logs("sys", "envcheck", session="s1")))

        # Persisted (outside the repo's tracked state, under the workspace dir).
        saved = json.loads(self.state_path.read_text())
        self.assertEqual(
            [(e["scope"], e["system"], e["resource"], e["outputs"]) for e in saved["resources"]],
            [("s1", "sys", "db", {"name": "db_s1_1"})],
        )

        # Reused: a restart of the service, and a brand-new manager over the same file.
        manager.restart("sys", "api", session="s1")
        self._wait_s(manager, "api", {"running"}, session="s1")
        self.assertEqual(self._created(), 1)
        manager.shutdown()
        self.assertTrue(self.state_path.is_file(), "shutdown must not destroy resources")
        second = self._session_manager([self._api()], [self._db()])
        self.assertEqual(second.resource_status("sys", "db", session="s1")["state"], "ready")
        second.start("sys", "api", session="s1")
        self._wait_s(second, "api", {"running"}, session="s1")
        self.assertEqual(self._created(), 1)
        self.assertTrue(any("hello db=db_s1_1" in line for line in second.logs("sys", "api", session="s1")))
        # Another session gets its own.
        second.start("sys", "api", session="s2")
        self._wait_s(second, "api", {"running"}, session="s2")
        self.assertEqual(second.resource_status("sys", "db", session="s2")["outputs"], {"name": "db_s2_2"})

        # Destroy: refused while in use, then run with the outputs substituted.
        with self.assertRaises(Busy):
            second.destroy_resource("sys", "db", session="s1")
        second.stop("sys", "api", session="s1")
        gone = second.destroy_resource("sys", "db", session="s1", wait=True)
        self.assertEqual(gone["state"], "absent")
        self.assertEqual(self.dropped.read_text().split(), ["db_s1_1"])
        saved = json.loads(self.state_path.read_text())
        self.assertEqual([e["scope"] for e in saved["resources"]], ["s2"])

    def test_create_failures(self) -> None:
        for behaviour, needle in (
            ("badjson", "not JSON"),
            ("fail", "exited with code 2"),
            ("evil", "not allowed"),
        ):
            with self.subTest(behaviour):
                manager = self._session_manager([self._api()], [self._db(behaviour)])
                manager.start("sys", "api", session="s1")
                res = self._wait_r(manager, "db", {"failed"}, session="s1")
                self.assertIn(needle, res["message"])
                self.assertEqual(res["outputs"], {})
                api = self._wait_s(manager, "api", {"failed"}, session="s1")
                self.assertIn("dependency db is failed", api["message"])
                self.assertIsNone(api["pid"])
                self.assertFalse(self.state_path.exists() and json.loads(self.state_path.read_text())["resources"])
                manager.shutdown()
        # A failed resource is retried by the next start.
        before = self._created()
        manager = self._session_manager([self._api()], [self._db("fail")])
        manager.start("sys", "api", session="s1")
        self._wait_r(manager, "db", {"failed"}, session="s1")
        manager.start("sys", "api", session="s1")
        self._wait_r(manager, "db", {"failed"}, session="s1")
        self.assertEqual(self._created(), before + 2)

    def test_repo_root_in_create_and_destroy(self) -> None:
        resource = LocalResourceSpec(
            id="db", create=self._res_cmd() + " {repo_root}", destroy=self._drop_cmd() + " {repo_root}",
        )
        manager = self._session_manager([self._api()], [resource])
        manager.start("sys", "api", session="s1")
        self._wait_s(manager, "api", {"running"}, session="s1")
        root = self.root.resolve().as_posix()
        self.assertTrue(any(f"root {root}" == line for line in manager.logs("sys", "db", session="s1")))
        manager.stop("sys", "api", session="s1")
        manager.destroy_resource("sys", "db", session="s1", wait=True)
        logs = manager.logs("sys", "db", session="s1")
        self.assertTrue(any(line.startswith("dropped db_s1_") for line in logs), logs)
        self.assertTrue(any(f"root {root}" == line for line in logs), logs)

    def test_declared_output_missing_fails(self) -> None:
        manager = self._session_manager([self._api()], [self._db(outputs=("name", "user"))])
        manager.start("sys", "api", session="s1")
        res = self._wait_r(manager, "db", {"failed"}, session="s1")
        self.assertIn("user", res["message"])

    def test_release_session(self) -> None:
        manager = self._session_manager(
            [
                LocalServiceSpec(id="secrets", command=self._cmd("{port}", "serve"), port="auto", scope="shared"),
                LocalServiceSpec(
                    id="api", command=self._cmd("{port}", "serve", "db={db.name} cache={cache.name}"),
                    port="auto", depends_on=("db", "cache", "secrets"),
                ),
            ],
            [
                self._db(),
                LocalResourceSpec(id="cache", create=self._res_cmd()),  # no destroy
            ],
        )
        manager.start("sys", "api", session="s1")
        manager.start("sys", "api", session="s2")
        api = self._wait_s(manager, "api", {"running"}, session="s1")
        self._wait_s(manager, "api", {"running"}, session="s2")
        s1_db = manager.resource_status("sys", "db", session="s1")["outputs"]["name"]

        report = manager.release_session("s1")
        self.assertEqual(report["stopped"], ["sys/api"])
        self.assertEqual(report["destroyed"], ["sys/db"])
        self.assertEqual(report["destroying"], [])
        self.assertEqual([f["resource"] for f in report["forgotten"]], ["sys/cache"])
        self.assertIn("name=db_s1_", report["forgotten"][0]["message"])
        self.assertEqual(self.dropped.read_text().split(), [s1_db])
        self.assertEqual(manager.service_status("sys", "api", session="s1")["state"], "stopped")
        deadline = time.monotonic() + 10
        while _port_open(api["port"]) and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertFalse(_port_open(api["port"]))
        for rid in ("db", "cache"):
            self.assertEqual(manager.resource_status("sys", rid, session="s1")["state"], "absent")
        # Nothing else was touched: the other session and the shared service run on.
        self.assertEqual(manager.service_status("sys", "api", session="s2")["state"], "running")
        self.assertEqual(manager.service_status("sys", "secrets")["state"], "running")
        self.assertEqual(manager.resource_status("sys", "db", session="s2")["state"], "ready")
        self.assertEqual(manager.system_status("sys", session="s1")["checkout"]["state"], "pending")
        saved = json.loads(self.state_path.read_text())
        self.assertEqual(sorted(e["scope"] for e in saved["resources"]), ["s2", "s2"])

    def test_release_in_background_and_failed_destroy_keeps_outputs(self) -> None:
        bad_destroy = LocalResourceSpec(
            id="db", create=self._res_cmd(),
            destroy=f"{_quote(sys.executable)} -c \"import sys; sys.exit(4)\" {{db.name}}",
        )
        manager = self._session_manager([self._api()], [bad_destroy])
        manager.start("sys", "api", session="s1")
        self._wait_s(manager, "api", {"running"}, session="s1")
        report = manager.release_session("s1", wait=False)
        self.assertEqual(report["destroying"], ["sys/db"])
        res = self._wait_r(manager, "db", {"failed"}, session="s1")
        self.assertIn("exited with code 4", res["message"])
        self.assertTrue(res["outputs"]["name"].startswith("db_s1_"))
        # Kept on disk too, so it can be destroyed again after a restart.
        saved = json.loads(self.state_path.read_text())
        self.assertEqual(saved["resources"][0]["state"], "failed")


# ---- per-session environments: routes ----


class SessionRoutesTest(SessionEnvTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.manager = self._session_manager(
            [LocalServiceSpec(id="api", command=self._cmd("{port}", "serve", "db={db.name}"), port="auto", depends_on=("db",))],
            [self._db()],
            max_envs=1,
        )
        for name, value in (("local_env", self.manager), ("audit_log", mock.Mock())):
            patcher = mock.patch.object(server_module, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.request = mock.Mock()

    def _code(self, call) -> int:
        with self.assertRaises(HTTPException) as caught:
            call()
        return caught.exception.status_code

    def test_session_param_on_every_route(self) -> None:
        listing = server_module.local_env_status(self.request, session="s1")
        self.assertEqual(listing["session"], "s1")
        self.assertEqual(listing["systems"][0]["resources"][0]["state"], "absent")
        self.assertEqual(listing["systems"][0]["services"][0]["session"], "s1")

        started = server_module.local_env_start("sys", "api", self.request, None, session="s1")
        self.assertEqual(started["state"], "starting")
        self._wait_s(self.manager, "api", {"running"}, session="s1")
        logs = server_module.local_env_logs("sys", "api", self.request, lines=50, session="s1")
        self.assertTrue(any("hello db=db_s1_1" in line for line in logs["lines"]))
        db_logs = server_module.local_env_logs("sys", "db", self.request, lines=50, session="s1")
        self.assertTrue(any("provisioning for s1" in line for line in db_logs["lines"]))

        overview = server_module.local_env_overview(self.request)
        self.assertEqual(overview["active_session_envs"], 1)
        self.assertEqual(overview["runs"][0]["session_name"], "Chat s1")
        self.assertEqual(overview["resources"][0]["outputs"], {"name": "db_s1_1"})

        # The limit (1) is a 409 naming who holds it.
        with self.assertRaises(HTTPException) as caught:
            server_module.local_env_start("sys", "api", self.request, None, session="s2")
        self.assertEqual(caught.exception.status_code, 409)
        self.assertIn("Chat s1", caught.exception.detail)

        # Destroying a resource in use is a 409; after a stop it goes.
        self.assertEqual(self._code(lambda: server_module.local_env_destroy_resource("sys", "db", self.request, session="s1")), 409)
        server_module.local_env_stop("sys", "api", self.request, session="s1")
        result = server_module.local_env_destroy_resource("sys", "db", self.request, session="s1")
        self.assertIn(result["state"], ("destroying", "absent"))
        self._wait_r(self.manager, "db", {"absent"}, session="s1")

        restarted = server_module.local_env_restart("sys", "api", self.request, None, session="s1")
        self.assertEqual(restarted["session"], "s1")
        stopped = server_module.local_env_stop_all("sys", self.request, session="s1")
        self.assertEqual(stopped["session"], "s1")
        started_all = server_module.local_env_start_all("sys", self.request, None, session="s2")
        self.assertEqual(started_all["session"], "s2")

    def test_unknown_session_and_targets_are_404(self) -> None:
        r = self.request
        for call in (
            lambda: server_module.local_env_status(r, session="nope"),
            lambda: server_module.local_env_start("sys", "api", r, None, session="nope"),
            lambda: server_module.local_env_start_all("sys", r, None, session="nope"),
            lambda: server_module.local_env_stop("sys", "api", r, session="nope"),
            lambda: server_module.local_env_stop_all("sys", r, session="nope"),
            lambda: server_module.local_env_restart("sys", "api", r, None, session="nope"),
            lambda: server_module.local_env_logs("sys", "api", r, lines=5, session="nope"),
            lambda: server_module.local_env_destroy_resource("sys", "db", r, session="nope"),
            lambda: server_module.local_env_destroy_resource("sys", "nope", r, session="s1"),
        ):
            self.assertEqual(self._code(call), 404)

    def test_session_lifecycle_routes_release_first(self) -> None:
        """terminate / delete / cleanup give the session's environment back BEFORE the
        session machinery removes its worktree - and skip it when the call would be
        refused anyway."""
        order = mock.Mock()
        order.local_env.release_session.return_value = {
            "session": "s1", "stopped": ["sys/api"], "destroying": [], "destroyed": [],
            "forgotten": [], "failed": [], "aborted": [],
        }
        session = types.SimpleNamespace(id="s1", is_default=False, status="active")
        order.sessions.get.return_value = session
        for name in ("terminate", "delete", "cleanup"):
            getattr(order.sessions, name).return_value = mock.Mock(to_dict=lambda: {"id": "s1"})
        with mock.patch.object(server_module, "local_env", order.local_env), \
                mock.patch.object(server_module, "sessions", order.sessions):
            server_module.terminate_session("s1", self.request)
            names = [c[0] for c in order.mock_calls if c[0] in ("local_env.release_session", "sessions.terminate")]
            self.assertEqual(names, ["local_env.release_session", "sessions.terminate"])
            order.local_env.release_session.assert_called_with("s1", wait=False)

            order.reset_mock()
            server_module.delete_session("s1", self.request)
            order.local_env.release_session.assert_called_once_with("s1", wait=False)

            order.reset_mock()
            session.status = "merged"
            server_module.cleanup_session("s1", self.request)
            order.local_env.release_session.assert_called_once_with("s1", wait=False)

            # Refused calls release nothing: terminate on a merged session, cleanup on an
            # active one, anything on the default session.
            order.reset_mock()
            order.sessions.terminate.side_effect = ValueError("can't")
            with self.assertRaises(HTTPException):
                server_module.terminate_session("s1", self.request)
            session.status = "active"
            order.sessions.cleanup.side_effect = ValueError("not merged")
            with self.assertRaises(HTTPException):
                server_module.cleanup_session("s1", self.request)
            session.is_default = True
            order.sessions.delete.side_effect = ValueError("default")
            with self.assertRaises(HTTPException):
                server_module.delete_session("s1", self.request)
            order.local_env.release_session.assert_not_called()


# ---- optional resources (`uses`), explicit creation, daily expiry ----


NY = "America/New_York"


def _ny(*parts) -> float:
    return datetime(*parts, tzinfo=ZoneInfo(NY)).timestamp()


class UsesAndExpiryConfigTest(unittest.TestCase):
    setUp = SessionEnvConfigTest.setUp
    tearDown = SessionEnvConfigTest.tearDown
    _write = SessionEnvConfigTest._write
    _fatal = SessionEnvConfigTest._fatal

    BASE = """\
        [systems.s.local]

        [[systems.s.local.resources]]
        id = "db"
        create = "mk"
        {resource_extra}

        [[systems.s.local.services]]
        id = "api"
        command = "run --db [{{db.name}}]"
        {service_extra}
    """

    def _cfg(self, resource_extra: str = "", service_extra: str = 'uses = ["db"]') -> str:
        return self.BASE.format(resource_extra=resource_extra, service_extra=service_extra)

    def test_uses_and_expiry_parse(self) -> None:
        cfg = self._write(self._cfg(
            'expires = "daily"\n        expires_at = "23:30"\n        timezone = "America/New_York"'
        ))
        local = cfg.systems["s"].local
        api, db = local.service("api"), local.resource("db")
        self.assertEqual(api.uses, ("db",))
        self.assertEqual(api.depends_on, ())
        self.assertEqual(local.consumed_resources("api"), ["db"])
        self.assertEqual((db.expires, db.expires_at, db.timezone), ("daily", "23:30", NY))
        defaults = self._write(self._cfg('expires = "daily"')).systems["s"].local.resource("db")
        self.assertEqual((defaults.expires_at, defaults.timezone), ("00:00", ""))

    def test_uses_and_expiry_errors(self) -> None:
        cases = {
            "uses unknown": self._cfg(service_extra='uses = ["nope"]'),
            "uses a service": self._cfg(service_extra='uses = ["api"]'),
            "uses not a list": self._cfg(service_extra='uses = "db"'),
            "uses and depends_on": self._cfg(service_extra='uses = ["db"]\n        depends_on = ["db"]'),
            "shared uses session": self._cfg(service_extra='uses = ["db"]\n        scope = "shared"'),
            "no uses, no dependency": self._cfg(service_extra=""),
            "bad expires": self._cfg('expires = "weekly"'),
            "expires_at 24:00": self._cfg('expires = "daily"\n        expires_at = "24:00"'),
            "expires_at 7:00": self._cfg('expires = "daily"\n        expires_at = "7:00"'),
            "expires_at not a string": self._cfg('expires = "daily"\n        expires_at = 0'),
            "unknown timezone": self._cfg('expires = "daily"\n        timezone = "Mars/Olympus"'),
            "expires_at without expires": self._cfg('expires_at = "00:00"'),
            "timezone without expires": self._cfg(f'timezone = "{NY}"'),
        }
        for name, config in cases.items():
            with self.subTest(name):
                self._fatal(config)


class ExpiryMathTest(unittest.TestCase):
    def test_boundaries_around_midnight(self) -> None:
        late = _ny(2026, 10, 9, 23, 59)
        self.assertEqual(last_expiry_boundary(late, "00:00", NY).isoformat(), "2026-10-09T00:00:00-04:00")
        self.assertEqual(next_expiry_boundary(late, "00:00", NY).isoformat(), "2026-10-10T00:00:00-04:00")
        after = _ny(2026, 10, 10, 0, 0, 30)
        self.assertEqual(last_expiry_boundary(after, "00:00", NY).isoformat(), "2026-10-10T00:00:00-04:00")
        # Exactly at the boundary: it has happened (last), the next one is tomorrow.
        at = _ny(2026, 10, 10, 0, 0)
        self.assertEqual(last_expiry_boundary(at, "00:00", NY).timestamp(), at)
        self.assertEqual(next_expiry_boundary(at, "00:00", NY).isoformat(), "2026-10-11T00:00:00-04:00")
        # A boundary later in the day than now belongs to yesterday.
        self.assertEqual(last_expiry_boundary(late - 3600 * 23, "23:30", NY).isoformat(), "2026-10-08T23:30:00-04:00")

    def test_dst_changes_move_the_instant_not_the_wall_clock(self) -> None:
        spring = _ny(2026, 3, 8, 3, 30)  # clocks went 02:00 EST -> 03:00 EDT
        last = last_expiry_boundary(spring, "00:00", NY)
        nxt = next_expiry_boundary(spring, "00:00", NY)
        self.assertEqual(last.isoformat(), "2026-03-08T00:00:00-05:00")
        self.assertEqual(nxt.isoformat(), "2026-03-09T00:00:00-04:00")
        self.assertEqual(nxt.timestamp() - last.timestamp(), 23 * 3600)
        autumn = _ny(2026, 11, 1, 12, 0)  # clocks went 02:00 EDT -> 01:00 EST
        last = last_expiry_boundary(autumn, "00:00", NY)
        nxt = next_expiry_boundary(autumn, "00:00", NY)
        self.assertEqual(last.isoformat(), "2026-11-01T00:00:00-04:00")
        self.assertEqual(nxt.isoformat(), "2026-11-02T00:00:00-05:00")
        self.assertEqual(nxt.timestamp() - last.timestamp(), 25 * 3600)

    def test_dst_free_zone(self) -> None:
        now = datetime(2026, 1, 15, 8, 0, tzinfo=ZoneInfo("Asia/Tokyo")).timestamp()
        self.assertEqual(last_expiry_boundary(now, "06:30", "Asia/Tokyo").isoformat(), "2026-01-15T06:30:00+09:00")
        self.assertEqual(next_expiry_boundary(now, "06:30", "Asia/Tokyo").isoformat(), "2026-01-16T06:30:00+09:00")
        self.assertEqual(
            next_expiry_boundary(now, "06:30", "Asia/Tokyo").timestamp()
            - last_expiry_boundary(now, "06:30", "Asia/Tokyo").timestamp(),
            24 * 3600,
        )

    def test_local_time_when_no_timezone(self) -> None:
        now = time.time()
        last = last_expiry_boundary(now, "00:00")
        self.assertLessEqual(last.timestamp(), now)
        self.assertLess(now - last.timestamp(), 25 * 3600)
        self.assertEqual((last.hour, last.minute), (0, 0))


class UsesAndExpiryTest(SessionEnvTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.now = [_ny(2026, 10, 9, 22, 0)]

    def _clock(self) -> float:
        return self.now[0]

    def _uses_api(self) -> LocalServiceSpec:
        return LocalServiceSpec(
            id="api", command=self._cmd("{port}", "serve", "db=[{db.name}]"), port="auto", uses=("db",),
        )

    def _expiring_db(self) -> LocalResourceSpec:
        return LocalResourceSpec(
            id="db", create=self._res_cmd(), destroy=self._drop_cmd(), timeout=60.0,
            expires="daily", timezone=NY,
        )

    def _manager_with(self, services, resources) -> LocalEnvManager:
        manager = self._session_manager(services, resources)
        manager._clock = self._clock
        return manager

    def _hello(self, manager, line: str, session: str = "s1") -> bool:
        return any(line in entry for entry in manager.logs("sys", "api", session=session))

    def test_uses_never_creates_and_reads_empty_then_explicit_create(self) -> None:
        manager = self._manager_with([self._uses_api()], [self._db()])
        manager.start("sys", "api", session="s1")
        api = self._wait_s(manager, "api", {"running"}, session="s1")
        self.assertTrue(self._hello(manager, "hello db=[]"), manager.logs("sys", "api", session="s1"))
        self.assertEqual(manager.resource_status("sys", "db", session="s1")["state"], "absent")
        self.assertEqual(self._created(), 0)
        self.assertEqual(api["uses"], ["db"])
        self.assertEqual(api["resources_used"], ["db"])
        self.assertFalse(api["restart_needed"])

        created = manager.create_resource("sys", "db", session="s1")
        self.assertEqual(created["state"], "creating")
        with self.assertRaises(Busy):
            manager.create_resource("sys", "db", session="s1")
        self._wait_r(manager, "db", {"ready"}, session="s1")
        with self.assertRaises(Busy):
            manager.create_resource("sys", "db", session="s1")
        # Running with the old (empty) value: it needs a restart, and says so.
        self.assertTrue(manager.service_status("sys", "api", session="s1")["restart_needed"])
        manager.restart("sys", "api", session="s1")
        self._wait_s(manager, "api", {"running"}, session="s1")
        self.assertTrue(self._hello(manager, "hello db=[db_s1_1]"))
        self.assertFalse(manager.service_status("sys", "api", session="s1")["restart_needed"])
        # Another session still has none, and the default session neither.
        manager.start("sys", "api", session="s2")
        self._wait_s(manager, "api", {"running"}, session="s2")
        self.assertTrue(self._hello(manager, "hello db=[]", session="s2"))

        # Using counts as in use: no destroy under a running service.
        with self.assertRaises(Busy):
            manager.destroy_resource("sys", "db", session="s1")
        manager.stop("sys", "api", session="s1")
        manager.destroy_resource("sys", "db", session="s1", wait=True)
        self.assertEqual(manager.resource_status("sys", "db", session="s1")["state"], "absent")
        # Stopped services never need a restart.
        self.assertFalse(manager.service_status("sys", "api", session="s1")["restart_needed"])

    def test_restart_needed_when_a_used_resource_goes_away(self) -> None:
        manager = self._manager_with([self._uses_api()], [self._db(destroy=False)])
        manager.create_resource("sys", "db", session="s1")
        self._wait_r(manager, "db", {"ready"}, session="s1")
        manager.start("sys", "api", session="s1")
        self._wait_s(manager, "api", {"running"}, session="s1")
        self.assertTrue(self._hello(manager, "hello db=[db_s1_1]"))
        self.assertFalse(manager.service_status("sys", "api", session="s1")["restart_needed"])
        # Forgotten (no destroy declared) while api is stopped, then api runs again with
        # nothing, then the resource comes back: each change flips the hint.
        manager.stop("sys", "api", session="s1")
        manager.destroy_resource("sys", "db", session="s1")
        manager.start("sys", "api", session="s1")
        self._wait_s(manager, "api", {"running"}, session="s1")
        self.assertFalse(manager.service_status("sys", "api", session="s1")["restart_needed"])
        manager.create_resource("sys", "db", session="s1")
        self._wait_r(manager, "db", {"ready"}, session="s1")
        self.assertTrue(manager.service_status("sys", "api", session="s1")["restart_needed"])

    def test_daily_expiry(self) -> None:
        manager = self._manager_with([self._uses_api()], [self._expiring_db()])
        manager.create_resource("sys", "db", session="s1")
        ready = self._wait_r(manager, "db", {"ready"}, session="s1")
        self.assertEqual(ready["created_at"], _ny(2026, 10, 9, 22, 0))
        self.assertEqual(ready["expires"], "daily")
        self.assertEqual(ready["expires_at_iso"], "2026-10-10T00:00:00-04:00")
        manager.start("sys", "api", session="s1")
        self._wait_s(manager, "api", {"running"}, session="s1")
        self.assertTrue(self._hello(manager, "hello db=[db_s1_1]"))

        # Still alive one minute before midnight...
        self.now[0] = _ny(2026, 10, 9, 23, 59)
        self.assertEqual(manager.resource_status("sys", "db", session="s1")["state"], "ready")
        # ...dropped by the nightly job right after it.
        self.now[0] = _ny(2026, 10, 10, 0, 5)
        expired = manager.resource_status("sys", "db", session="s1")
        self.assertEqual(expired["state"], "expired")
        self.assertEqual(
            expired["message"],
            "dropped at 2026-10-10T00:00-04:00 by the nightly job; create it again",
        )
        self.assertEqual(expired["outputs"], {})
        self.assertIsNone(expired["expires_at_iso"])
        self.assertTrue(manager.service_status("sys", "api", session="s1")["restart_needed"])
        self.assertEqual(manager.overview()["resources"], [])
        # The persisted outputs stay until overwritten.
        saved = json.loads(self.state_path.read_text())
        self.assertEqual(saved["resources"][0]["outputs"], {"name": "db_s1_1"})
        # A restart substitutes nothing for it.
        manager.restart("sys", "api", session="s1")
        self._wait_s(manager, "api", {"running"}, session="s1")
        self.assertTrue(self._hello(manager, "hello db=[]"))

        # An expired resource may be created again (no 409)...
        manager.create_resource("sys", "db", session="s1")
        again = self._wait_r(manager, "db", {"ready"}, session="s1")
        self.assertEqual(again["outputs"], {"name": "db_s1_2"})
        self.assertEqual(again["expires_at_iso"], "2026-10-11T00:00:00-04:00")
        saved = json.loads(self.state_path.read_text())
        self.assertEqual(saved["resources"][0]["outputs"], {"name": "db_s1_2"})

        # ...and once expired is forgotten, never destroyed: the job already dropped it.
        self.now[0] = _ny(2026, 10, 11, 1, 0)
        manager.stop("sys", "api", session="s1")
        gone = manager.destroy_resource("sys", "db", session="s1", wait=True)
        self.assertEqual(gone["state"], "absent")
        self.assertFalse(self.dropped.exists(), "destroy must not run for an expired resource")

    def test_expired_dependency_is_recreated_on_next_start(self) -> None:
        web = LocalServiceSpec(
            id="api", command=self._cmd("{port}", "serve", "db=[{db.name}]"), port="auto", depends_on=("db",),
        )
        manager = self._manager_with([web], [self._expiring_db()])
        manager.start("sys", "api", session="s1")
        self._wait_s(manager, "api", {"running"}, session="s1")
        self.assertTrue(self._hello(manager, "hello db=[db_s1_1]"))
        self.now[0] = _ny(2026, 10, 10, 0, 1)
        self.assertTrue(manager.service_status("sys", "api", session="s1")["restart_needed"])
        manager.restart("sys", "api", session="s1")
        self._wait_s(manager, "api", {"running"}, session="s1")
        self.assertEqual(self._created(), 2)
        self.assertTrue(self._hello(manager, "hello db=[db_s1_2]"))
        self.assertEqual(manager.resource_status("sys", "db", session="s1")["state"], "ready")

    def test_release_reports_expired_without_destroying(self) -> None:
        manager = self._manager_with([self._uses_api()], [self._expiring_db()])
        manager.create_resource("sys", "db", session="s1")
        self._wait_r(manager, "db", {"ready"}, session="s1")
        self.now[0] = _ny(2026, 10, 10, 3, 0)
        report = manager.release_session("s1")
        self.assertEqual(report["expired"], ["sys/db"])
        self.assertEqual(report["destroyed"], [])
        self.assertFalse(self.dropped.exists())


class CreateRouteTest(SessionEnvTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.manager = self._session_manager(
            [LocalServiceSpec(id="api", command=self._cmd("{port}", "serve", "db=[{db.name}]"), port="auto", uses=("db",))],
            [self._db()],
        )
        for name, value in (("local_env", self.manager), ("audit_log", mock.Mock())):
            patcher = mock.patch.object(server_module, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.request = mock.Mock()

    def _code(self, call) -> int:
        with self.assertRaises(HTTPException) as caught:
            call()
        return caught.exception.status_code

    def test_create_route(self) -> None:
        r = self.request
        created = server_module.local_env_create_resource("sys", "db", r, session="s1")
        self.assertEqual(created["state"], "creating")
        self.assertEqual(self._code(lambda: server_module.local_env_create_resource("sys", "db", r, session="s1")), 409)
        self._wait_r(self.manager, "db", {"ready"}, session="s1")
        self.assertEqual(self._code(lambda: server_module.local_env_create_resource("sys", "db", r, session="s1")), 409)
        self.assertEqual(self._code(lambda: server_module.local_env_create_resource("sys", "nope", r, session="s1")), 404)
        self.assertEqual(self._code(lambda: server_module.local_env_create_resource("sys", "db", r, session="nope")), 404)
        # The default session's copy is independent.
        self.assertEqual(server_module.local_env_create_resource("sys", "db", r)["state"], "creating")
        self._wait_r(self.manager, "db", {"ready"})


class SubprocessEnvTest(unittest.TestCase):
    def test_agents_are_told_how_to_reach_the_server(self) -> None:
        from pm_studio import agent_env as agent_env_mod
        from pm_studio.accounts import AGENT_HEADER_NAME, AGENT_TOKEN
        from pm_studio.config import CONFIG

        with mock.patch.object(agent_env_mod, "agent_env", return_value=None):
            env = agent_env_mod.subprocess_env()
        self.assertEqual(env["PM_STUDIO_URL"], CONFIG.base_url)
        self.assertEqual(env["PM_STUDIO_AGENT_HEADER"], f"{AGENT_HEADER_NAME}: {AGENT_TOKEN}")
        self.assertIn("PATH", {k.upper() for k in env})

        # With isolation on, the filtered env is the base - and still gets both.
        with mock.patch.object(agent_env_mod, "agent_env", return_value={"ONLY": "this"}):
            env = agent_env_mod.subprocess_env()
        self.assertEqual(env["PM_STUDIO_PYTHON"], sys.executable)
        self.assertEqual(set(env), {"ONLY", "PM_STUDIO_URL", "PM_STUDIO_AGENT_HEADER", "PM_STUDIO_PYTHON"})

    def test_session_id_is_passed_when_given(self) -> None:
        from pm_studio import agent_env as agent_env_mod

        with mock.patch.object(agent_env_mod, "agent_env", return_value={"ONLY": "this"}):
            env = agent_env_mod.subprocess_env("abc123")
            self.assertEqual(env["PM_STUDIO_SESSION"], "abc123")
            self.assertNotIn("PM_STUDIO_SESSION", agent_env_mod.subprocess_env())


class DevPromptTest(unittest.TestCase):
    """A dev task for a system with a local environment is told how to drive it; every
    other dispatch prompt is untouched. Reuses the gitflow suite's fake `claude`."""

    def setUp(self) -> None:
        import threading

        from pm_studio import roadmap as roadmap_module
        from pm_studio import tasks as tasks_module
        from tests.test_gitflow_judge import _FakeClaude, _init_repo

        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        _init_repo(self.repo)
        (self.repo / "GITFLOW.md").write_text("RULES-MARKER")
        (self.repo / "pm_studio_local").mkdir()
        (self.repo / "pm_studio_local" / "config.toml").write_text("")
        self.fake_class = _FakeClaude
        for module, name, value in (
            (tasks_module, "CONFIG", load_config(self.repo)),
            (roadmap_module, "SYSTEMS", {
                "rides": SystemSpec(
                    label="Rides", gitflow="GITFLOW.md",
                    local=LocalEnvSpec(services=(LocalServiceSpec(id="api", command="x"),)),
                ),
                "claims": SystemSpec(label="Claims"),
            }),
        ):
            patcher = mock.patch.object(module, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)
        self.registry = tasks_module.TaskRegistry("sess", self.repo / "ws", self.repo, threading.Lock())

    def _prompt(self, system: str) -> str:
        fake = self.fake_class(self.repo)
        with patch_claude(fake):
            self.registry._execute("Build the thing", system)
        return fake.prompts[0]

    def test_local_env_note_only_for_systems_that_declare_one(self) -> None:
        prompt = self._prompt("rides")
        self.assertIn("$PM_STUDIO_URL/local-env/rides/<service>/start", prompt)
        # Every call targets the agent's OWN session's environment.
        self.assertIn("/local-env/rides/<service>/start?session=$PM_STUDIO_SESSION", prompt)
        self.assertIn('/local-env?session=$PM_STUDIO_SESSION"', prompt)
        self.assertIn("logs?lines=200&session=$PM_STUDIO_SESSION", prompt)
        # The temporary database is opt-in, through the create route.
        self.assertIn("/local-env/rides/resources/<resource>/create?session=$PM_STUDIO_SESSION", prompt)
        self.assertIn("restart_needed", prompt)
        self.assertIn("services: api", prompt)
        # The gitflow rules still come last.
        self.assertLess(prompt.index("PM_STUDIO_URL"), prompt.index("RULES-MARKER"))
        self.assertNotIn("PM_STUDIO_URL", self._prompt("claims"))

    def test_dev_agent_env_carries_its_session(self) -> None:
        fake = self.fake_class(self.repo)
        envs: list[dict] = []

        def capture(cmd, **kwargs):
            if isinstance(cmd, (list, tuple)) and cmd and cmd[0] == "claude":
                envs.append(kwargs.get("env") or {})
            return fake(cmd, **kwargs)

        with patch_claude(capture):
            self.registry._execute("Build the thing", "rides")
        self.assertEqual(envs[0]["PM_STUDIO_SESSION"], "sess")


if __name__ == "__main__":
    unittest.main()
