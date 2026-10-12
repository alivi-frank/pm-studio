"""Tests for `[agents.mcp.<name>]`: the MCP servers PM Studio hands its agents.

What these pin down:

1. **One place, strictly parsed.** config.toml is the only source (every launch passes
   --strict-mcp-config); a malformed table is fatal at startup.
2. **Who gets what.** Dev agents every server, the PM only `pm = true` ones (and
   `mcp__<name>` on its --allowedTools, recomputed per turn), judges none.
3. **No secrets leak.** ${VAR} values live only in the per-launch temp file (mkstemp,
   deleted afterwards); warnings, the summary and the status payload carry variable
   NAMES, commands as written and URL hosts only.
4. **Zero install, OS-adapted.** npx & co. are wrapped as `cmd /c` on Windows; a server
   whose variables are unset is skipped, one whose command is missing is unavailable.
5. **Hot reload.** A changed config.toml is picked up at the next launch; a broken edit
   keeps the last good list and reports the error.
"""

import dataclasses
import io
import json
import os
import sys
import tempfile
import textwrap
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

from tests._claude_stub import patch_claude

from fastapi import HTTPException

from pm_studio import agent as agent_module
from pm_studio import agent_mcp as agent_mcp_module
from pm_studio import judge as judge_module
from pm_studio import server as server_module
from pm_studio.agent_mcp import AgentMcp, adapt_command, expand
from pm_studio.config import AgentConfigError, AgentMcpServer, load_config, parse_agent_mcp
from pm_studio.signals import judge as signals_judge_module

CONFIG_TOML = """\
[agents.mcp.playwright]
command = "npx"
args = ["-y", "@playwright/mcp@latest", "--isolated"]

[agents.mcp.dev-db]
command = "npx"
args = ["-y", "@modelcontextprotocol/server-postgres", "${AGENT_MCP_DEV_DB_URL}"]
pm = true
env = { PGAPPNAME = "${APP_NAME:-pm-studio}" }

[agents.mcp.docs]
type = "http"
url = "https://${DOCS_HOST:-docs.example.com}/mcp"
headers = { Authorization = "Bearer ${DOCS_TOKEN}" }
pm = true
"""

SECRET_URL = "postgres://agent:s3cret-pw@localhost:5432/dev"
SECRET_TOKEN = "tok-very-secret"


def _which(name: str):
    return f"/usr/bin/{name}" if name in ("npx", "python", "cmd") else None


def _registry(tmp: Path, text: str = CONFIG_TOML, environ=None, windows=False, which=_which) -> AgentMcp:
    path = tmp / "config.toml"
    path.write_text(text, encoding="utf-8")
    import tomllib

    servers = parse_agent_mcp(tomllib.loads(text))
    env = {"AGENT_MCP_DEV_DB_URL": SECRET_URL, "DOCS_TOKEN": SECRET_TOKEN} if environ is None else environ
    return AgentMcp(path, servers, environ=env, windows=windows, which=which)


def _bump(path: Path, text: str) -> None:
    """Rewrites the file with a guaranteed newer mtime (coarse filesystem clocks)."""
    before = path.stat().st_mtime_ns
    path.write_text(text, encoding="utf-8")
    os.utime(path, ns=(before + 2_000_000_000, before + 2_000_000_000))


