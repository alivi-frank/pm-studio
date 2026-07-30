"""Tests for the role capability matrix and the audit trail.

The matrix is the security-relevant part of enterprise mode, so these assertions are
written to fail loudly if a future change quietly widens a role - especially
`dispatch_dev_task`, which is equivalent to running arbitrary code on the host.
"""

import tempfile
import unittest
from pathlib import Path

from pm_studio.accounts import ROLES, agent_principal
from pm_studio.authz import (
    CAPABILITIES,
    CAPABILITY_LABELS,
    AuditLog,
    capabilities_of,
    describe_matrix,
    role_has,
)


class CapabilityMatrixTest(unittest.TestCase):
    def test_dispatching_a_dev_agent_is_pm_and_admin_only(self) -> None:
        """Dev agents run with bypassed permissions, so this capability is effectively
        'may execute code on this host'. If this test starts failing because a role was
        added to the tuple, that is a deliberate decision someone must justify."""
        self.assertEqual(CAPABILITIES["dispatch_dev_task"], ("admin", "pm"))
        self.assertTrue(role_has("admin", "dispatch_dev_task"))
        self.assertTrue(role_has("pm", "dispatch_dev_task"))
        self.assertFalse(role_has("reviewer", "dispatch_dev_task"))
        self.assertFalse(role_has("viewer", "dispatch_dev_task"))

    def test_reads_are_open_to_every_role(self) -> None:
        """Transparency is the deployment default: everyone who can sign in sees the
        whole roadmap. Roles restrict what you may do, not what you may see."""
        for role in ROLES:
            self.assertTrue(role_has(role, "view"), role)

    def test_admin_holds_every_capability(self) -> None:
        """The owner set the instance up and must never be locked out of part of it."""
        for capability in CAPABILITIES:
            self.assertTrue(role_has("admin", capability), capability)

    def test_only_admin_manages_people(self) -> None:
        self.assertEqual(CAPABILITIES["manage_users"], ("admin",))

    def test_viewer_holds_nothing_but_view(self) -> None:
        self.assertEqual(capabilities_of("viewer"), ["view"])

    def test_reviewer_cannot_yet_write_anything(self) -> None:
        """The reviewer workflow is deliberately deferred; until it lands the role is
        read-only, so it must not silently carry write capabilities."""
        self.assertEqual(capabilities_of("reviewer"), ["view"])

    def test_pm_can_build_but_not_administer(self) -> None:
        held = capabilities_of("pm")
        self.assertIn("run_session", held)
        self.assertIn("manage_session_lifecycle", held)
        self.assertIn("manage_roadmap", held)
        self.assertIn("dispatch_dev_task", held)
        self.assertNotIn("manage_users", held)

    def test_agent_principal_can_run_the_core_loop_but_not_the_roster(self) -> None:
        """The per-process agent token must be able to do exactly what an agent did in
        personal mode, and nothing administrative."""
        agent = agent_principal()
        self.assertTrue(role_has(agent.role, "dispatch_dev_task"))
        self.assertTrue(role_has(agent.role, "manage_roadmap"))
        self.assertTrue(role_has(agent.role, "run_session"))
        self.assertFalse(role_has(agent.role, "manage_users"))

    def test_every_role_appears_in_the_described_matrix(self) -> None:
        matrix = describe_matrix()
        self.assertEqual(set(matrix), set(ROLES))

    def test_every_capability_has_a_human_label(self) -> None:
        """The 403 body names the capability, so a missing label would render as a
        KeyError instead of an explanation."""
        self.assertEqual(set(CAPABILITY_LABELS), set(CAPABILITIES))


class AuditLogTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "nested" / "audit.jsonl"
        self.log = AuditLog(self.path)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_records_the_actor_not_just_the_action(self) -> None:
        entry = self.log.record(
            agent_principal(), "dev_task.dispatched", "default", "add a button"
        )
        self.assertEqual(entry.actor_id, "agent")
        self.assertEqual(entry.actor_role, "pm")
        self.assertEqual(entry.action, "dev_task.dispatched")
        self.assertEqual(entry.target, "default")
        self.assertEqual(entry.detail, "add a button")

    def test_creates_its_directory_and_appends(self) -> None:
        self.log.record(agent_principal(), "one")
        self.log.record(agent_principal(), "two")
        self.assertTrue(self.path.exists())
        self.assertEqual(len(self.path.read_text().strip().splitlines()), 2)

    def test_tail_is_newest_first_and_bounded(self) -> None:
        for index in range(5):
            self.log.record(agent_principal(), f"action-{index}")
        entries = self.log.tail(limit=2)
        self.assertEqual([e["action"] for e in entries], ["action-4", "action-3"])

    def test_missing_file_reads_as_empty(self) -> None:
        self.assertEqual(self.log.tail(), [])

    def test_truncated_final_line_is_skipped(self) -> None:
        """A process killed mid-append must not make the whole view unreadable."""
        self.log.record(agent_principal(), "good")
        with self.path.open("a") as handle:
            handle.write('{"at": 1.0, "action": "trunc')
        entries = self.log.tail()
        self.assertEqual([e["action"] for e in entries], ["good"])


if __name__ == "__main__":
    unittest.main()
