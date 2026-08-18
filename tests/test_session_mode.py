"""Tests for session modes: build (today's behavior) vs research/strategy (ideation,
planning, no code changes).

The failure this feature exists to prevent: a stakeholder working ideation or strategy
in a session, and the PM - whose default loop is "spec it, then dispatch it" - launching
dev tasks that modify code nobody decided to build yet. Research mode removes the
dispatch capability rather than asking the PM to hold back, using the same
prompt+allowlist pairing that scopes roadmap writes: what the PM is told and what it can
actually do are rebuilt together, so they can never disagree about whether this session
may dispatch.
"""

import dataclasses
import tempfile
import threading
import unittest
from pathlib import Path

from pm_studio import agent as agent_module
from pm_studio.sessions import MODES, Session, validate_mode


class ModeFieldTest(unittest.TestCase):
    """The persisted field and its back-compat contract."""

    def _session(self, **overrides) -> Session:
        defaults = dict(
            id="s1",
            name="Test session",
            branch="session/s1",
            worktree_path=None,
            base_branch="main",
            created_at=0.0,
            status="active",
            is_default=False,
        )
        return Session(**{**defaults, **overrides})

    def test_default_is_build(self) -> None:
        """Every session that predates the field is a build session - today's behavior,
        unchanged."""
        self.assertEqual(self._session().mode, "build")

    def test_a_record_written_before_the_field_loads_unchanged(self) -> None:
        """sessions.json back-compat: from_dict on an old record must not raise, and
        must land on build."""
        data = self._session().to_dict()
        del data["mode"]
        self.assertEqual(Session.from_dict(data).mode, "build")

    def test_validate_mode_accepts_each_declared_mode(self) -> None:
        for mode in MODES:
            self.assertEqual(validate_mode(mode), mode)

    def test_validate_mode_refuses_anything_else(self) -> None:
        for bogus in ("ideation", "BUILD", "", "readonly"):
            with self.assertRaises(ValueError):
                validate_mode(bogus)


class ResearchModeAgentTest(unittest.TestCase):
    """What a research PM is told, and what it is actually allowed to do - both halves,
    asserted together, in both directions."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _agent(self, mode="build", product=None):
        session = dataclasses.make_dataclass(
            "StubSession",
            ["id", "product", "initiative_id", "adopted_products", "model", "mode", "worktree_path"],
        )(
            id="s1",
            product=product,
            initiative_id=None,
            adopted_products=[],
            model="claude-opus-5",
            mode=mode,
            worktree_path=str(self.tmp),
        )
        return agent_module.PMAgent(session, threading.Lock())

    def _dispatch(self, pm) -> str:
        return f"Bash(curl -s -X POST {pm.tasks_base_url}*)"

    def test_a_build_session_keeps_todays_dispatch_grant(self) -> None:
        pm = self._agent(mode="build")
        self.assertIn(self._dispatch(pm), pm.allowed_tools)
        self.assertIn("Start a dev task", pm.system_prompt)

    def test_a_research_session_has_no_dispatch_grant(self) -> None:
        """The enforcement half: launching a dev task is structurally impossible, not
        discouraged - there is no allowlist entry for the CLI to match."""
        pm = self._agent(mode="research")
        self.assertNotIn(self._dispatch(pm), pm.allowed_tools)

    def test_a_research_session_is_never_shown_the_dispatch_command(self) -> None:
        """The behavioral half: a prompt that documents a curl the allowlist refuses
        produces a PM that retries a command that can never succeed."""
        pm = self._agent(mode="research")
        self.assertNotIn("Start a dev task", pm.system_prompt)
        self.assertNotIn(f"curl -s -X POST {pm.tasks_base_url}", pm.system_prompt)
        # And it is told what it is instead of being left to infer it from absence.
        self.assertIn("RESEARCH / STRATEGY session", pm.system_prompt)

    def test_reading_task_records_stays_granted_in_research_mode(self) -> None:
        """A session switched to research mid-life still has task history worth
        reading; reading is not the boundary."""
        pm = self._agent(mode="research")
        self.assertIn(f"Bash(curl -s {pm.tasks_base_url}*)", pm.allowed_tools)

    def test_switching_to_research_revokes_dispatch_live(self) -> None:
        """set_mode is the graduation/parking lever - both halves must flip together,
        without a rebuild or a restart."""
        pm = self._agent(mode="build")
        pm.set_mode("research")
        self.assertNotIn(self._dispatch(pm), pm.allowed_tools)
        self.assertNotIn("Start a dev task", pm.system_prompt)

    def test_switching_back_to_build_restores_dispatch(self) -> None:
        pm = self._agent(mode="research")
        pm.set_mode("build")
        self.assertIn(self._dispatch(pm), pm.allowed_tools)
        self.assertIn("Start a dev task", pm.system_prompt)

    def test_mode_does_not_disturb_the_rest_of_the_scope(self) -> None:
        """Mode is orthogonal to authority: a research PM on a board keeps its roadmap
        write grant - it plans and files work, it just can't dispatch it."""
        from pm_studio import roadmap as roadmap_module

        orig = roadmap_module.PRODUCTS
        roadmap_module.PRODUCTS = {"billing": "Billing"}
        try:
            pm = self._agent(mode="research", product="billing")
            self.assertIn(
                f"Bash(curl -s -X PATCH {agent_module.ROADMAP_BASE_URL}/billing/*)",
                pm.allowed_tools,
            )
            self.assertNotIn(self._dispatch(pm), pm.allowed_tools)
        finally:
            roadmap_module.PRODUCTS = orig

    def test_the_other_sessions_block_stops_talking_about_dispatch(self) -> None:
        """The per-turn overlap warning is dispatch advice; a research PM gets the
        research version of it rather than an instruction it cannot follow."""
        research = self._agent(mode="research")
        build = self._agent(mode="build")
        context = "- session abc: building the billing API"
        self.assertNotIn(
            "before dispatching", research._with_session_context("x", context)
        )
        self.assertIn("before dispatching", build._with_session_context("x", context))
        # Both still carry the snapshot itself.
        self.assertIn(context, research._with_session_context("x", context))


if __name__ == "__main__":
    unittest.main()
