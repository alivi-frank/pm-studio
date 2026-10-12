"""Stopping what is running: a PM turn, a dev task, a whole session.

Why: a PM turn or a dev task could only be waited out (up to 30 minutes each), and
archiving or terminating a session hid it while its PM turn, dev tasks and local
environment kept running - and spending - out of sight.

What is pinned here:
  - _stream_claude hands the process to `on_start` the moment it exists;
  - a stopped PM turn ends as a pm_error "Stopped by ..." that is written to the
    transcript like any failed turn, and its process is really gone;
  - a stopped dev task is recorded as "stopped" (never judged, never re-engaging the PM),
    and the whole process tree goes - the agent's own children included;
  - stopping a session with nothing running is a no-op that says so;
  - every path that ends or hides a session (archive, terminate, clean up, delete) stops
    it first.

The processes are real: a Python child standing in for the `claude` CLI, sleeping until
it is killed, so the kill is the real one.
"""

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

from fastapi import HTTPException

from pm_studio import agent as agent_module
from pm_studio import server as server_module
from pm_studio import tasks as tasks_module
from pm_studio.agent import PMAgent
from pm_studio.tasks import TaskRegistry

REAL_STREAM = agent_module._stream_claude

# Stands in for `claude`: reads its prompt, says one thing, starts a child of its own
# (as the CLI starts MCP servers) and sleeps until it is killed.
SLEEPER = textwrap.dedent("""
    import json, subprocess, sys, time
    sys.stdin.read()
    if len(sys.argv) > 1:
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        with open(sys.argv[1], "w") as handle:
            handle.write(str(child.pid))
    print(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "Working."}]}}), flush=True)
    time.sleep(30)
""")


