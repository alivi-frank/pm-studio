"""Tests for per-system git workflow rules and the independent compliance judge.

A system may declare `gitflow` - a repo-root-relative file of NON-NEGOTIABLE git
workflow rules. Three promises hang off that one key, and they are what these tests
pin down:

1. **Delivery is guaranteed.** Every dev task dispatched for the system carries the
   file's contents verbatim, appended LAST (so the rules win on conflict), read fresh
   from the dispatching worktree (so an edit applies to the very next task). A task
   whose rules cannot be read is refused before any agent spend - running without
   declared non-negotiables is the one silent failure this layer exists to prevent.
2. **Attribution is enforced where delivery depends on it.** Once [systems] is
   declared, a dispatch must name a valid system - the same required/refused/listed
   contract roadmap creates already have - because the system id is what routes the
   rules. With no [systems], dispatch behaves byte-for-byte as before.
3. **The verdict is independent and never silently absent.** The judge inspects the
   task's exact commit range, cannot write, and lands ON the completion record before
   subscribers are notified - so the PM's auto-continue turn already knows. Unjudgeable
   states (no range) are visible "inconclusive" verdicts, never quiet passes; states
   with nothing to judge (no rules, no change) carry no verdict at all.
"""

import json
import subprocess
import tempfile
import textwrap
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

from pm_studio import agent as agent_module
from pm_studio import judge as judge_module
from pm_studio import roadmap as roadmap_module
from pm_studio import tasks as tasks_module
from pm_studio.config import SystemSpec, load_config
from pm_studio.tasks import TaskRegistry, validate_dispatch_system


