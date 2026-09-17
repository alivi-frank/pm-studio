"""Tests for the git source: real repositories in a temp dir, no mocking of git."""

import subprocess
import tempfile
import unittest
from pathlib import Path

from pm_studio.signals.model import KIND_COMMIT, KIND_MERGE
from pm_studio.signals.sources.git import GitSource, discover_repos


def run(args, cwd):
    subprocess.run(args, cwd=str(cwd), check=True, capture_output=True)


def make_repo(path: Path, author=("Ada Lovelace", "ada@example.com")) -> None:
    path.mkdir(parents=True)
    run(["git", "init", "-q", "-b", "main"], path)
    run(["git", "config", "user.name", author[0]], path)
    run(["git", "config", "user.email", author[1]], path)
    run(["git", "config", "commit.gpgsign", "false"], path)


def commit(path: Path, message: str, filename="f.txt", content="x\n") -> None:
    (path / filename).write_text((path / filename).read_text() + content if (path / filename).exists() else content)
    run(["git", "add", "-A"], path)
    run(["git", "commit", "-q", "-m", message], path)


class GitSourceTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.repo = self.root / "src" / "svc" / "backend"
        make_repo(self.repo)
        commit(self.repo, "feat(NDT-100): first\n\nCo-Authored-By: Claude <noreply@anthropic.com>")
        commit(self.repo, "chore: no ticket here", filename="g.txt")
        run(["git", "checkout", "-q", "-b", "feature/NDT-101-thing"], self.repo)
        commit(self.repo, "fix: ticket #1234 on branch", filename="h.txt")
        run(["git", "checkout", "-q", "main"], self.repo)
        run(["git", "merge", "-q", "--no-ff", "-m", "Merge branch 'feature/NDT-101-thing'", "feature/NDT-101-thing"], self.repo)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_discovery_finds_nested_checkouts(self) -> None:
        self.assertEqual(discover_repos(self.root, ["src/svc"]), ["src/svc/backend"])
        self.assertEqual(discover_repos(self.root, ["src/svc/backend"]), ["src/svc/backend"])
        self.assertEqual(discover_repos(self.root, ["nope"]), [])

    def test_commits_become_signals_with_refs(self) -> None:
        source = GitSource(self.root, {"svc": "src/svc"}, jira_projects={"NDT"}, ado_enabled=True)
        result = source.collect(0.0)
        by_subject = {s.meta["subject"]: s for s in result.signals}
        first = by_subject["feat(NDT-100): first"]
        self.assertEqual(first.kind, KIND_COMMIT)
        self.assertEqual(first.refs, ["jira:NDT-100"])
        self.assertTrue(first.meta["ai"])
        self.assertEqual(first.system, "svc")
        self.assertEqual(first.repo, "src/svc/backend")
        self.assertEqual(first.actor_email, "ada@example.com")
        self.assertEqual(first.meta["files"], 1)
        self.assertEqual(by_subject["chore: no ticket here"].refs, [])
        self.assertEqual(by_subject["fix: ticket #1234 on branch"].refs, ["ado:1234"])
        merge = by_subject["Merge branch 'feature/NDT-101-thing'"]
        self.assertEqual(merge.kind, KIND_MERGE)
        self.assertEqual(merge.refs, ["jira:NDT-101"])
        self.assertEqual(merge.meta["branch"], "feature/NDT-101-thing")
        facts = result.facts["repos"]["src/svc/backend"]
        self.assertEqual(facts["commits"], 4)
        self.assertEqual(facts["keyed_commits"], 2)

    def test_since_bounds_the_scan(self) -> None:
        source = GitSource(self.root, {"svc": "src/svc"})
        far_future = 4_000_000_000.0
        self.assertEqual(source.collect(far_future).signals, [])

    def test_fetch_runs_first_and_a_failed_fetch_is_a_note(self) -> None:
        calls = []
        real = GitSource._default_runner
        def runner(args, cwd):
            calls.append(args[:2])
            if args[:2] == ["git", "fetch"]:
                raise RuntimeError("could not read Username")
            return real(args, cwd)
        source = GitSource(self.root, {"svc": "src/svc"}, runner=runner, fetch=True)
        result = source.collect(0.0)
        self.assertEqual(calls[0], ["git", "fetch"])
        self.assertEqual(len(result.signals), 4)  # the scan still ran on local refs
        self.assertIn("fetch failed", result.notes[0])
        self.assertEqual(result.facts["fetched"], 0)
        self.assertEqual(result.facts["repos"]["src/svc/backend"]["fetch_error"], "could not read Username")
        off = GitSource(self.root, {"svc": "src/svc"}, runner=runner, fetch=False)
        calls.clear(); off.collect(0.0)
        self.assertNotIn(["git", "fetch"], calls)

    def test_broken_repo_is_a_note_not_a_crash(self) -> None:
        def runner(args, cwd):
            raise RuntimeError("boom")
        source = GitSource(self.root, {"svc": "src/svc"}, runner=runner)
        result = source.collect(0.0)
        self.assertEqual(result.signals, [])
        self.assertIn("boom", result.notes[0])
