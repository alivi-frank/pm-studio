"""Tests for PMAgent transcript persistence.

Focus: the chat_history.json write is split so the incoming stakeholder/system entry
is persisted BEFORE the PM turn runs (so a mid-turn reload's GET /history already shows
the just-sent message) and the reply entry is appended AFTER the turn completes - while
still producing exactly the same two-entry transcript a single combined write did.
"""

import threading
import tempfile
import types
import unittest
from pathlib import Path

from pm_studio import agent as agent_module
from pm_studio.agent import PMAgent


def _make_agent(worktree: Path) -> PMAgent:
    session = types.SimpleNamespace(
        id="testsess",
        product=None,
        initiative_id=None,
        adopted_products=[],
        model="claude-sonnet-5",
        worktree_path=str(worktree),
    )
    return PMAgent(session, threading.Lock())


class TranscriptPersistenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        # gitsnapshot.snapshot shells out to git; stub it so a turn doesn't need a repo.
        self._orig_snapshot = agent_module.gitsnapshot.snapshot
        agent_module.gitsnapshot.snapshot = lambda *a, **k: None
        self.agent = _make_agent(self.tmp)

    def tearDown(self) -> None:
        agent_module.gitsnapshot.snapshot = self._orig_snapshot
        self._tmp.cleanup()

    def test_user_message_persisted_before_reply(self) -> None:
        """The core regression guard: at the moment the PM turn runs, load_history()
        must already contain the just-sent user message (the reply is produced
        separately, afterwards)."""
        captured = {}

        def fake_pm_turn(text: str) -> dict:
            captured["mid_turn"] = self.agent.load_history()
            return {"type": "pm_reply", "text": "hi back"}

        self.agent._run_pm_turn = fake_pm_turn
        list(self.agent.handle_user_message("hello PM"))

        mid = captured["mid_turn"]
        self.assertEqual(len(mid), 1, "user entry should be on disk before the reply")
        self.assertEqual(mid[0]["role"], "user")
        self.assertEqual(mid[0]["text"], "hello PM")
        self.assertIn("ts", mid[0])

    def test_completed_turn_yields_two_entry_transcript(self) -> None:
        """A normal completed turn still persists exactly user + pm, same as before."""
        self.agent._run_pm_turn = lambda text: {"type": "pm_reply", "text": "done"}
        list(self.agent.handle_user_message("build it"))

        hist = self.agent.load_history()
        self.assertEqual([e["role"] for e in hist], ["user", "pm"])
        self.assertEqual(hist[0]["text"], "build it")
        self.assertEqual(hist[1]["text"], "done")
        self.assertIn("ts", hist[0])
        self.assertIn("ts", hist[1])
        self.assertEqual(self.agent.last_message_role(), "assistant")

    def test_error_turn_yields_user_plus_error(self) -> None:
        """An errored turn persists user + error, and last_message_role stays assistant."""
        self.agent._run_pm_turn = lambda text: {"type": "pm_error", "message": "boom"}
        list(self.agent.handle_user_message("do a thing"))

        hist = self.agent.load_history()
        self.assertEqual([e["role"] for e in hist], ["user", "error"])
        self.assertEqual(hist[1]["text"], "boom")
        self.assertEqual(self.agent.last_message_role(), "assistant")

    def test_task_completion_flows_as_system_plus_reply(self) -> None:
        """handle_task_completion (role system) benefits from the same split: the system
        entry lands before the turn, the reply after."""
        captured = {}

        def fake_pm_turn(text: str) -> dict:
            captured["mid_turn"] = self.agent.load_history()
            return {"type": "pm_reply", "text": "ack"}

        self.agent._run_pm_turn = fake_pm_turn
        task = {"id": "abc123", "status": "done", "description": "build widget"}
        list(self.agent.handle_task_completion(task))

        mid = captured["mid_turn"]
        self.assertEqual(len(mid), 1)
        self.assertEqual(mid[0]["role"], "system")

        hist = self.agent.load_history()
        self.assertEqual([e["role"] for e in hist], ["system", "pm"])

    def test_incoming_entry_preserves_attachments(self) -> None:
        """Attachments are stamped onto the incoming entry exactly as before."""
        self.agent._append_incoming_entry(
            "look at this", attachments=["a.png", "b.png"], role="user"
        )
        hist = self.agent.load_history()
        self.assertEqual(hist[0]["attachments"], ["a.png", "b.png"])
        # No attachments key when there are none.
        self.agent._append_incoming_entry("plain", role="user")
        self.assertNotIn("attachments", self.agent.load_history()[1])


if __name__ == "__main__":
    unittest.main()