class GitflowConfigTest(unittest.TestCase):
    """The `gitflow` key on [systems.<id>] - the operator-facing contract."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "pm_studio_local").mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write(self, config: str):
        (self.root / "pm_studio_local" / "config.toml").write_text(textwrap.dedent(config))
        return load_config(self.root)

    def test_gitflow_is_parsed_when_the_file_exists(self) -> None:
        (self.root / "docs").mkdir()
        (self.root / "docs" / "GITFLOW.md").write_text("branch from develop")
        cfg = self._write("""\
            [systems.claims]
            label = "Claims Processor"
            gitflow = "docs/GITFLOW.md"
        """)
        self.assertEqual(cfg.systems["claims"].gitflow, "docs/GITFLOW.md")

    def test_gitflow_pointing_at_nothing_is_fatal(self) -> None:
        """A path typo would silently mean "no rules injected" on every task for the
        system - the opposite of what the operator declared. Refuse to start."""
        with self.assertRaises(SystemExit):
            self._write("""\
                [systems.claims]
                label = "Claims Processor"
                gitflow = "docs/DOES_NOT_EXIST.md"
            """)

    def test_absent_gitflow_defaults_empty(self) -> None:
        cfg = self._write("""\
            [systems]
            claims = "Claims Processor"
        """)
        self.assertEqual(cfg.systems["claims"].gitflow, "")


class DispatchValidationTest(unittest.TestCase):
    """The `system` field on POST /tasks - mirrors roadmap attribution rules."""

    def setUp(self) -> None:
        self._orig = roadmap_module.SYSTEMS

    def tearDown(self) -> None:
        roadmap_module.SYSTEMS = self._orig

    def test_undeclared_table_accepts_no_field(self) -> None:
        """The pre-system dispatch, byte-for-byte."""
        roadmap_module.SYSTEMS = {}
        self.assertIsNone(validate_dispatch_system(""))

    def test_undeclared_table_refuses_a_named_system(self) -> None:
        """Honest rather than quietly ignored - same call the roadmap store makes."""
        roadmap_module.SYSTEMS = {}
        self.assertIsNotNone(validate_dispatch_system("claims"))

    def test_declared_table_requires_a_system(self) -> None:
        roadmap_module.SYSTEMS = {"claims": SystemSpec(label="Claims")}
        error = validate_dispatch_system("")
        # The message has to name the valid ids, or the PM retries the same payload.
        self.assertIn("claims", error)

    def test_unknown_system_is_refused_naming_the_valid_ids(self) -> None:
        roadmap_module.SYSTEMS = {"claims": SystemSpec(label="Claims")}
        error = validate_dispatch_system("rides")
        self.assertIn("rides", error)
        self.assertIn("claims", error)

    def test_valid_system_passes(self) -> None:
        roadmap_module.SYSTEMS = {"claims": SystemSpec(label="Claims")}
        self.assertIsNone(validate_dispatch_system("claims"))


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    )
    return proc.stdout.strip()


def _init_repo(repo: Path, initial_commit: bool = True) -> None:
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    if initial_commit:
        (repo / "README.md").write_text("seed\n")
        # Real deployments gitignore workspace runtime state (task records, chat
        # history) - see gitsnapshot.snapshot's docstring. Without this, the task's
        # own JSON record would move HEAD and every task would look like a change.
        (repo / ".gitignore").write_text("ws/\npm_studio_local/\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "seed")


class _FakeClaude:
    """Intercepts the `claude` subprocess (dev agent) while every real git call made
    by the registry and gitsnapshot still runs - the shape a registry test needs.
    Optionally mutates the repo (`touch`) or runs an arbitrary effect, the way a real
    dev turn would. Construct BEFORE entering mock.patch so _real_run is the real one."""

    def __init__(self, repo: Path, touch: str | None = None, effect=None):
        self.repo = repo
        self.touch = touch
        self.effect = effect
        self.prompts: list[str] = []
        self._real_run = subprocess.run

    def __call__(self, cmd, **kwargs):
        if isinstance(cmd, (list, tuple)) and cmd and cmd[0] == "claude":
            self.prompts.append(cmd[list(cmd).index("-p") + 1])
            if self.touch:
                (self.repo / self.touch).write_text("changed\n")
            if self.effect:
                self.effect()
            return types.SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"result": "dev done", "is_error": False}),
                stderr="",
            )
        return self._real_run(cmd, **kwargs)


class GitflowInjectionTest(unittest.TestCase):
    """Delivery: the rules reach the dev agent's prompt, verbatim, last, or the task
    refuses to run."""

    RULES = "Branch from develop. PRs target develop. Conventional commits only."

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        _init_repo(self.repo)
        (self.repo / "GITFLOW.md").write_text(self.RULES)

        local = self.repo / "pm_studio_local"
        local.mkdir()
        (local / "config.toml").write_text("")
        (local / "DEV_INSTRUCTIONS.md").write_text("LOCAL-MARKER: run the linter.")
        self._orig_config = tasks_module.CONFIG
        tasks_module.CONFIG = load_config(self.repo)

        self._orig_systems = roadmap_module.SYSTEMS
        roadmap_module.SYSTEMS = {
            "claims": SystemSpec(label="Claims Processor", gitflow="GITFLOW.md")
        }
        self.registry = TaskRegistry(
            "sess", self.repo / "ws", self.repo, threading.Lock()
        )

    def tearDown(self) -> None:
        tasks_module.CONFIG = self._orig_config
        roadmap_module.SYSTEMS = self._orig_systems
        self._tmp.cleanup()

    def test_rules_are_appended_last_and_verbatim(self) -> None:
        fake = _FakeClaude(self.repo)
        with mock.patch("subprocess.run", fake):
            is_error, _, _ = self.registry._execute("Build the thing", "claims")
        self.assertFalse(is_error)
        prompt = fake.prompts[0]
        self.assertIn(self.RULES, prompt)
        self.assertIn("NON-NEGOTIABLE git workflow rules for the Claims Processor", prompt)
        # Order is the precedence story: description, then local instructions, then
        # the rules - last, so they win on conflict.
        self.assertLess(prompt.index("Build the thing"), prompt.index("LOCAL-MARKER"))
        self.assertLess(prompt.index("LOCAL-MARKER"), prompt.index(self.RULES))

    def test_no_system_means_no_injection(self) -> None:
        fake = _FakeClaude(self.repo)
        with mock.patch("subprocess.run", fake):
            self.registry._execute("Build the thing")
        self.assertNotIn("NON-NEGOTIABLE", fake.prompts[0])

    def test_unreadable_rules_refuse_the_task_before_any_agent_spend(self) -> None:
        roadmap_module.SYSTEMS = {
            "claims": SystemSpec(label="Claims Processor", gitflow="MISSING.md")
        }
        fake = _FakeClaude(self.repo)
        with mock.patch("subprocess.run", fake):
            is_error, result, _ = self.registry._execute("Build the thing", "claims")
        self.assertTrue(is_error)
        self.assertIn("MISSING.md", result)
        self.assertEqual(fake.prompts, [])  # the dev agent never ran

    def test_rules_are_read_fresh_from_the_worktree_at_dispatch(self) -> None:
        (self.repo / "GITFLOW.md").write_text("EDITED RULES")
        fake = _FakeClaude(self.repo)
        with mock.patch("subprocess.run", fake):
            self.registry._execute("Build the thing", "claims")
        self.assertIn("EDITED RULES", fake.prompts[0])
        self.assertNotIn(self.RULES, fake.prompts[0])


class JudgeWiringTest(unittest.TestCase):
    """The verdict: produced for exactly the right tasks, from the task's own commit
    range, and already on the record when subscribers hear "done"."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        _init_repo(self.repo)
        # Committed, not just written: an untracked rules file would be swept up by
        # the task's own snapshot and make every task look like a change.
        (self.repo / "GITFLOW.md").write_text("rules")
        _git(self.repo, "add", "GITFLOW.md")
        _git(self.repo, "commit", "-q", "-m", "rules")

        local = self.repo / "pm_studio_local"
        local.mkdir()
        (local / "config.toml").write_text("")
        self._orig_config = tasks_module.CONFIG
        tasks_module.CONFIG = load_config(self.repo)

        self._orig_systems = roadmap_module.SYSTEMS
        roadmap_module.SYSTEMS = {
            "claims": SystemSpec(label="Claims Processor", gitflow="GITFLOW.md")
        }
        self.registry = TaskRegistry(
            "sess", self.repo / "ws", self.repo, threading.Lock()
        )
        self.judge_calls: list[dict] = []
        self._orig_run_judge = judge_module.run_judge

        def fake_run_judge(**kwargs):
            self.judge_calls.append(kwargs)
            return {
                "verdict": "violation",
                "violations": [{"rule": "PRs target develop", "evidence": "abc123 on main"}],
                "summary": "Committed straight to main.",
                "model": "sonnet",
                "agent_usage": {},
            }

        judge_module.run_judge = fake_run_judge

    def tearDown(self) -> None:
        judge_module.run_judge = self._orig_run_judge
        tasks_module.CONFIG = self._orig_config
        roadmap_module.SYSTEMS = self._orig_systems
        self._tmp.cleanup()

    def _run(self, system: str, touch: str | None):
        """One synchronous task run (the thread body directly, for determinism)."""
        with self.registry.git_lock:
            head_before = self.registry._git_head()
        fake = _FakeClaude(self.repo, touch=touch)
        with mock.patch("subprocess.run", fake):
            self.registry._run_and_record("t1", "Do the work", 1.0, system, head_before)
        return self.registry.get_task("t1")

    def test_changed_work_is_judged_over_its_exact_range(self) -> None:
        task = self._run("claims", touch="new_file.txt")
        self.assertEqual(task["judge"]["verdict"], "violation")
        (call,) = self.judge_calls
        self.assertEqual(call["head_before"], task["head_before"])
        self.assertEqual(call["head_after"], task["head_after"])
        self.assertNotEqual(task["head_before"], task["head_after"])
        self.assertEqual(call["rules"], "rules")

    def test_verdict_is_on_the_record_before_subscribers_hear_done(self) -> None:
        """The "done" notification triggers the PM's auto-continue turn - a verdict
        arriving after it would let the PM build on unjudged work."""
        seen: list[dict] = []
        self.registry.subscribe(lambda t: seen.append(dict(t)))
        self._run("claims", touch="new_file.txt")
        done_events = [t for t in seen if t.get("status") == "done"]
        self.assertTrue(done_events)
        self.assertIn("judge", done_events[0])

    def test_a_task_that_changed_nothing_is_not_judged(self) -> None:
        task = self._run("claims", touch=None)
        self.assertNotIn("judge", task)
        self.assertEqual(self.judge_calls, [])

    def test_a_system_without_rules_is_not_judged(self) -> None:
        roadmap_module.SYSTEMS = {"claims": SystemSpec(label="Claims Processor")}
        task = self._run("claims", touch="new_file.txt")
        self.assertNotIn("judge", task)

    def test_an_unknown_range_is_inconclusive_not_skipped(self) -> None:
        """Rules declared but no commit range to verify against: visible, never a
        quiet pass."""
        with mock.patch.object(self.registry, "_git_head", return_value=""):
            fake = _FakeClaude(self.repo, touch="new_file.txt")
            with mock.patch("subprocess.run", fake):
                self.registry._run_and_record("t2", "Do the work", 1.0, "claims", "")
        task = self.registry.get_task("t2")
        self.assertEqual(task["judge"]["verdict"], "inconclusive")
        self.assertEqual(self.judge_calls, [])

    def test_merge_resolution_is_never_judged(self) -> None:
        fake = _FakeClaude(self.repo, touch="conflict.txt")
        with mock.patch("subprocess.run", fake):
            task = self.registry.run_conflict_resolution("fix the merge")
        self.assertNotIn("judge", task)
        self.assertNotIn("NON-NEGOTIABLE", fake.prompts[0])


