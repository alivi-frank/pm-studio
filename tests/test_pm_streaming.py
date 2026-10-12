"""Live progress for PM turns, and a roadmap context that stops re-sending itself.

Why: a PM turn used to run `claude -p --output-format json`, so the chat showed nothing
until the whole turn was over - a minute or more of a frozen page while the PM was in fact
reading files and dispatching work. And every turn prepended the full roadmap snapshot to
a conversation that is resumed turn after turn, so each one carried every earlier
snapshot too: slower and costlier with every message.
"""

import json
import sys
import tempfile
import textwrap
import threading
import types
import unittest
from pathlib import Path

from pm_studio import agent as agent_module
from pm_studio.agent import PMAgent, progress_events, roadmap_delta


def _make_agent(worktree: Path) -> PMAgent:
    session = types.SimpleNamespace(
        id="s1", product=None, initiative_id=None, adopted_products=[],
        model="claude-sonnet-5", mode="build", worktree_path=str(worktree),
    )
    return PMAgent(session, threading.Lock())


def _assistant(*blocks: dict) -> dict:
    return {"type": "assistant", "message": {"content": list(blocks)}}


class StreamClaudeTest(unittest.TestCase):
    """_stream_claude against a real child process standing in for the CLI."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.script = Path(self._tmp.name) / "fake_claude.py"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run(self, body: str, text: str = "hello", timeout: float = 30):
        self.script.write_text(textwrap.dedent(body), encoding="utf-8")
        seen: list[dict] = []
        outcome = agent_module._stream_claude(
            [sys.executable, str(self.script)], text,
            cwd=self._tmp.name, env=None, timeout=timeout, on_message=seen.append,
        )
        return outcome, seen

    def test_messages_arrive_in_order_and_the_result_is_returned(self) -> None:
        (code, result, stray, _), seen = self._run("""
            import json, sys
            prompt = sys.stdin.buffer.read().decode("utf-8")  # as the CLI does
            print(json.dumps({"type": "system", "subtype": "init"}), flush=True)
            print(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "Looking ✓"}]}}), flush=True)
            print("not json", flush=True)
            print(json.dumps({"type": "result", "result": "echo:" + prompt, "session_id": "c9"}), flush=True)
        """, text="hola → mundo")
        self.assertEqual(code, 0)
        self.assertEqual(result["result"], "echo:hola → mundo")
        self.assertEqual([m["type"] for m in seen], ["system", "assistant"])
        self.assertEqual(stray, "not json")

    def test_no_result_line_is_reported_as_none(self) -> None:
        (code, result, _, stderr), _ = self._run("""
            import sys
            sys.stderr.write("boom")
            sys.exit(3)
        """)
        self.assertEqual((code, result), (3, None))
        self.assertEqual(stderr, "boom")

    def test_a_turn_past_its_timeout_is_killed(self) -> None:
        (code, result, _, _), _ = self._run("""
            import time
            time.sleep(30)
        """, timeout=1)
        self.assertIsNone(code)
        self.assertIsNone(result)

    def test_a_failing_listener_never_breaks_the_turn(self) -> None:
        self.script.write_text(
            'import json\nprint(json.dumps({"type": "assistant"}))\n'
            'print(json.dumps({"type": "result", "result": "ok"}))\n',
            encoding="utf-8",
        )

        def broken(message):
            raise RuntimeError("listener bug")

        code, result, _, _ = agent_module._stream_claude(
            [sys.executable, str(self.script)], "", cwd=self._tmp.name, env=None,
            timeout=30, on_message=broken,
        )
        self.assertEqual((code, result["result"]), (0, "ok"))


class ProgressEventsTest(unittest.TestCase):
    def test_text_and_tool_blocks_become_progress(self) -> None:
        events = progress_events(_assistant(
            {"type": "text", "text": "Let me check the index."},
            {"type": "tool_use", "name": "Read", "input": {"file_path": "C:/repo/PROJECT_INDEX.md"}},
            {"type": "thinking", "thinking": "private"},
            {"type": "text", "text": "   "},
        ))
        self.assertEqual(events, [
            {"type": "pm_progress", "kind": "text", "text": "Let me check the index."},
            {"type": "pm_progress", "kind": "tool", "text": "Reading PROJECT_INDEX.md"},
        ])

    def test_other_messages_carry_none(self) -> None:
        self.assertEqual(progress_events({"type": "user", "message": {"content": []}}), [])
        self.assertEqual(progress_events({"type": "system"}), [])

    def test_curl_labels_never_carry_the_command_or_the_agent_token(self) -> None:
        token = "SECRET-AGENT-TOKEN"
        cases = {
            f'curl -s -X POST http://h/tasks/s1 -H "X-PM-Studio-Agent: {token}" -d \'{{}}\'': "Dispatching a dev task",
            f'curl -s http://h/tasks/s1 -H "X-PM-Studio-Agent: {token}"': "Checking dev tasks",
            f'curl -s -X POST http://h/sessions/s1/checkout/epicride -H "X: {token}"': "Checking a system's code out",
            f'curl -s -X POST http://h/sessions/s1/meta -H "X: {token}"': "Updating the session title",
            f'curl -s -X PATCH http://h/roadmap/p/items/1 -H "X: {token}"': "Updating the roadmap",
            f'curl -s http://h/elsewhere -H "X: {token}"': "Calling PM Studio",
        }
        for command, label in cases.items():
            (event,) = progress_events(_assistant(
                {"type": "tool_use", "name": "Bash", "input": {"command": command}}
            ))
            self.assertEqual(event["text"], label)
            self.assertNotIn(token, event["text"])

    def test_other_tools_get_short_labels(self) -> None:
        def label(name, tool_input):
            (event,) = progress_events(_assistant({"type": "tool_use", "name": name, "input": tool_input}))
            return event["text"]

        self.assertEqual(label("Bash", {"command": "ls -la src/epicride"}), "Running ls")
        self.assertEqual(label("Write", {"file_path": "/w/SPEC.md"}), "Writing SPEC.md")
        self.assertEqual(label("Glob", {"pattern": "src/**/*.ts"}), "Searching for src/**/*.ts")
        self.assertEqual(label("WebFetch", {"url": "https://example.com/a/b"}), "Reading example.com")
        self.assertEqual(label("mcp__dev-db__query", {"sql": "select 1"}), "Using dev-db: query")


class TurnProgressTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_snapshot = agent_module.gitsnapshot.snapshot
        agent_module.gitsnapshot.snapshot = lambda *a, **k: None
        self.agent = _make_agent(Path(self._tmp.name))

    def tearDown(self) -> None:
        agent_module.gitsnapshot.snapshot = self._orig_snapshot
        self._tmp.cleanup()

    def test_progress_is_yielded_live_and_kept_out_of_the_transcript(self) -> None:
        def fake_pm_turn(text: str) -> dict:
            self.agent._progress_sink({"type": "pm_progress", "kind": "tool", "text": "Reading A.md"})
            self.agent._progress_sink({"type": "pm_progress", "kind": "text", "text": "done"})
            return {"type": "pm_reply", "text": "done"}

        self.agent._run_pm_turn = fake_pm_turn
        events = list(self.agent.handle_user_message("hi"))
        self.assertEqual(
            [e["type"] for e in events], ["pm_working", "pm_progress", "pm_progress", "pm_reply"]
        )
        self.assertEqual([e["role"] for e in self.agent.load_history()], ["user", "pm"])
        self.assertIsNone(self.agent._progress_sink)

    def test_an_exception_in_the_turn_becomes_a_pm_error(self) -> None:
        def broken(text: str) -> dict:
            raise RuntimeError("kaboom")

        self.agent._run_pm_turn = broken
        events = list(self.agent.handle_user_message("hi"))
        self.assertEqual(events[-1], {"type": "pm_error", "message": "kaboom"})


class CurrentInstructionsTest(unittest.TestCase):
    """`claude -p --resume` keeps the system prompt a conversation started with, so a
    resumed conversation never saw a restart's new agent token (every curl 401'd) or any
    later prompt change. The current prompt is re-stated once per change instead."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.agent = _make_agent(Path(self._tmp.name))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_a_new_conversation_gets_no_note(self) -> None:
        self.agent.claude_session_id = None
        self.assertEqual(self.agent._with_current_instructions("hi", "PROMPT-1"), "hi")

    def test_a_resumed_conversation_after_a_restart_gets_the_prompt_once(self) -> None:
        self.agent.claude_session_id = "conv"  # resumed, nothing known about its prompt
        first = self.agent._with_current_instructions("hi", "PROMPT-1")
        self.assertIn("REPLACE the system prompt", first)
        self.assertIn("PROMPT-1", first)
        self.assertTrue(first.endswith("hi"))
        self.assertEqual(self.agent._with_current_instructions("again", "PROMPT-1"), "again")

    def test_a_changed_prompt_is_restated(self) -> None:
        self.agent.claude_session_id = "conv"
        self.agent._with_current_instructions("hi", "PROMPT-1")
        self.assertIn("PROMPT-2", self.agent._with_current_instructions("hi", "PROMPT-2"))

    def test_a_failed_turn_restates_next_time(self) -> None:
        original = agent_module.gitsnapshot.snapshot
        agent_module.gitsnapshot.snapshot = lambda *a, **k: None
        try:
            self.agent.claude_session_id = "conv"
            self.agent._instructions_sent = self.agent.system_prompt
            self.agent._run_pm_turn = lambda text: {"type": "pm_error", "message": "x"}
            list(self.agent.handle_user_message("hi"))
        finally:
            agent_module.gitsnapshot.snapshot = original
        self.assertIsNone(self.agent._instructions_sent)