class ParseTest(unittest.TestCase):
    def test_valid_stdio_and_http(self) -> None:
        import tomllib

        servers = parse_agent_mcp(tomllib.loads(CONFIG_TOML))
        self.assertEqual([s.name for s in servers], ["playwright", "dev-db", "docs"])
        playwright, db, docs = servers
        self.assertEqual(playwright, AgentMcpServer(
            name="playwright", transport="stdio", command="npx",
            args=("-y", "@playwright/mcp@latest", "--isolated"),
        ))
        self.assertTrue(db.pm)
        self.assertEqual(db.env, (("PGAPPNAME", "${APP_NAME:-pm-studio}"),))
        self.assertEqual((docs.transport, docs.url), ("http", "https://${DOCS_HOST:-docs.example.com}/mcp"))
        self.assertEqual(docs.headers, (("Authorization", "Bearer ${DOCS_TOKEN}"),))
        sse = parse_agent_mcp({"agents": {"mcp": {"s": {"type": "sse", "url": "http://h/sse"}}}})
        self.assertEqual(sse[0].transport, "sse")
        hash(servers)

    def test_missing_table_is_no_servers(self) -> None:
        self.assertEqual(parse_agent_mcp({}), ())
        self.assertEqual(parse_agent_mcp({"agents": {}}), ())

    def test_errors(self) -> None:
        cases = {
            "unknown agents key": {"agents": {"pm_allowed_tools": []}},
            "mcp not a table": {"agents": {"mcp": ["x"]}},
            "bad name": {"agents": {"mcp": {"dev db": {"command": "x"}}}},
            "server not a table": {"agents": {"mcp": {"x": "npx"}}},
            "unknown server key": {"agents": {"mcp": {"x": {"command": "x", "cwd": "."}}}},
            "both command and url": {"agents": {"mcp": {"x": {"command": "x", "url": "http://h"}}}},
            "neither": {"agents": {"mcp": {"x": {"pm": True}}}},
            "empty command": {"agents": {"mcp": {"x": {"command": ""}}}},
            "args not strings": {"agents": {"mcp": {"x": {"command": "x", "args": [1]}}}},
            "args a string": {"agents": {"mcp": {"x": {"command": "x", "args": "-y"}}}},
            "env not strings": {"agents": {"mcp": {"x": {"command": "x", "env": {"A": 1}}}}},
            "pm not bool": {"agents": {"mcp": {"x": {"command": "x", "pm": "yes"}}}},
            "command with http type": {"agents": {"mcp": {"x": {"command": "x", "type": "http"}}}},
            "url with stdio type": {"agents": {"mcp": {"x": {"url": "http://h", "type": "stdio"}}}},
            "url with args": {"agents": {"mcp": {"x": {"url": "http://h", "args": []}}}},
            "command with headers": {"agents": {"mcp": {"x": {"command": "x", "headers": {}}}}},
            "headers not strings": {"agents": {"mcp": {"x": {"url": "http://h", "headers": {"A": 1}}}}},
        }
        for name, raw in cases.items():
            with self.subTest(name), self.assertRaises(AgentConfigError):
                parse_agent_mcp(raw)

    def test_fatal_at_startup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pm_studio_local").mkdir()
            (root / "pm_studio_local" / "config.toml").write_text('[agents.mcp.x]\nurl = "http://h"\ncommand = "x"\n')
            err = io.StringIO()
            with mock.patch("sys.stderr", err), self.assertRaises(SystemExit):
                load_config(root)
            self.assertIn("both `command` and `url`", err.getvalue())
            (root / "pm_studio_local" / "config.toml").write_text(CONFIG_TOML)
            self.assertEqual(len(load_config(root).agent_mcp), 3)


class ResolutionTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_expand(self) -> None:
        env = {"A": "1", "EMPTY": ""}
        self.assertEqual(expand("x${A}y", env), ("x1y", []))
        self.assertEqual(expand("${B:-dflt}", env), ("dflt", []))
        self.assertEqual(expand("${EMPTY:-dflt}", env), ("dflt", []))
        self.assertEqual(expand("${B:-}", env), ("", []))
        self.assertEqual(expand("${EMPTY}", env), ("", []))
        self.assertEqual(expand("${B}-${C}", env), ("${B}-${C}", ["B", "C"]))
        self.assertEqual(expand("$HOME ${A", env), ("$HOME ${A", []))

    def test_windows_wrapping(self) -> None:
        self.assertEqual(adapt_command("npx", ["-y", "p"], windows=True), ("cmd", ["/c", "npx", "-y", "p"]))
        for launcher in ("npm", "pnpm", "yarn", "bunx"):
            self.assertEqual(adapt_command(launcher, [], windows=True)[0], "cmd")
        self.assertEqual(adapt_command("npx", ["-y"], windows=False), ("npx", ["-y"]))
        self.assertEqual(adapt_command("C:/node/npx.cmd", ["-y"], windows=True), ("C:/node/npx.cmd", ["-y"]))
        self.assertEqual(adapt_command("uvx", ["tool"], windows=True), ("uvx", ["tool"]))

        registry = _registry(self.tmp, windows=True)
        config, _ = registry.launch_config(pm=False)
        self.assertEqual(config["mcpServers"]["playwright"]["command"], "cmd")
        self.assertEqual(config["mcpServers"]["playwright"]["args"][:3], ["/c", "npx", "-y"])

    def test_dev_gets_everything_pm_only_pm_true(self) -> None:
        registry = _registry(self.tmp)
        config, names = registry.launch_config(pm=False)
        self.assertEqual(names, ["playwright", "dev-db", "docs"])
        db = config["mcpServers"]["dev-db"]
        self.assertEqual(db, {
            "type": "stdio", "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-postgres", SECRET_URL],
            "env": {"PGAPPNAME": "pm-studio"},
        })
        self.assertEqual(config["mcpServers"]["docs"], {
            "type": "http", "url": "https://docs.example.com/mcp",
            "headers": {"Authorization": f"Bearer {SECRET_TOKEN}"},
        })
        _, pm_names = registry.launch_config(pm=True)
        self.assertEqual(pm_names, ["dev-db", "docs"])

    def test_missing_variable_skips_with_one_nameonly_warning(self) -> None:
        registry = _registry(self.tmp, environ={"DOCS_TOKEN": SECRET_TOKEN})
        err = io.StringIO()
        with mock.patch("sys.stderr", err):
            _, names = registry.launch_config(pm=False)
            registry.launch_config(pm=False)
            registry.launch_config(pm=True)
        self.assertEqual(names, ["playwright", "docs"])
        log = err.getvalue()
        self.assertEqual(log.count("dev-db"), 1, log)
        self.assertIn("AGENT_MCP_DEV_DB_URL", log)
        self.assertNotIn(SECRET_TOKEN, log)

    def test_unavailable_command_is_left_out(self) -> None:
        registry = _registry(self.tmp, which=lambda name: None)
        with mock.patch("sys.stderr", io.StringIO()):
            _, names = registry.launch_config(pm=False)
        self.assertEqual(names, ["docs"])  # url servers need no local command

    def test_temp_file_content_permissions_and_cleanup(self) -> None:
        registry = _registry(self.tmp)
        with registry.launch_args(pm=False) as (args, names):
            self.assertEqual(args[0], "--mcp-config")
            self.assertEqual(args[2], "--strict-mcp-config")
            path = Path(args[1])
            self.assertTrue(path.is_file())
            # Outside the repo (the system temp dir).
            self.assertEqual(path.parent.resolve(), Path(tempfile.gettempdir()).resolve())
            if os.name != "nt":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(path.read_text())["mcpServers"]["dev-db"]["args"][-1], SECRET_URL)
            self.assertEqual(names, ["playwright", "dev-db", "docs"])
        self.assertFalse(path.exists())
        # Deleted even when the launch blows up.
        with self.assertRaises(RuntimeError):
            with registry.launch_args(pm=True) as (args, _):
                path = Path(args[1])
                raise RuntimeError("boom")
        self.assertFalse(path.exists())

    def test_no_servers_is_strict_only(self) -> None:
        registry = _registry(self.tmp, text="")
        with registry.launch_args(pm=False) as (args, names):
            self.assertEqual((args, names), (["--strict-mcp-config"], []))

    def test_status_payload_has_no_secrets(self) -> None:
        registry = _registry(self.tmp, environ={"DOCS_TOKEN": SECRET_TOKEN, "DOCS_HOST": "mcp.internal:8443"},
                             which=lambda n: None if n == "npx" else f"/bin/{n}")
        status = registry.status()
        self.assertEqual(status["source"], "pm_studio_local/config.toml")
        self.assertIsNone(status["error"])
        self.assertEqual(status["node"], {"npx": False, "path": None})
        by_name = {s["name"]: s for s in status["servers"]}
        self.assertEqual(by_name["playwright"]["status"], "unavailable")
        self.assertIn("install Node.js", by_name["playwright"]["message"])
        self.assertEqual(by_name["playwright"]["command"], "npx")
        self.assertEqual(by_name["dev-db"]["status"], "skipped")
        self.assertEqual(by_name["dev-db"]["missing_env"], ["AGENT_MCP_DEV_DB_URL"])
        self.assertEqual(by_name["dev-db"]["message"], "set AGENT_MCP_DEV_DB_URL in .env")
        self.assertEqual(by_name["docs"]["status"], "ready")
        self.assertEqual(by_name["docs"]["url_host"], "mcp.internal")
        self.assertIsNone(by_name["docs"]["command"])
        self.assertTrue(by_name["docs"]["pm"])
        dumped = json.dumps(status)
        for secret in (SECRET_TOKEN, "Bearer", "@playwright/mcp", "server-postgres", "/mcp"):
            self.assertNotIn(secret, dumped)

        ready = _registry(self.tmp).status()
        self.assertEqual({s["status"] for s in ready["servers"]}, {"ready"})
        self.assertEqual(ready["node"]["path"], "/usr/bin/npx")
        self.assertNotIn(SECRET_URL, json.dumps(ready))
        self.assertNotIn("s3cret", _registry(self.tmp).summary())

    def test_summary_line(self) -> None:
        registry = _registry(self.tmp, environ={"DOCS_TOKEN": "x"})
        self.assertEqual(
            registry.summary(),
            "agent MCP: 2 servers (playwright, docs), skipped: dev-db (AGENT_MCP_DEV_DB_URL)",
        )

    def test_hot_reload_and_keep_last_good(self) -> None:
        registry = _registry(self.tmp)
        path = self.tmp / "config.toml"
        loaded = registry.status()["loaded_at"]
        with mock.patch("sys.stderr", io.StringIO()):
            # An edit is picked up at the next use, no restart.
            _bump(path, CONFIG_TOML + '\n[agents.mcp.extra]\ncommand = "python"\npm = true\n')
            self.assertEqual([s.name for s in registry.servers()][-1], "extra")
            self.assertGreaterEqual(registry.status()["loaded_at"], loaded)
            # A broken edit keeps the last good list and says why.
            _bump(path, '[agents.mcp.bad]\ncommand = "x"\nurl = "http://h"\n')
            self.assertEqual(len(registry.servers()), 4)
            self.assertIn("both `command` and `url`", registry.status()["error"])
            _bump(path, "[agents.mcp\n")  # not even TOML
            self.assertEqual(len(registry.servers()), 4)
            self.assertIsNotNone(registry.status()["error"])
            # Fixed: back in force, error cleared.
            _bump(path, '[agents.mcp.only]\ncommand = "python"\n')
            self.assertEqual([s.name for s in registry.servers()], ["only"])
            self.assertIsNone(registry.status()["error"])
            # An unchanged file is not re-read; force re-reads anyway.
            with mock.patch.object(agent_mcp_module, "parse_agent_mcp", side_effect=AssertionError):
                registry.servers()
            with mock.patch.object(agent_mcp_module, "parse_agent_mcp", return_value=()) as parse:
                registry.refresh(force=True)
                parse.assert_called_once()
            self.assertEqual(registry.servers(), ())

    def test_check(self) -> None:
        def which(name):
            return sys.executable if name == "python" else None

        text = textwrap.dedent(f"""\
            [agents.mcp.alive]
            command = "python"
            args = ["-c", "import sys; sys.stdin.read()"]

            [agents.mcp.dies]
            command = "python"
            args = ["-c", "import sys; sys.exit(3)"]

            [agents.mcp.remote]
            url = "http://localhost:1/mcp"
        """)
        registry = _registry(self.tmp, text=text, which=which)
        # The real interpreter, whatever the bare name resolves to on this machine.
        registry._resolve_orig = registry._resolve

        def resolve(spec, extra_env=None):
            entry, missing = registry._resolve_orig(spec, extra_env)
            if entry.get("command") == "python":
                entry["command"] = sys.executable
            return entry, missing

        registry._resolve = resolve
        self.assertTrue(registry.check("alive", alive_seconds=1.0)["ok"])
        dies = registry.check("dies", alive_seconds=5.0)
        self.assertFalse(dies["ok"])
        self.assertIn("exited with code 3", dies["message"])
        self.assertIsNone(registry.check("remote")["ok"])
        with self.assertRaises(KeyError):
            registry.check("nope")