class NestedRepoJudgeTest(unittest.TestCase):
    """Systems whose code lives in their OWN repositories inside the checkout (nested
    clones, tracked as gitlinks): the rules apply where the work happens, so the judge
    must run inside those repos - a commit range when the agent committed, the dirty
    tree when it didn't (the deployment snapshot can't sweep nested work into a
    commit, so uncommitted nested work would otherwise be invisible)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        _init_repo(self.repo)
        (self.repo / "GITFLOW.md").write_text("rules")
        _git(self.repo, "add", "GITFLOW.md")
        _git(self.repo, "commit", "-q", "-m", "rules")
        # The registry convention: one system folder holding sibling clones.
        self.backend = self.repo / "src" / "epic" / "backend"
        self.frontend = self.repo / "src" / "epic" / "frontend"
        for child in (self.backend, self.frontend):
            child.mkdir(parents=True)
            _init_repo(child)
        # Commit the gitlink entries at seed (as the real deployment has them) - an
        # untracked child would be swept into the first task's snapshot, moving root
        # HEAD and making an idle task look like root work.
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "-m", "children")

        local = self.repo / "pm_studio_local"
        local.mkdir()
        (local / "config.toml").write_text("")
        self._orig_config = tasks_module.CONFIG
        tasks_module.CONFIG = load_config(self.repo)

        self._orig_systems = roadmap_module.SYSTEMS
        roadmap_module.SYSTEMS = {
            "epic": SystemSpec(label="EpicRide", path="src/epic", gitflow="GITFLOW.md")
        }
        self.registry = TaskRegistry(
            "sess", self.repo / "ws", self.repo, threading.Lock()
        )
        self.judge_calls: list[dict] = []
        self.responses: dict[str, dict] = {}
        self._orig_run_judge = judge_module.run_judge

        def fake_run_judge(**kwargs):
            self.judge_calls.append(kwargs)
            return self.responses.get(kwargs.get("repo_rel"), {
                "verdict": "pass", "violations": [], "summary": "ok",
                "model": "m", "agent_usage": {"cost_usd": 1.0},
            })

        judge_module.run_judge = fake_run_judge

    def tearDown(self) -> None:
        judge_module.run_judge = self._orig_run_judge
        tasks_module.CONFIG = self._orig_config
        roadmap_module.SYSTEMS = self._orig_systems
        self._tmp.cleanup()

    def _run(self, effect=None):
        rels = self.registry._system_repo_dirs("epic")
        with self.registry.git_lock:
            head_before = self.registry._git_head()
            nested_before = self.registry._repo_states(rels)
        fake = _FakeClaude(self.repo, effect=effect)
        with mock.patch("subprocess.run", fake):
            self.registry._run_and_record(
                "t1", "Do the work", 1.0, "epic", head_before, nested_before
            )
        return self.registry.get_task("t1")

    def _commit_in(self, child: Path, name: str) -> None:
        (child / name).write_text("changed\n")
        _git(child, "add", "-A")
        _git(child, "commit", "-q", "-m", f"feat: {name}")

    def test_discovery_finds_the_sibling_clones(self) -> None:
        self.assertEqual(
            self.registry._system_repo_dirs("epic"),
            ["src/epic/backend", "src/epic/frontend"],
        )

    def test_discovery_of_a_path_that_is_itself_a_repo(self) -> None:
        roadmap_module.SYSTEMS = {
            "epic": SystemSpec(label="E", path="src/epic/backend", gitflow="GITFLOW.md")
        }
        self.assertEqual(self.registry._system_repo_dirs("epic"), ["src/epic/backend"])

    def test_discovery_is_empty_without_a_path_or_repos(self) -> None:
        roadmap_module.SYSTEMS = {"epic": SystemSpec(label="E", gitflow="GITFLOW.md")}
        self.assertEqual(self.registry._system_repo_dirs("epic"), [])

    def test_a_commit_in_a_child_repo_is_judged_inside_that_repo(self) -> None:
        task = self._run(effect=lambda: self._commit_in(self.backend, "svc.ts"))
        (call,) = self.judge_calls
        self.assertEqual(call["repo_root"], self.backend)
        self.assertEqual(call["repo_rel"], "src/epic/backend")
        self.assertFalse(call["uncommitted"])
        before = task["system_repos_before"]["src/epic/backend"]
        after = task["system_repos_after"]["src/epic/backend"]
        self.assertEqual(call["head_before"], before["head"])
        self.assertEqual(call["head_after"], after["head"])
        self.assertNotEqual(before["head"], after["head"])
        self.assertEqual(task["judge"]["verdict"], "pass")

    def test_the_deployment_repo_is_not_judged_alongside_a_child(self) -> None:
        """The root's only change is the registry's own gitlink-bump snapshot -
        judging bookkeeping against the system's branching rules would manufacture
        false violations."""
        self._run(effect=lambda: self._commit_in(self.backend, "svc.ts"))
        self.assertEqual(len(self.judge_calls), 1)
        self.assertEqual(self.judge_calls[0]["repo_rel"], "src/epic/backend")

    def test_uncommitted_child_work_is_judged_as_a_dirty_tree(self) -> None:
        """The deployment snapshot cannot commit work inside a nested repo, so an
        agent that edits without committing would otherwise leave NO trace - and
        'commit your own work' is exactly the kind of rule it just broke."""
        task = self._run(
            effect=lambda: (self.backend / "svc.ts").write_text("uncommitted\n")
        )
        (call,) = self.judge_calls
        self.assertEqual(call["repo_rel"], "src/epic/backend")
        self.assertTrue(call["uncommitted"])
        self.assertEqual(call["head_before"], call["head_after"])
        self.assertIn("judge", task)

    def test_work_in_both_children_folds_worst_wins(self) -> None:
        self.responses["src/epic/frontend"] = {
            "verdict": "violation",
            "violations": [{"rule": "no main commits", "evidence": "abc on main"}],
            "summary": "committed to main", "model": "m", "agent_usage": {"cost_usd": 2.0},
        }
        task = self._run(effect=lambda: (
            self._commit_in(self.backend, "svc.ts"),
            self._commit_in(self.frontend, "app.ts"),
        ))
        self.assertEqual(len(self.judge_calls), 2)
        verdict = task["judge"]
        self.assertEqual(verdict["verdict"], "violation")
        self.assertEqual(
            verdict["violations"][0]["evidence"], "[src/epic/frontend] abc on main"
        )
        # Both runs were real agent calls; the merged verdict carries both spends.
        self.assertEqual(verdict["agent_usage"]["cost_usd"], 3.0)

    def test_untouched_children_fall_back_to_the_deployment_repo(self) -> None:
        """A system with nested repos can still have work land at the root (docs,
        config); with no child touched, the root range stays the evidence."""
        task = self._run(effect=lambda: (self.repo / "notes.md").write_text("n\n"))
        (call,) = self.judge_calls
        self.assertEqual(call["repo_rel"], "")
        self.assertEqual(call["repo_root"], self.repo)
        self.assertEqual(call["head_before"], task["head_before"])
        self.assertEqual(call["head_after"], task["head_after"])

    def test_nothing_changed_anywhere_is_not_judged(self) -> None:
        task = self._run(effect=None)
        self.assertNotIn("judge", task)
        self.assertEqual(self.judge_calls, [])


class MergeVerdictsTest(unittest.TestCase):
    def _v(self, verdict: str, cost: float = 1.0) -> dict:
        return {
            "verdict": verdict,
            "violations": [{"rule": "r", "evidence": "e"}] if verdict == "violation" else [],
            "summary": f"{verdict} summary",
            "model": "m",
            "agent_usage": {"cost_usd": cost, "input_tokens": 10},
        }

    def test_single_verdict_passes_through_unchanged(self) -> None:
        verdict = self._v("pass")
        self.assertIs(judge_module.merge_verdicts([("a", verdict)]), verdict)

    def test_any_violation_wins(self) -> None:
        merged = judge_module.merge_verdicts(
            [("a", self._v("pass")), ("b", self._v("violation")), ("c", self._v("inconclusive"))]
        )
        self.assertEqual(merged["verdict"], "violation")
        self.assertEqual(merged["violations"], [{"rule": "r", "evidence": "[b] e"}])

    def test_inconclusive_beats_pass(self) -> None:
        merged = judge_module.merge_verdicts(
            [("a", self._v("pass")), ("b", self._v("inconclusive"))]
        )
        self.assertEqual(merged["verdict"], "inconclusive")

    def test_summaries_are_prefixed_and_spend_is_summed(self) -> None:
        merged = judge_module.merge_verdicts(
            [("a", self._v("pass", 1.5)), ("b", self._v("pass", 2.5))]
        )
        self.assertIn("a: pass summary", merged["summary"])
        self.assertIn("b: pass summary", merged["summary"])
        self.assertEqual(merged["agent_usage"]["cost_usd"], 4.0)
        self.assertEqual(merged["agent_usage"]["input_tokens"], 20)


class EvidenceBlockTest(unittest.TestCase):
    """The prompt the judge actually receives, per evidence shape."""

    def _prompt_for(self, **kwargs) -> str:
        captured = {}

        def fake_run(cmd, **_):
            captured["prompt"] = cmd[list(cmd).index("-p") + 1]
            result = json.dumps({"verdict": "pass", "violations": [], "summary": "ok"})
            return types.SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"result": result, "is_error": False}),
                stderr="",
            )

        with mock.patch.object(judge_module.subprocess, "run", fake_run):
            judge_module.run_judge(
                repo_root=Path("/tmp"), system_id="epic", system_label="EpicRide",
                gitflow_path="GITFLOW.md", rules="rules", description="task",
                head_before="aaa", head_after="bbb", fallback_model="m", **kwargs,
            )
        return captured["prompt"]

    def test_root_evidence_carries_the_snapshot_caveat(self) -> None:
        prompt = self._prompt_for()
        self.assertIn("Dev task", prompt)  # the bookkeeping-snapshot caveat
        self.assertIn("aaa..bbb", prompt)

    def test_nested_evidence_names_the_repo_and_drops_the_caveat(self) -> None:
        prompt = self._prompt_for(repo_rel="src/epic/backend")
        self.assertIn("src/epic/backend", prompt)
        self.assertIn("OWN repository", prompt)
        self.assertIn("aaa..bbb", prompt)
        # No snapshot commits exist inside a nested repo - the caveat would hand the
        # agent an exemption it could hide behind.
        self.assertNotIn("Dev task", prompt)

    def test_uncommitted_evidence_points_at_the_dirty_tree(self) -> None:
        prompt = self._prompt_for(repo_rel="src/epic/backend", uncommitted=True)
        self.assertIn("UNCOMMITTED", prompt)
        self.assertIn("git status", prompt)
        self.assertNotIn("aaa..bbb", prompt)


class JudgeParsingTest(unittest.TestCase):
    """parse_verdict: strict on shape, tolerant of exactly one deviation (a fence)."""

    def test_valid_pass(self) -> None:
        verdict = judge_module.parse_verdict(
            '{"verdict": "pass", "violations": [], "summary": "All good."}'
        )
        self.assertEqual(verdict["verdict"], "pass")
        self.assertEqual(verdict["violations"], [])

    def test_fenced_json_is_accepted(self) -> None:
        text = '```json\n{"verdict": "pass", "violations": [], "summary": "ok"}\n```'
        self.assertEqual(judge_module.parse_verdict(text)["verdict"], "pass")

    def test_prose_is_rejected(self) -> None:
        self.assertIsNone(judge_module.parse_verdict("The work looks compliant to me."))

    def test_unknown_verdict_is_rejected(self) -> None:
        self.assertIsNone(
            judge_module.parse_verdict('{"verdict": "fine", "violations": []}')
        )

    def test_violation_without_citations_is_rejected(self) -> None:
        """An uncited "violation" is unusable - the caller records inconclusive rather
        than trusting it or waving it through."""
        self.assertIsNone(
            judge_module.parse_verdict(
                '{"verdict": "violation", "violations": [], "summary": "bad"}'
            )
        )

    def test_violations_are_normalized(self) -> None:
        verdict = judge_module.parse_verdict(
            '{"verdict": "violation", "violations": '
            '[{"rule": " r ", "evidence": " e "}, "junk"], "summary": "s"}'
        )
        self.assertEqual(verdict["violations"], [{"rule": "r", "evidence": "e"}])

    def test_judge_model_defaults_to_the_opus_tier(self) -> None:
        """The verdict gets the strongest model the deployment offers, whichever way
        the [models] table spells it - never a cheaper model over spelling."""
        with mock.patch.object(judge_module, "MODELS", {"claude-opus-4-8": "Opus", "sonnet": "Sonnet"}):
            self.assertEqual(judge_module.judge_model("sonnet"), "claude-opus-4-8")
        models = {"claude-sonnet-5": "Sonnet", "claude-opus-5": "Opus"}
        with mock.patch.object(judge_module, "MODELS", models):
            self.assertEqual(judge_module.judge_model("claude-sonnet-5"), "claude-opus-5")

    def test_judge_model_falls_back_to_the_registry_model(self) -> None:
        """A deployment that declares no opus id judges on the dispatching session's
        own model rather than inventing an id the CLI may not accept."""
        with mock.patch.object(judge_module, "MODELS", {"sonnet": "Sonnet", "haiku": "Haiku"}):
            self.assertEqual(judge_module.judge_model("sonnet"), "sonnet")


class PromptSlotsTest(unittest.TestCase):
    """The PM-side halves: the dispatch slots and the completion-turn judge note."""

    def setUp(self) -> None:
        self._orig = agent_module.SYSTEMS

    def tearDown(self) -> None:
        agent_module.SYSTEMS = self._orig

    def test_no_systems_renders_empty_slots(self) -> None:
        """The byte-compat promise: an undeclared deployment's prompt is unchanged."""
        agent_module.SYSTEMS = {}
        self.assertEqual(agent_module._dispatch_system_slots(), ("", ""))

    def test_declared_systems_require_the_field_and_name_the_ids(self) -> None:
        agent_module.SYSTEMS = {"claims": SystemSpec(label="Claims")}
        field, note = agent_module._dispatch_system_slots()
        self.assertIn('"system"', field)
        self.assertIn("`claims`", note)
        # No system declares rules, so the prompt says nothing about a judge it
        # doesn't have - the same honesty rule the tracker guidance follows.
        self.assertNotIn("compliance judge", note)

    def test_gitflow_systems_are_marked_and_the_judge_is_explained(self) -> None:
        agent_module.SYSTEMS = {
            "claims": SystemSpec(label="Claims", gitflow="GITFLOW.md"),
            "rides": SystemSpec(label="Rides"),
        }
        _, note = agent_module._dispatch_system_slots()
        self.assertIn("`claims` [git rules]", note)
        self.assertIn("`rides`,", note.replace("`rides`.", "`rides`,"))
        self.assertNotIn("`rides` [git rules]", note)
        self.assertIn("compliance judge", note)
        self.assertIn("never restate", note)

    def test_completion_note_for_each_verdict(self) -> None:
        self.assertEqual(agent_module._judge_completion_note({}, at_cap=False), "")
        passed = {"judge": {"verdict": "pass", "violations": [], "summary": ""}}
        self.assertIn("compliant", agent_module._judge_completion_note(passed, False))
        inconclusive = {"judge": {"verdict": "inconclusive", "violations": [], "summary": "no range"}}
        note = agent_module._judge_completion_note(inconclusive, False)
        self.assertIn("could not verify", note)
        self.assertIn("no range", note)

    def test_violation_note_cites_and_directs_remediation(self) -> None:
        task = {
            "judge": {
                "verdict": "violation",
                "violations": [{"rule": "PRs target develop", "evidence": "abc123 on main"}],
                "summary": "s",
            }
        }
        note = agent_module._judge_completion_note(task, at_cap=False)
        self.assertIn("PRs target develop", note)
        self.assertIn("abc123 on main", note)
        self.assertIn("remediation dev task", note)
        # At the auto-continue cap the PM must NOT be told to dispatch - the cap
        # exists precisely to stop a stuck loop from dispatching forever.
        capped = agent_module._judge_completion_note(task, at_cap=True)
        self.assertIn("cannot dispatch", capped)
        self.assertNotIn("dispatch a remediation", capped)


if __name__ == "__main__":
    unittest.main()