class RoadmapDeltaTest(unittest.TestCase):
    SNAPSHOT = "\n".join(f"- [now] Change {i} (id c{i})" for i in range(40))

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.agent = _make_agent(Path(self._tmp.name))
        self.agent.claude_session_id = "conv-1"  # a resumed conversation

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _turn(self, snapshot: str) -> str:
        return self.agent._with_roadmap_context("MESSAGE", snapshot)

    def test_first_turn_is_full_then_unchanged_then_delta(self) -> None:
        self.assertIn("Roadmap snapshot for this turn", self._turn(self.SNAPSHOT))
        unchanged = self._turn(self.SNAPSHOT)
        self.assertIn("unchanged since the last snapshot", unchanged)
        self.assertNotIn("Change 7", unchanged)
        changed = self.SNAPSHOT.replace("Change 7 (id c7)", "Change 7 renamed (id c7)")
        delta = self._turn(changed)
        self.assertIn("REMOVED: - [now] Change 7 (id c7)", delta)
        self.assertIn("ADDED: - [now] Change 7 renamed (id c7)", delta)
        self.assertNotIn("Change 8", delta)
        self.assertTrue(delta.endswith("MESSAGE"))

    def test_a_fresh_conversation_always_gets_the_full_snapshot(self) -> None:
        self._turn(self.SNAPSHOT)
        self.agent.claude_session_id = None  # reset / brand-new conversation
        self.assertIn("Roadmap snapshot for this turn", self._turn(self.SNAPSHOT))

    def test_a_large_change_is_sent_in_full(self) -> None:
        self._turn(self.SNAPSHOT)
        rewritten = self.SNAPSHOT.replace("[now]", "[next]")
        self.assertIn("Roadmap snapshot for this turn", self._turn(rewritten))

    def test_the_full_snapshot_is_refreshed_periodically(self) -> None:
        self._turn(self.SNAPSHOT)
        for _ in range(agent_module.ROADMAP_FULL_EVERY):
            self.assertIn("unchanged", self._turn(self.SNAPSHOT))
        self.assertIn("Roadmap snapshot for this turn", self._turn(self.SNAPSHOT))

    def test_a_failed_turn_resends_in_full(self) -> None:
        agent_module_snapshot = agent_module.gitsnapshot.snapshot
        agent_module.gitsnapshot.snapshot = lambda *a, **k: None
        try:
            self.agent._run_pm_turn = lambda text: {"type": "pm_error", "message": "x"}
            list(self.agent.handle_user_message("hi", roadmap_context=self.SNAPSHOT))
        finally:
            agent_module.gitsnapshot.snapshot = agent_module_snapshot
        self.assertIn("Roadmap snapshot for this turn", self._turn(self.SNAPSHOT))

    def test_no_roadmap_context_leaves_the_prompt_alone(self) -> None:
        self.assertEqual(self._turn(""), "MESSAGE")

    def test_roadmap_delta_lists_only_changed_lines(self) -> None:
        self.assertEqual(roadmap_delta("a\nb\nc", "a\nB\nc\nd"), "REMOVED: b\nADDED: B\nADDED: d")


