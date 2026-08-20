"""Tests for the per-session PM turn queue behind the chat websocket.

The failure this exists to prevent: the stakeholder having a thought while the PM is
mid-turn and having nowhere to put it. The receive loop used to run the turn inline, so
a connection could not accept a second message until the first reply landed, and the UI
disabled the composer to match. Now the loop only enqueues, and one worker per session
drains the queue.

What is pinned here:
  - a message sent mid-turn is accepted, and reported as queued (`ahead` > 0);
  - it is written to the session's queue file the moment it arrives and removed as its
    turn starts, so a reload finds it in exactly one of the two files;
  - queued turns still run one at a time, in the order they were sent - two `--resume`
    calls racing on one Claude session id is the thing the queue must never allow;
  - a sender that disconnects while parked does not take its message down with it: the
    turn runs and is transcribed, only the events are lost;
  - the worker/queue registries are left clean once the queue drains, so the next
    message starts a fresh worker instead of waiting on a dead one;
  - a record stranded by a restart is picked back up when a page reconnects, and one
    already being handled in memory is not run a second time.
"""

import asyncio
import base64
import json
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

from pm_studio import agent as agent_module
from pm_studio import server as server_module
from pm_studio.agent import PMAgent

SESSION_ID = "s-queue"


class FakePMAgent:
    """Records turn order and refuses to overlap silently: each turn parks on `gate`
    until the test releases it, so a second turn starting early is observable."""

    def __init__(self) -> None:
        self.started: list[str] = []
        self.finished: list[str] = []
        self.overlapped = False
        self.gate = threading.Event()
        self._active = 0

    def handle_user_message(self, text, attachments, other_ctx, roadmap_ctx, pending_id=None):
        self._active += 1
        if self._active > 1:
            self.overlapped = True
        self.started.append(text)
        yield {"type": "pm_working"}
        self.gate.wait(5)
        self._active -= 1
        self.finished.append(text)
        yield {"type": "pm_reply", "text": f"re: {text}", "agent_usage": None}


class FakeSocket:
    def __init__(self, alive: bool = True) -> None:
        self.alive = alive
        self.sent: list[dict] = []

    async def send_text(self, message: str) -> None:
        if not self.alive:
            raise RuntimeError("connection closed")
        self.sent.append(json.loads(message))