class LaunchCommandTest(unittest.TestCase):
    """The flags each kind of agent is actually launched with."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.registry = _registry(self.tmp)
        patcher = mock.patch.object(agent_mcp_module, "AGENT_MCP", self.registry)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)

    @staticmethod
    def _flag(command: list, name: str):
        return command[command.index(name) + 1] if name in command else None

    def _capture(self, reply: dict, fallthrough=None):
        calls = []

        def run(cmd, **kwargs):
            if isinstance(cmd, (list, tuple)) and cmd and cmd[0] == "claude":
                config_path = self._flag(list(cmd), "--mcp-config")
                calls.append({
                    "cmd": list(cmd),
                    "mcp": json.loads(Path(config_path).read_text()) if config_path else None,
                    "config_path": config_path,
                    "system_prompt": Path(self._flag(list(cmd), "--system-prompt-file")).read_text(encoding="utf-8")
                    if "--system-prompt-file" in cmd else "",
                })
                return types.SimpleNamespace(returncode=0, stdout=json.dumps(reply), stderr="")
            return fallthrough(cmd, **kwargs)

        return calls, run

    def test_dev_task_gets_every_server(self) -> None:
        from pm_studio import roadmap as roadmap_module
        from pm_studio import tasks as tasks_module
        from pm_studio.config import SystemSpec
        from tests.test_gitflow_judge import _FakeClaude, _init_repo

        repo = self.tmp / "repo"
        repo.mkdir()
        _init_repo(repo)
        (repo / "pm_studio_local").mkdir()
        (repo / "pm_studio_local" / "config.toml").write_text("")
        fake = _FakeClaude(repo)
        calls, run = self._capture({"result": "done", "is_error": False}, fallthrough=fake)
        registry = tasks_module.TaskRegistry("sess", repo / "ws", repo, threading.Lock())
        with mock.patch.object(tasks_module, "CONFIG", load_config(repo)), \
                mock.patch.object(roadmap_module, "SYSTEMS", {"claims": SystemSpec(label="Claims")}), \
                patch_claude(run):
            registry._execute("Build the thing", "claims")
        cmd = calls[0]["cmd"]
        self.assertIn("--strict-mcp-config", cmd)
        self.assertEqual(sorted(calls[0]["mcp"]["mcpServers"]), ["dev-db", "docs", "playwright"])
        self.assertFalse(Path(calls[0]["config_path"]).exists(), "temp config must be deleted")
        self.assertNotIn("--allowedTools", cmd)  # bypassPermissions, as before

    def _pm(self, mode: str = "build"):
        session = dataclasses.make_dataclass(
            "StubSession",
            ["id", "product", "initiative_id", "adopted_products", "model", "mode", "worktree_path"],
        )(
            id="s1", product=None, initiative_id=None, adopted_products=[],
            model="claude-opus-5", mode=mode, worktree_path=str(self.tmp),
        )
        return agent_module.PMAgent(session, threading.Lock())

    def test_pm_gets_pm_servers_and_allowlist_recomputed_per_turn(self) -> None:
        for mode in ("build", "research"):
            with self.subTest(mode=mode):
                pm = self._pm(mode)
                base = pm.allowed_tools
                reply = {"type": "result", "result": "hi", "session_id": "c1"}
                calls, run = self._capture(reply)

                def stream(command, text, **kwargs):  # the PM turn's streaming runner
                    run(command)
                    return 0, reply, "", ""

                with mock.patch.object(agent_module, "_stream_claude", stream),                         mock.patch("sys.stderr", io.StringIO()):
                    pm._run_pm_turn("hello")
                    turn = calls[-1]
                    self.assertEqual(self._flag(turn["cmd"], "--allowedTools"), base + " mcp__dev-db mcp__docs")
                    self.assertEqual(sorted(turn["mcp"]["mcpServers"]), ["dev-db", "docs"])
                    self.assertIn("--strict-mcp-config", turn["cmd"])
                    self.assertIn("mcp__dev-db, mcp__docs", turn["system_prompt"])
                    self.assertEqual(pm.allowed_tools, base, "the PM's own allowlist is never changed")

                    # A server added to config.toml reaches the SAME PM on its next turn.
                    _bump(self.tmp / "config.toml", CONFIG_TOML + '\n[agents.mcp.extra]\ncommand = "python"\npm = true\n')
                    self.registry._which = lambda n: f"/bin/{n}"
                    pm._run_pm_turn("again")
                    self.assertEqual(
                        self._flag(calls[-1]["cmd"], "--allowedTools"),
                        base + " mcp__dev-db mcp__docs mcp__extra",
                    )
                    self.assertIn("--resume", calls[-1]["cmd"])
                    # And without any pm servers: no extras, no prompt note, still strict.
                    _bump(self.tmp / "config.toml", '[agents.mcp.devonly]\ncommand = "python"\n')
                    pm._run_pm_turn("once more")
                    self.assertEqual(self._flag(calls[-1]["cmd"], "--allowedTools"), base)
                    self.assertNotIn("--mcp-config", calls[-1]["cmd"])
                    self.assertIn("--strict-mcp-config", calls[-1]["cmd"])
                    self.assertNotIn("MCP servers", calls[-1]["system_prompt"])
                _bump(self.tmp / "config.toml", CONFIG_TOML)
                self.registry._which = _which

    def test_judges_get_no_servers(self) -> None:
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = list(cmd)
            result = json.dumps({"verdict": "pass", "violations": [], "summary": "ok"})
            return types.SimpleNamespace(returncode=0, stdout=json.dumps({"result": result, "is_error": False}), stderr="")

        with mock.patch.object(judge_module.subprocess, "run", fake_run):
            judge_module.run_judge(
                repo_root=self.tmp, system_id="epic", system_label="EpicRide",
                gitflow_path="GITFLOW.md", rules="rules", description="task",
                head_before="aaa", head_after="bbb", fallback_model="m",
            )
        self.assertIn("--strict-mcp-config", captured["cmd"])
        self.assertNotIn("--mcp-config", captured["cmd"])

        seen = []

        def runner(cmd):
            seen.append(list(cmd))
            return types.SimpleNamespace(returncode=0, stdout=json.dumps({"result": "{}", "is_error": False}), stderr="")

        with contextlib_suppress():
            signals_judge_module.run_judge(
                {}, dossier_path=self.tmp / "d.json", repo_root=self.tmp, model="m", runner=runner,
            )
        self.assertTrue(seen)
        self.assertIn("--strict-mcp-config", seen[0])
        self.assertNotIn("--mcp-config", seen[0])


def contextlib_suppress():
    import contextlib

    return contextlib.suppress(Exception)


class RoutesTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.registry = _registry(self.tmp, environ={"DOCS_TOKEN": SECRET_TOKEN})
        for module, name, value in (
            (agent_mcp_module, "AGENT_MCP", self.registry),
            (server_module, "audit_log", mock.Mock()),
        ):
            patcher = mock.patch.object(module, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)
        self.request = mock.Mock()

    def test_status_and_reload(self) -> None:
        status = server_module.agents_mcp_status(self.request)
        self.assertEqual([s["status"] for s in status["servers"]], ["ready", "skipped", "ready"])
        self.assertNotIn(SECRET_TOKEN, json.dumps(status))
        # Reload forces a re-read even with an unchanged mtime.
        (self.tmp / "config.toml").write_text('[agents.mcp.only]\ncommand = "npx"\n', encoding="utf-8")
        os.utime(self.tmp / "config.toml", ns=(self.registry._mtime, self.registry._mtime))
        with mock.patch("sys.stderr", io.StringIO()):
            reloaded = server_module.agents_mcp_reload(self.request)
        self.assertEqual([s["name"] for s in reloaded["servers"]], ["only"])

    def test_check_unknown_is_404(self) -> None:
        with self.assertRaises(HTTPException) as caught:
            server_module.agents_mcp_check("nope", self.request)
        self.assertEqual(caught.exception.status_code, 404)

    def test_reload_requires_manage_local_env(self) -> None:
        from pm_studio.accounts import User

        enterprise = dataclasses.replace(server_module.CONFIG, mode="enterprise")
        viewer = mock.Mock()
        viewer.state.user = User(
            id="v", email="v@x", name="V", role="viewer", status="active", created_at=0.0, updated_at=0.0,
        )
        with mock.patch.object(server_module, "CONFIG", enterprise):
            with self.assertRaises(HTTPException) as caught:
                server_module.agents_mcp_reload(viewer)
            self.assertEqual(caught.exception.status_code, 403)
            self.assertIn("servers", server_module.agents_mcp_status(viewer))


if __name__ == "__main__":
    unittest.main()