if __name__ == "__main__":
    unittest.main()


class DevStepLabelTest(unittest.TestCase):
    def _label(self, command: str) -> str:
        (event,) = progress_events(_assistant({"type": "tool_use", "name": "Bash", "input": {"command": command}}))
        return event["text"]

    def test_local_env_calls_read_as_what_they_do(self) -> None:
        base = 'curl -s -X POST "$PM_STUDIO_URL/local-env/epicride/api/{}?session=$S" -H "$PM_STUDIO_AGENT_HEADER"'
        self.assertEqual(self._label(base.format("start")), "Starting epicride/api")
        self.assertEqual(self._label(base.format("wait")), "Waiting for epicride/api to be up")
        self.assertEqual(self._label(base.format("logs")), "Reading epicride/api logs")
        self.assertEqual(self._label('curl -s "$PM_STUDIO_URL/local-env?session=$S" -H "$H"'),
                         "Checking the local environment")

    def test_package_and_git_subcommands_show_but_never_their_arguments(self) -> None:
        self.assertEqual(self._label("cd src/epicride/backend && npm install 2>&1 | tail"), "Running npm install")
        self.assertEqual(self._label("git status --porcelain -- secret-path"), "Running git status")
        self.assertEqual(self._label("python scripts/x.py --password hunter2"), "Running python")