class ChatQueueTest(unittest.TestCase):
    def setUp(self) -> None:
        self.agent = FakePMAgent()
        runtime = mock.Mock(pm_agent=self.agent)
        patches = [
            mock.patch.object(server_module.sessions, "get_runtime", return_value=runtime),
            mock.patch.object(
                server_module.sessions, "describe_other_active_sessions", return_value=""
            ),
            mock.patch.object(server_module, "_roadmap_context_for", return_value=""),
            mock.patch.object(server_module, "_record_signal"),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(server_module._chat_queues.pop, SESSION_ID, None)
        self.addCleanup(server_module._chat_workers.pop, SESSION_ID, None)
        self.addCleanup(server_module._chat_inflight.discard, SESSION_ID)
        self.addCleanup(server_module._chat_queued_ids.pop, SESSION_ID, None)

    def _item(self, text: str, socket: FakeSocket) -> dict:
        return {
            "pending": {"id": f"id-{text}", "text": text, "attachments": [], "ts": 0.0},
            "websocket": socket,
            "send_lock": asyncio.Lock(),
            "user": None,
        }

    async def _until(self, predicate) -> None:
        for _ in range(500):
            if predicate():
                return
            await asyncio.sleep(0.01)
        self.fail("timed out waiting for the queue to make progress")

    def test_messages_sent_mid_turn_are_queued_and_run_in_order(self) -> None:
        socket = FakeSocket()

        async def scenario() -> None:
            first = server_module._enqueue_pm_turn(SESSION_ID, self._item("one", socket))
            worker = server_module._chat_workers[SESSION_ID]
            # Nothing is ahead of the first message: it starts immediately.
            self.assertEqual(first, 0)
            await self._until(lambda: self.agent.started == ["one"])

            # Typed while the PM is thinking - accepted, and reported as parked behind
            # the turn already running.
            second = server_module._enqueue_pm_turn(SESSION_ID, self._item("two", socket))
            third = server_module._enqueue_pm_turn(SESSION_ID, self._item("three", socket))
            self.assertEqual((second, third), (1, 2))

            self.agent.gate.set()
            await asyncio.wait_for(worker, timeout=10)

        asyncio.run(scenario())

        self.assertEqual(self.agent.started, ["one", "two", "three"])
        self.assertEqual(self.agent.finished, ["one", "two", "three"])
        self.assertFalse(self.agent.overlapped, "queued turns must never run concurrently")
        self.assertEqual(
            [e["type"] for e in socket.sent],
            ["pm_working", "pm_reply"] * 3,
        )
        # Each pm_working names the queue record it belongs to, so a page showing that
        # record as a queued bubble knows which one to promote.
        self.assertEqual(
            [e["queued_id"] for e in socket.sent if e["type"] == "pm_working"],
            ["id-one", "id-two", "id-three"],
        )
        self.assertEqual(
            [e["text"] for e in socket.sent if e["type"] == "pm_reply"],
            ["re: one", "re: two", "re: three"],
        )

    def test_a_disconnected_sender_still_gets_its_turn_run(self) -> None:
        """Closing the tab while parked must not swallow the message - it is already on
        the session. Only the events are lost, and the turn after it still runs."""
        dead = FakeSocket(alive=False)
        live = FakeSocket()

        async def scenario() -> None:
            server_module._enqueue_pm_turn(SESSION_ID, self._item("orphan", dead))
            worker = server_module._chat_workers[SESSION_ID]
            await self._until(lambda: self.agent.started == ["orphan"])
            server_module._enqueue_pm_turn(SESSION_ID, self._item("after", live))
            self.agent.gate.set()
            await asyncio.wait_for(worker, timeout=10)

        asyncio.run(scenario())

        self.assertEqual(self.agent.finished, ["orphan", "after"])
        self.assertEqual(dead.sent, [])
        self.assertEqual([e["type"] for e in live.sent], ["pm_working", "pm_reply"])

    def test_a_drained_queue_leaves_nothing_registered(self) -> None:
        socket = FakeSocket()
        self.agent.gate.set()

        async def scenario() -> None:
            server_module._enqueue_pm_turn(SESSION_ID, self._item("only", socket))
            worker = server_module._chat_workers[SESSION_ID]
            await asyncio.wait_for(worker, timeout=10)
            # A later message must start a fresh worker, not wait on the finished one.
            self.assertNotIn(SESSION_ID, server_module._chat_workers)
            self.assertNotIn(SESSION_ID, server_module._chat_queues)
            self.assertNotIn(SESSION_ID, server_module._chat_inflight)
            self.assertNotIn(SESSION_ID, server_module._chat_queued_ids)
            self.assertEqual(server_module._enqueue_pm_turn(SESSION_ID, self._item("next", socket)), 0)
            await asyncio.wait_for(server_module._chat_workers[SESSION_ID], timeout=10)

        asyncio.run(scenario())
        self.assertEqual(self.agent.finished, ["only", "next"])


def _make_agent(worktree: Path) -> PMAgent:
    session = types.SimpleNamespace(
        id="testsess",
        product=None,
        initiative_id=None,
        adopted_products=[],
        model="claude-sonnet-5",
        mode="build",
        worktree_path=str(worktree),
    )
    return PMAgent(session, threading.Lock())


class PendingMessagePersistenceTest(unittest.TestCase):
    """The durable half of the queue: what is on disk while a message waits, and what
    moves it from the queue file to the transcript."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_snapshot = agent_module.gitsnapshot.snapshot
        agent_module.gitsnapshot.snapshot = lambda *a, **k: None  # no repo in the tmpdir
        self.agent = _make_agent(Path(self._tmp.name))

    def tearDown(self) -> None:
        agent_module.gitsnapshot.snapshot = self._orig_snapshot
        self._tmp.cleanup()

    def test_a_waiting_message_is_on_disk_before_anything_runs_it(self) -> None:
        """The whole point: the message exists independently of the connection that
        sent it, so a reload (which re-reads this file) still finds it."""
        record = self.agent.enqueue_pending("think about pricing")
        self.assertEqual([r["id"] for r in self.agent.load_pending()], [record["id"]])
        self.assertEqual(self.agent.load_pending()[0]["text"], "think about pricing")
        # Nothing has run, so it is not in the transcript yet.
        self.assertEqual(self.agent.load_history(), [])

        # A fresh agent over the same workspace reads it back - the reload case.
        self.assertEqual(
            [r["id"] for r in _make_agent(Path(self._tmp.name)).load_pending()],
            [record["id"]],
        )

    def test_images_are_decoded_on_arrival_not_when_the_turn_starts(self) -> None:
        """An attachment has to outlive the websocket message that carried it, so it is
        written to uploads/ at enqueue time and referenced by filename from then on."""
        png = base64.b64encode(b"not really a png, but bytes are bytes").decode()
        record = self.agent.enqueue_pending("look at this", [{"mime": "image/png", "data": png}])
        self.assertEqual(len(record["attachments"]), 1)
        self.assertTrue((self.agent.uploads_dir / record["attachments"][0]).is_file())

    def test_the_turn_moves_it_from_the_queue_file_to_the_transcript(self) -> None:
        """Exactly one of the two files holds it at any moment a reader can observe,
        and the transcript entry carries the queue record's id so a reader caught
        mid-move can tell the two copies are one message."""
        record = self.agent.enqueue_pending("ship it")
        seen = {}

        def fake_pm_turn(text: str) -> dict:
            seen["pending"] = self.agent.load_pending()
            seen["history"] = self.agent.load_history()
            return {"type": "pm_reply", "text": "on it"}

        self.agent._run_pm_turn = fake_pm_turn
        list(
            self.agent.handle_user_message(
                record["text"], record["attachments"], pending_id=record["id"]
            )
        )

        self.assertEqual(seen["pending"], [], "dropped from the queue as the turn starts")
        self.assertEqual(seen["history"][0]["id"], record["id"])
        self.assertEqual(self.agent.load_pending(), [])
        self.assertEqual([e["role"] for e in self.agent.load_history()], ["user", "pm"])

    def test_reset_clears_messages_that_never_ran(self) -> None:
        """Reset archives and clears the conversation; a message still waiting belongs
        to that conversation, not to the fresh one."""
        self.agent.enqueue_pending("never mind")
        self.agent.reset()
        self.assertEqual(self.agent.load_pending(), [])
        self.assertFalse(self.agent.pending_path.exists())


class ResumePendingTest(unittest.TestCase):
    """What happens on connect: leftovers from a dead process get run, live ones don't
    get run twice."""

    def setUp(self) -> None:
        self.addCleanup(server_module._chat_queues.pop, SESSION_ID, None)
        self.addCleanup(server_module._chat_workers.pop, SESSION_ID, None)
        self.addCleanup(server_module._chat_queued_ids.pop, SESSION_ID, None)

    def _resume(self, records: list[dict]) -> list[dict]:
        """Runs _resume_pending with the queue worker stubbed out, returning the items
        it would have enqueued."""
        runtime = mock.Mock(pm_agent=mock.Mock(load_pending=lambda: records))
        enqueued = []
        with mock.patch.object(
            server_module, "_enqueue_pm_turn", side_effect=lambda sid, item: enqueued.append(item)
        ):
            server_module._resume_pending(SESSION_ID, runtime, FakeSocket(), asyncio.Lock(), None)
        return enqueued

    def test_a_record_stranded_by_a_restart_is_picked_back_up(self) -> None:
        record = {"id": "abc", "text": "still waiting", "attachments": [], "ts": 0.0}
        self.assertEqual([i["pending"] for i in self._resume([record])], [record])

    def test_a_record_already_in_memory_is_left_alone(self) -> None:
        """The plain-reload case: the worker never stopped, so re-queueing the record
        would run the same message twice."""
        record = {"id": "abc", "text": "still waiting", "attachments": [], "ts": 0.0}
        server_module._chat_queued_ids[SESSION_ID] = {"abc"}
        self.assertEqual(self._resume([record]), [])


if __name__ == "__main__":
    unittest.main()
