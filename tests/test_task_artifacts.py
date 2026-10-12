"""Quick tasks, task artifacts and on-demand code for the PM.

The failure these exist to prevent: a stakeholder asks the PM to "show me the login
page", and the PM - which can only plan and dispatch - answers that it cannot take
screenshots, or reads a session worktree whose submodules are still empty folders and
reports the code as missing. The fixes are structural:

- a QUICK task (look, don't build) is one curl away, and the PM's prompt says so;
- every dev task gets a folder for files the stakeholder should see, shown on its card;
- a dev task checks its system's code out before it runs, and the PM can do the same
  for its own worktree before reading code.
"""

import json
import subprocess
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

from tests._claude_stub import patch_claude

from pm_studio import agent as agent_module
from pm_studio import agent_mcp as agent_mcp_module
from pm_studio import roadmap as roadmap_module
from pm_studio import tasks as tasks_module
from pm_studio.config import AgentMcpServer, SystemSpec, load_config
from pm_studio.sessions import Session
from pm_studio.tasks import TaskRegistry, list_artifacts


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def _init_repo(repo: Path) -> None:
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    (repo / "README.md").write_text("seed\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "seed")


class _FakeClaude:
    """Stands in for the dev agent's `claude` process; every other command runs for
    real. `effect(env)` runs inside the fake turn, with the env the agent got."""

    def __init__(self, effect=None):
        self.effect = effect
        self.calls: list[dict] = []
        self._real_run = subprocess.run

    def __call__(self, cmd, **kwargs):
        if isinstance(cmd, (list, tuple)) and cmd and cmd[0] == "claude":
            self.calls.append({"cmd": list(cmd), **kwargs})
            if self.effect:
                self.effect(kwargs["env"])
            return types.SimpleNamespace(
                returncode=0, stdout=json.dumps({"result": "looked", "is_error": False}), stderr=""
            )
        return self._real_run(cmd, **kwargs)


class _RegistryCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        _init_repo(self.repo)
        (self.repo / "pm_studio_local").mkdir()
        (self.repo / "pm_studio_local" / "config.toml").write_text("")
        self._orig_config = tasks_module.CONFIG
        tasks_module.CONFIG = load_config(self.repo)
        self._orig_systems = roadmap_module.SYSTEMS
        roadmap_module.SYSTEMS = {"web": SystemSpec(label="Web App")}
        self.registry = TaskRegistry("sess", self.repo / "ws", self.repo, threading.Lock())

    def tearDown(self) -> None:
        tasks_module.CONFIG = self._orig_config
        roadmap_module.SYSTEMS = self._orig_systems
        self._tmp.cleanup()

    def _run(self, task_id: str, quick: bool, effect=None) -> tuple[dict, _FakeClaude]:
        fake = _FakeClaude(effect)
        with patch_claude(fake):
            self.registry._run_and_record(task_id, "Look at it", 1.0, "web", "", None, quick)
        return self.registry.get_task(task_id), fake


class ArtifactsTest(_RegistryCase):
    def test_files_the_agent_leaves_are_listed_on_the_record(self) -> None:
        def effect(env):
            folder = Path(env[tasks_module.ARTIFACTS_ENV])
            (folder / "login-page.png").write_bytes(b"\x89PNG")
            (folder / "logs").mkdir()
            (folder / "logs" / "api.txt").write_text("ok")

        task, _ = self._run("t1", quick=False, effect=effect)
        self.assertEqual(
            task["artifacts"],
            [{"name": "login-page.png", "size": 4}, {"name": "logs/api.txt", "size": 2}],
        )

    def test_the_folder_is_named_in_the_prompt_and_the_environment(self) -> None:
        _, fake = self._run("t2", quick=False)
        (call,) = fake.calls
        folder = str(self.registry.artifacts_dir("t2"))
        self.assertEqual(call["env"][tasks_module.ARTIFACTS_ENV], folder)
        self.assertIn(folder, call["input"])
        self.assertTrue(Path(folder).is_dir())

    def test_a_task_that_leaves_nothing_has_no_artifacts_key(self) -> None:
        task, _ = self._run("t3", quick=False)
        self.assertNotIn("artifacts", task)

    def test_the_listing_is_bounded(self) -> None:
        folder = Path(self._tmp.name) / "many"
        folder.mkdir()
        for i in range(tasks_module.MAX_ARTIFACTS + 5):
            (folder / f"f{i:03}.txt").write_text("x")
        self.assertEqual(len(list_artifacts(folder)), tasks_module.MAX_ARTIFACTS)
        self.assertEqual(list_artifacts(folder / "missing"), [])

    def test_artifact_path_stays_inside_the_task_folder(self) -> None:
        def effect(env):
            (Path(env[tasks_module.ARTIFACTS_ENV]) / "shot.png").write_bytes(b"x")

        self._run("t4", quick=False, effect=effect)
        (self.registry.tasks_dir / "secret.txt").write_text("no")
        self.assertIsNotNone(self.registry.artifact_path("t4", "shot.png"))
        self.assertIsNone(self.registry.artifact_path("t4", "../secret.txt"))
        self.assertIsNone(self.registry.artifact_path("t4", "../t4.json"))
        self.assertIsNone(self.registry.artifact_path("t4", str(self.registry.tasks_dir / "secret.txt")))
        self.assertIsNone(self.registry.artifact_path("t4", "missing.png"))
        self.assertIsNone(self.registry.artifact_path("nope", "shot.png"))

    def test_merge_resolution_gets_no_artifacts_folder(self) -> None:
        fake = _FakeClaude()
        with patch_claude(fake):
            self.registry.run_conflict_resolution("fix the merge")
        self.assertNotIn(tasks_module.ARTIFACTS_ENV, fake.calls[0]["env"])


class QuickTaskTest(_RegistryCase):
    def test_quick_task_carries_the_preamble_and_the_short_timeout(self) -> None:
        task, fake = self._run("q1", quick=True)
        (call,) = fake.calls
        self.assertTrue(call["input"].startswith(tasks_module.QUICK_TASK_PREAMBLE))
        self.assertEqual(call["timeout"], tasks_module.QUICK_TASK_TIMEOUT_SECONDS)
        self.assertTrue(task["quick"])

    def test_ordinary_task_is_unchanged(self) -> None:
        task, fake = self._run("q2", quick=False)
        (call,) = fake.calls
        self.assertTrue(call["input"].startswith("Look at it"))
        self.assertEqual(call["timeout"], tasks_module.DEV_AGENT_TIMEOUT_SECONDS)
        self.assertFalse(task["quick"])

    def _model_of(self, call: dict) -> str:
        cmd = call["cmd"]
        return cmd[cmd.index("--model") + 1]

    def test_quick_task_runs_on_the_quick_model_when_one_is_configured(self) -> None:
        self.registry.model = "big-model"
        with mock.patch.object(tasks_module.models_module, "QUICK_MODEL", "small-model"):
            _, quick = self._run("m1", quick=True)
            _, build = self._run("m2", quick=False)
        self.assertEqual(self._model_of(quick.calls[0]), "small-model")
        self.assertEqual(self._model_of(build.calls[0]), "big-model")

    def test_without_a_quick_model_the_session_model_is_used(self) -> None:
        self.registry.model = "big-model"
        with mock.patch.object(tasks_module.models_module, "QUICK_MODEL", None):
            _, quick = self._run("m3", quick=True)
        self.assertEqual(self._model_of(quick.calls[0]), "big-model")

    def test_prompt_asks_for_absolute_artifact_paths(self) -> None:
        _, fake = self._run("p1", quick=True)
        self.assertIn("ABSOLUTE path inside that folder", fake.calls[0]["input"])

    def test_start_task_records_quick_and_runs_in_the_background(self) -> None:
        with mock.patch.object(self.registry, "_run_and_record") as run:
            task = self.registry.start_task("Look", "web", quick=True)
        self.assertTrue(task["quick"])
        self.assertEqual(task["status"], "running")


class CheckoutBeforeTaskTest(_RegistryCase):
    def test_the_system_code_is_checked_out_before_the_agent_runs(self) -> None:
        order: list[str] = []
        with mock.patch.object(tasks_module, "ensure_system_code",
                               side_effect=lambda wt, s: order.append(f"checkout:{s}") or []):
            self._run("c1", quick=False, effect=lambda env: order.append("agent"))
        self.assertEqual(order, ["checkout:web", "agent"])

    def test_a_failed_checkout_is_reported_not_fatal(self) -> None:
        with mock.patch.object(tasks_module, "ensure_system_code",
                               side_effect=RuntimeError("no primary clone")):
            task, fake = self._run("c2", quick=False)
        self.assertEqual(task["checkout_error"], "no primary clone")
        self.assertIn("could not check this system's code out", fake.calls[0]["input"])

    def test_ensure_system_code_passes_path_and_links(self) -> None:
        from pm_studio.config import LocalEnvSpec

        roadmap_module.SYSTEMS = {
            "web": SystemSpec(label="Web", path="src/web",
                              local=LocalEnvSpec(link_from_primary=("src/web/*/node_modules",)))
        }
        with mock.patch.object(tasks_module.session_checkout, "ensure_system_checkout",
                               return_value=["ok"]) as ensure:
            notes = tasks_module.ensure_system_code(Path("/wt"), "web")
        self.assertEqual(notes, ["ok"])
        ensure.assert_called_once_with(
            tasks_module.CONFIG.repo_root, Path("/wt"), "src/web", ("src/web/*/node_modules",)
        )

    def test_ensure_system_code_ignores_systems_without_a_path(self) -> None:
        with mock.patch.object(tasks_module.session_checkout, "ensure_system_checkout") as ensure:
            self.assertEqual(tasks_module.ensure_system_code(Path("/wt"), "web"), [])
            self.assertEqual(tasks_module.ensure_system_code(Path("/wt"), "unknown"), [])
        ensure.assert_not_called()


class QuickModelConfigTest(unittest.TestCase):
    """[models] quick - a reserved key next to `default`, never a model of its own."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "pm_studio_local").mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _load(self, models: str):
        (self.root / "pm_studio_local" / "config.toml").write_text(f"[models]\n{models}")
        return load_config(self.root)

    def test_quick_names_one_of_the_declared_models(self) -> None:
        cfg = self._load('default = "big"\nquick = "small"\nbig = "Big"\nsmall = "Small"\n')
        self.assertEqual(cfg.quick_model, "small")
        self.assertEqual(cfg.models, {"big": "Big", "small": "Small"})

    def test_quick_is_optional(self) -> None:
        self.assertIsNone(self._load('big = "Big"\n').quick_model)

    def test_an_undeclared_quick_model_is_fatal(self) -> None:
        with self.assertRaises(SystemExit):
            self._load('quick = "nope"\nbig = "Big"\n')


class McpExtraEnvTest(unittest.TestCase):
    def test_per_launch_variables_resolve_in_server_args(self) -> None:
        spec = AgentMcpServer(
            name="browser", transport="stdio", command="npx",
            args=("pw-mcp", "--output-dir", "${PM_STUDIO_ARTIFACTS_DIR:-.out}"),
        )
        mcp = agent_mcp_module.AgentMcp(
            Path("missing.toml"), (spec,), environ={}, windows=False, which=lambda c: c
        )
        config, _ = mcp.launch_config(pm=False, extra_env={"PM_STUDIO_ARTIFACTS_DIR": "/a/t1"})
        self.assertEqual(config["mcpServers"]["browser"]["args"][-1], "/a/t1")
        config, _ = mcp.launch_config(pm=False)
        self.assertEqual(config["mcpServers"]["browser"]["args"][-1], ".out")


class PmPromptTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.worktree = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _agent(self, worktree: Path, systems: dict) -> agent_module.PMAgent:
        session = Session(
            id="s1", name="t", branch="session/s1", worktree_path=str(worktree),
            base_branch="main", created_at=0.0, status="active", is_default=False,
        )
        with mock.patch.object(agent_module, "SYSTEMS", systems):
            return agent_module.PMAgent(session, threading.Lock())

    def test_build_prompt_offers_quick_tasks(self) -> None:
        agent = self._agent(self.worktree, {"web": SystemSpec(label="Web", path="src/web")})
        self.assertIn('"quick": true', agent.system_prompt)
        self.assertIn("instead of saying it can't be done", agent.system_prompt)

    def test_session_worktree_gets_the_checkout_curl_and_its_allowlist_entry(self) -> None:
        agent = self._agent(self.worktree, {"web": SystemSpec(label="Web", path="src/web")})
        self.assertIn(f"{agent.checkout_url}/<system_id>", agent.system_prompt)
        self.assertIn("EMPTY folders", agent.system_prompt)
        # Research reads the primary checkout; a worktree checkout is only for its own branch.
        self.assertIn(f"PRIMARY checkout, {agent_module.CONFIG.repo_root.resolve()}, READ-ONLY", agent.system_prompt)
        self.assertIn(f"Bash(curl -s -X POST {agent.checkout_url}/*)", agent.allowed_tools)
        self.assertTrue(agent.checkout_url.endswith("/sessions/s1/checkout"))

    def test_primary_checkout_gets_no_checkout_curl(self) -> None:
        agent = self._agent(agent_module.CONFIG.repo_root, {"web": SystemSpec(label="Web", path="src/web")})
        self.assertIsNone(agent.checkout_url)
        self.assertNotIn("/checkout", agent.allowed_tools)
        self.assertNotIn("EMPTY folders", agent.system_prompt)

    def test_no_system_with_code_means_no_checkout_curl(self) -> None:
        agent = self._agent(self.worktree, {"web": SystemSpec(label="Web")})
        self.assertNotIn("/checkout", agent.allowed_tools)

    def test_completion_note_names_the_artifacts_by_path(self) -> None:
        agent = self._agent(self.worktree, {})
        note = agent._artifacts_completion_note(
            {"id": "t9", "artifacts": [{"name": "login.png", "size": 1}]}
        )
        self.assertIn(str(agent.workspace_dir / "tasks" / "t9" / "login.png"), note)
        self.assertEqual(agent._artifacts_completion_note({"id": "t9"}), "")


if __name__ == "__main__":
    unittest.main()


class LiveProgressTest(_RegistryCase):
    """A running task's steps reach its record (and so its card) while it runs."""

    def _assistant(self, *blocks):
        return {"type": "assistant", "message": {"content": list(blocks)}}

    def test_steps_are_written_while_running_and_kept_at_the_end(self) -> None:
        seen_while_running: list[list[str]] = []
        registry = self.registry
        registry._write_task({"id": "s1", "status": "running", "started_at": 1.0, "description": "d"})

        def run_agent(command, text, *, cwd, env, timeout, on_message, on_start=None):
            on_message(self._assistant({"type": "text", "text": "Starting the API."}))
            on_message(self._assistant({"type": "tool_use", "name": "Bash", "input": {
                "command": 'curl -s -X POST "$PM_STUDIO_URL/local-env/epicride/api/start?session=s" -H "$H"'}}))
            seen_while_running.append([s["text"] for s in registry.get_task("s1")["progress"]])
            on_message({"type": "user", "message": {"content": []}})  # tool results: no step
            return 0, {"type": "result", "result": "done"}, "", ""

        with mock.patch.object(tasks_module, "run_agent", run_agent):
            registry._run_and_record("s1", "d", 1.0, "web", "", None, False)
        self.assertEqual(seen_while_running, [["Starting the API.", "Starting epicride/api"]])
        final = registry.get_task("s1")
        self.assertEqual(final["status"], "done")
        self.assertEqual([s["kind"] for s in final["progress"]], ["text", "tool"])

    def test_the_step_list_is_bounded(self) -> None:
        def run_agent(command, text, *, cwd, env, timeout, on_message, on_start=None):
            for i in range(tasks_module.MAX_PROGRESS_STEPS + 10):
                on_message(self._assistant({"type": "text", "text": f"step {i} " + "x" * 500}))
            return 0, {"type": "result", "result": "done"}, "", ""

        with mock.patch.object(tasks_module, "run_agent", run_agent):
            self.registry._run_and_record("s2", "d", 1.0, "web", "", None, False)
        steps = self.registry.get_task("s2")["progress"]
        self.assertEqual(len(steps), tasks_module.MAX_PROGRESS_STEPS)
        self.assertTrue(steps[-1]["text"].startswith(f"step {tasks_module.MAX_PROGRESS_STEPS + 9} "))
        self.assertLessEqual(max(len(s["text"]) for s in steps), tasks_module.MAX_STEP_CHARS)

    def test_a_tool_outcome_updates_its_own_step_in_place(self) -> None:
        from pm_studio import mcp_activity

        def run_agent(command, text, *, cwd, env, timeout, on_message, **kwargs):
            on_message(self._assistant(
                {"type": "tool_use", "id": "a", "name": "mcp__dev-db__query", "input": {"sql": "select 1"}},
                {"type": "tool_use", "id": "b", "name": "Read", "input": {"file_path": "/r/README.md"}},
            ))
            on_message({"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "b", "content": "file text"},
                {"type": "tool_result", "tool_use_id": "a", "content": "read ECONNRESET", "is_error": True},
            ]}})
            on_message(self._assistant({"type": "text", "text": "The database is unreachable."}))
            return 0, {"type": "result", "result": "done"}, "", ""

        log = mcp_activity.McpActivityLog(self.repo / "ws" / "mcp-activity.jsonl")
        with mock.patch.object(tasks_module, "run_agent", run_agent), \
                mock.patch.object(mcp_activity, "ACTIVITY_LOG", log):
            self.registry._run_and_record("s3", "d", 1.0, "web", "", None, False)
        steps = self.registry.get_task("s3")["progress"]
        self.assertEqual([s["kind"] for s in steps], ["tool", "tool", "text"])
        db, read, _ = steps
        self.assertEqual((db["id"], db["ok"], db["outcome"]), ("a", False, "read ECONNRESET"))
        self.assertEqual(db["text"], "Using dev-db: query (select 1)")
        self.assertIsInstance(db["duration_ms"], int)
        self.assertEqual((read["ok"], read["outcome"]), (True, ""))
        # Only the MCP call is logged, attributed to this task.
        (entry,) = log.entries()
        self.assertEqual((entry["source"], entry["task_id"], entry["server"], entry["ok"]),
                         ("task", "s3", "dev-db", False))

    def test_a_timeout_is_reported(self) -> None:
        with mock.patch.object(tasks_module, "run_agent",
                               lambda *a, **k: (None, None, "", "")):
            is_error, result, _ = self.registry._execute("d", "web")
        self.assertTrue(is_error)
        self.assertIn("timed out", result)


class UnavailableSourceRuleTest(_RegistryCase):
    """A dev task whose data source is unreachable reports it - it never hunts the disk
    for configuration or credentials to get in another way."""

    def test_every_task_carries_the_stop_and_report_rule(self) -> None:
        for quick in (True, False):
            _, fake = self._run(f"u{quick}", quick=quick)
            prompt = fake.calls[0]["input"]
            self.assertIn("Never search the filesystem for configuration files, credentials", prompt)
            self.assertLess(prompt.index("Look at it"), prompt.index("retry at most once"))


class McpPromptNoteTest(unittest.TestCase):
    def test_the_pm_is_told_to_stop_on_a_failing_mcp_server(self) -> None:
        note = agent_module._mcp_prompt_note(["dev-db"])
        self.assertIn("retry at most once, then STOP", note)
        self.assertIn("do not dispatch a dev task to get the same data another way", note)
        self.assertEqual(agent_module._mcp_prompt_note([]), "")