def _alive(pid: int) -> bool:
    if os.name == "nt":
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True
        ).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _wait_for(predicate, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


class _TempCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.script = self.root / "fake_claude.py"
        self.script.write_text(SLEEPER, encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()


class StreamClaudeOnStartTest(_TempCase):
    def test_on_start_receives_the_running_process(self) -> None:
        script = self.root / "quick.py"
        script.write_text(
            'import json, sys\nsys.stdin.read()\nprint(json.dumps({"type": "result", "result": "ok"}))\n',
            encoding="utf-8",
        )
        seen: list[subprocess.Popen] = []
        code, result, _, _ = agent_module._stream_claude(
            [sys.executable, str(script)], "hi", cwd=str(self.root), env=None, timeout=30,
            on_message=lambda m: None, on_start=seen.append,
        )
        self.assertEqual((code, result["result"]), (0, "ok"))
        self.assertEqual(len(seen), 1)
        self.assertIsInstance(seen[0], subprocess.Popen)
        self.assertEqual(seen[0].returncode, 0)

    def test_killing_the_process_ends_the_run_early(self) -> None:
        procs: list[subprocess.Popen] = []

        def on_start(proc):
            procs.append(proc)
            threading.Timer(0.5, agent_module.kill_process_tree, args=(proc,)).start()

        started = time.monotonic()
        code, result, _, _ = agent_module._stream_claude(
            [sys.executable, str(self.script)], "hi", cwd=str(self.root), env=None, timeout=60,
            on_message=lambda m: None, on_start=on_start,
        )
        self.assertLess(time.monotonic() - started, 20)
        self.assertIsNone(result)
        self.assertIsNotNone(code, "a stop is not a timeout")
        self.assertIsNotNone(procs[0].poll())


class StopPMTurnTest(_TempCase):
    def setUp(self) -> None:
        super().setUp()
        worktree = self.root / "worktree"
        worktree.mkdir()
        session = types.SimpleNamespace(
            id="s1", product=None, initiative_id=None, adopted_products=[],
            model="claude-sonnet-5", mode="build", worktree_path=str(worktree),
        )
        self.agent = PMAgent(session, threading.Lock())
        patches = [
            mock.patch.object(agent_module.gitsnapshot, "snapshot", lambda *a, **k: None),
            # The PM's real command, run as the sleeper instead of the CLI.
            mock.patch.object(
                agent_module, "_stream_claude",
                lambda command, text, **kwargs: REAL_STREAM([sys.executable, str(self.script)], text, **kwargs),
            ),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def test_stopping_a_running_turn_ends_it_as_a_recorded_error(self) -> None:
        events: list[dict] = []
        turn = threading.Thread(target=lambda: events.extend(self.agent.handle_user_message("hi")))
        turn.start()
        self.assertTrue(_wait_for(lambda: self.agent._turn_proc is not None), "the turn never started")
        proc = self.agent._turn_proc

        self.assertTrue(self.agent.stop_turn())
        turn.join(20)
        self.assertFalse(turn.is_alive())
        self.assertIsNotNone(proc.poll(), "the process is gone")
        self.assertEqual(events[-1], {"type": "pm_error", "message": "Stopped by the user."})
        history = self.agent.load_history()
        self.assertEqual([(e["role"], e["text"]) for e in history],
                         [("user", "hi"), ("error", "Stopped by the user.")])
        self.assertIsNone(self.agent._stop_reason)
        self.assertFalse(self.agent.turn_active)

    def test_a_stop_names_who_asked(self) -> None:
        events: list[dict] = []
        turn = threading.Thread(target=lambda: events.extend(self.agent.handle_user_message("hi")))
        turn.start()
        self.assertTrue(_wait_for(lambda: self.agent._turn_proc is not None))
        self.agent.stop_turn("Ana")
        turn.join(20)
        self.assertEqual(events[-1]["message"], "Stopped by Ana.")

    def test_stopping_with_no_turn_running_does_nothing(self) -> None:
        self.assertFalse(self.agent.stop_turn())
        self.assertIsNone(self.agent._stop_reason)


def _init_repo(path: Path) -> None:
    for args in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"],
                 ["commit", "-q", "--allow-empty", "-m", "init"]):
        subprocess.run(["git", *args], cwd=path, check=True, capture_output=True)


class StopDevTaskTest(_TempCase):
    def setUp(self) -> None:
        super().setUp()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        _init_repo(self.repo)
        self.pidfile = self.root / "grandchild.pid"
        self.registry = TaskRegistry("sess", self.repo / "ws", self.repo, threading.Lock())
        self.judge = mock.Mock(return_value=None)
        self.registry._judge = self.judge
        self.updates: list[dict] = []
        self.registry.subscribe(self.updates.append)

        def run_agent(command, text, **kwargs):
            return REAL_STREAM([sys.executable, str(self.script), str(self.pidfile)], text, **kwargs)

        patch = mock.patch.object(tasks_module, "run_agent", run_agent)
        patch.start()
        self.addCleanup(patch.stop)

    def test_a_stopped_task_is_recorded_as_stopped_and_its_whole_tree_dies(self) -> None:
        task = self.registry.start_task("Build the thing")
        self.assertTrue(_wait_for(lambda: task["id"] in self.registry._procs and self.pidfile.exists()
                                  and self.pidfile.read_text().strip()))
        grandchild = int(self.pidfile.read_text())
        proc = self.registry._procs[task["id"]]

        stopped = self.registry.stop_task(task["id"])

        self.assertEqual(stopped["status"], "stopped")
        self.assertRegex(stopped["result"], r"^Stopped by the user after \d+s\.$")
        self.assertNotIn("judge", stopped)
        self.judge.assert_not_called()
        self.assertIsNotNone(proc.poll())
        self.assertTrue(_wait_for(lambda: not _alive(grandchild), 10), "the agent's own child survived")
        self.assertEqual(self.updates[-1]["status"], "stopped")
        self.assertEqual(self.registry.get_task(task["id"])["status"], "stopped")
        self.assertEqual((self.registry._procs, self.registry._threads, self.registry._stops), ({}, {}, {}))

        # ...and a stopped task never wakes the PM up, unlike a finished one.
        with mock.patch.object(server_module, "_auto_continue_pm") as auto, \
                mock.patch.object(server_module, "_main_loop", None):
            server_module._on_task_update("sess", stopped)
            server_module._on_task_update("sess", {**stopped, "status": "done"})
            self.assertTrue(_wait_for(lambda: auto.call_count == 1, 5))
            time.sleep(0.2)
        self.assertEqual(auto.call_count, 1)
        self.assertEqual(auto.call_args.args[1]["status"], "done")

    def test_stop_all_stops_every_running_task(self) -> None:
        first = self.registry.start_task("One")
        self.assertTrue(_wait_for(lambda: first["id"] in self.registry._procs))
        self.pidfile.unlink(missing_ok=True)
        second = self.registry.start_task("Two")
        self.assertTrue(_wait_for(lambda: second["id"] in self.registry._procs))
        stopped = self.registry.stop_all("the server shutting down")
        self.assertEqual(sorted(t["id"] for t in stopped), sorted([first["id"], second["id"]]))
        self.assertEqual({t["status"] for t in stopped}, {"stopped"})
        self.assertTrue(all(t["result"].startswith("Stopped by the server shutting down after ")
                            for t in stopped))

    def test_a_task_that_is_not_running_cannot_be_stopped(self) -> None:
        self.registry._write_task({"id": "t-done", "kind": "dev", "status": "done", "started_at": 1.0})
        self.assertIsNone(self.registry.stop_task("t-done"))
        self.assertIsNone(self.registry.stop_task("nope"))


class _Request:
    headers: dict = {}


class StopRoutesTest(_TempCase):
    def setUp(self) -> None:
        super().setUp()
        patch = mock.patch.object(server_module, "_require", return_value=None)
        patch.start()
        self.addCleanup(patch.stop)

    def _runtime(self, *, turn: bool = False, pending=(), tasks=()):
        pm_agent = mock.Mock()
        pm_agent.load_pending.return_value = [{"id": i} for i in pending]
        pm_agent.stop_turn.return_value = turn
        registry = mock.Mock()
        registry.stop_all.return_value = [{"id": t, "status": "stopped"} for t in tasks]
        return types.SimpleNamespace(pm_agent=pm_agent, task_registry=registry)

    def test_stopping_an_idle_session_is_a_no_op(self) -> None:
        runtime = self._runtime()
        with mock.patch.object(server_module.sessions, "get_runtime", return_value=runtime):
            report = server_module._stop_session("s-idle")
        self.assertEqual(report, {"session": "s-idle", "pm_turn": False, "tasks": [], "services": [], "discarded": []})
        self.assertFalse(server_module._stopped_anything(report))

    def test_stopping_a_busy_session_stops_every_part(self) -> None:
        runtime = self._runtime(turn=True, pending=["m1", "m2"], tasks=["t1"])
        with mock.patch.object(server_module.sessions, "get_runtime", return_value=runtime), \
                mock.patch.object(server_module.local_env, "stop_session", return_value=["web/api"]) as env, \
                mock.patch.object(server_module, "_discard_queued_turns") as discard:
            report = server_module._stop_session("s1", "Ana")
        self.assertEqual(report, {"session": "s1", "pm_turn": True, "tasks": ["t1"],
                                  "services": ["web/api"], "discarded": ["m1", "m2"]})
        discard.assert_called_once_with("s1")
        self.assertEqual([c.args[0] for c in runtime.pm_agent.drop_pending.call_args_list], ["m1", "m2"])
        runtime.pm_agent.stop_turn.assert_called_once_with("Ana")
        runtime.task_registry.stop_all.assert_called_once_with("Ana")
        env.assert_called_once_with("s1", "Stopped by Ana.")

    def test_one_part_failing_never_keeps_the_rest_running(self) -> None:
        runtime = self._runtime(tasks=["t1"])
        runtime.pm_agent.stop_turn.side_effect = RuntimeError("boom")
        with mock.patch.object(server_module.sessions, "get_runtime", return_value=runtime), \
                mock.patch.object(server_module.local_env, "stop_session", return_value=[]), \
                mock.patch("builtins.print"):
            report = server_module._stop_session("s1")
        self.assertEqual(report["tasks"], ["t1"])

    def test_the_session_route_404s_for_an_unknown_session(self) -> None:
        with mock.patch.object(server_module.sessions, "get", return_value=None):
            with self.assertRaises(HTTPException) as caught:
                server_module.stop_session("nope", _Request())
        self.assertEqual(caught.exception.status_code, 404)

    def test_the_task_route_404s_for_an_unknown_task_and_409s_for_a_finished_one(self) -> None:
        registry = mock.Mock()
        registry.get_task.side_effect = lambda task_id: {"id": "t1", "status": "done"} if task_id == "t1" else None
        registry.stop_task.return_value = None
        runtime = types.SimpleNamespace(task_registry=registry)
        with mock.patch.object(server_module.sessions, "get_runtime", return_value=runtime):
            with self.assertRaises(HTTPException) as unknown:
                server_module.stop_task("s1", "nope", _Request())
            with self.assertRaises(HTTPException) as finished:
                server_module.stop_task("s1", "t1", _Request())
        self.assertEqual((unknown.exception.status_code, finished.exception.status_code), (404, 409))

    def test_the_chat_route_stops_the_turn_and_discards_the_queue(self) -> None:
        runtime = self._runtime(turn=True, pending=["m1"])
        with mock.patch.object(server_module.sessions, "get_runtime", return_value=runtime), \
                mock.patch.object(server_module, "_discard_queued_turns"):
            report = server_module.stop_chat("s1", _Request())
        self.assertEqual(report, {"session": "s1", "pm_turn": True, "discarded": ["m1"]})
        runtime.task_registry.stop_all.assert_not_called()


class LifecycleStopsTest(unittest.TestCase):
    """Archive, terminate, clean up and delete all stop the session before anything else."""

    def test_every_path_that_ends_or_hides_a_session_stops_it_first(self) -> None:
        session = mock.Mock(is_default=False, status="active")
        session.to_dict.return_value = {"id": "s1"}
        merged = mock.Mock(is_default=False, status="merged")
        merged.to_dict.return_value = {"id": "s1"}
        cases = [
            ("archive", server_module.archive_session, session, "archiving the session"),
            ("terminate", server_module.terminate_session, session, "terminating the session"),
            ("cleanup", server_module.cleanup_session, merged, "cleaning up the session"),
            ("delete", server_module.delete_session, session, "deleting the session"),
        ]
        for name, route, existing, doing in cases:
            with self.subTest(name), \
                    mock.patch.object(server_module, "_require", return_value=None), \
                    mock.patch.object(server_module.sessions, "get", return_value=existing), \
                    mock.patch.object(server_module.sessions, name, return_value=existing) as lifecycle, \
                    mock.patch.object(server_module, "_release_local_env"), \
                    mock.patch.object(server_module, "_stop_session",
                                      return_value={"session": "s1", "pm_turn": False, "tasks": [],
                                                    "services": [], "discarded": []}) as stop:
                order: list[str] = []
                stop.side_effect = lambda *a, **k: order.append("stop") or stop.return_value
                lifecycle.side_effect = lambda *a, **k: order.append(name) or existing
                route("s1", _Request())
                stop.assert_called_once_with("s1", f"the user {doing}")
                self.assertEqual(order, ["stop", name])


if __name__ == "__main__":
    unittest.main()
