"""Tests for the guarantee that credential-bearing state never reaches a commit.

Every PM turn and dev task ends in a repo-wide `git add -A` snapshot. Before these
tests, the only thing keeping a password hash out of a pushed commit was the operator
happening to have the right lines in .gitignore - and a deployment that ran `init`
before accounts.json existed had no such line. So the protection is now in the snapshot
itself, and this file pins it.
"""

import dataclasses
import subprocess
import tempfile
import unittest
from pathlib import Path

from pm_studio import gitsnapshot
from pm_studio.gitsnapshot import (
    SENSITIVE_WORKSPACE_FILES,
    sensitive_pathspecs,
)
from pm_studio.scaffold import GITIGNORE_ENTRIES, missing_gitignore_entries

WORKSPACE_REL = "pm_studio/workspace"


def git(*args, cwd):
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=False
    )


class SnapshotNeverCommitsSecretsTest(unittest.TestCase):
    """The core guarantee, exercised against a real git repo with NO .gitignore at all -
    which is the worst case and exactly the upgraded-deployment scenario."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        git("init", "-q", "-b", "main", cwd=self.root)
        git("config", "user.email", "t@e.co", cwd=self.root)
        git("config", "user.name", "T", cwd=self.root)
        (self.root / "README.md").write_text("seed\n")
        git("add", "-A", cwd=self.root)
        git("commit", "-qm", "seed", cwd=self.root)

        self.workspace = self.root / WORKSPACE_REL
        self.workspace.mkdir(parents=True)
        # snapshot() derives its pathspecs from the live CONFIG, and this repo's own
        # deployment uses a different workspace_root - so point it at the temp repo's.
        self._orig_config = gitsnapshot.CONFIG
        gitsnapshot.CONFIG = dataclasses.replace(
            gitsnapshot.CONFIG, workspace_root="pm_studio"
        )

    def tearDown(self) -> None:
        gitsnapshot.CONFIG = self._orig_config
        self._tmp.cleanup()

    def _write_secrets(self) -> None:
        (self.workspace / "accounts.json").write_text(
            '{"users": [{"password_hash": "deadbeef", "password_salt": "cafe"}]}'
        )
        (self.workspace / "costing.json").write_text('{"roster": [{"rate_per_hour": 250}]}')
        (self.workspace / "audit.jsonl").write_text('{"actor_email": "a@b.co"}\n')
        (self.workspace / "activity.jsonl").write_text('{"user_id": "u1"}\n')
        (self.workspace / "accounts.json.tmp").write_text('{"users": []}')

    def _tracked(self) -> list[str]:
        return git("ls-files", cwd=self.root).stdout.split()

    def test_no_gitignore_at_all_and_secrets_still_are_not_committed(self) -> None:
        self._write_secrets()
        (self.root / "product.py").write_text("print('real work')\n")

        gitsnapshot.snapshot("a PM turn", self.root)

        tracked = self._tracked()
        # The actual product change still lands - the guard must not break snapshots.
        self.assertIn("product.py", tracked)
        for name in SENSITIVE_WORKSPACE_FILES:
            self.assertNotIn(f"{WORKSPACE_REL}/{name}", tracked, name)
        self.assertNotIn(f"{WORKSPACE_REL}/accounts.json.tmp", tracked)

    def test_no_hash_or_rate_appears_anywhere_in_history(self) -> None:
        """The end-to-end assertion an auditor would actually make."""
        self._write_secrets()
        (self.root / "product.py").write_text("print('real work')\n")
        gitsnapshot.snapshot("a PM turn", self.root)

        blob = git("log", "-p", "--all", cwd=self.root).stdout
        self.assertNotIn("deadbeef", blob)
        self.assertNotIn("password_hash", blob)
        self.assertNotIn("rate_per_hour", blob)

    def test_a_snapshot_with_only_secrets_changed_commits_nothing(self) -> None:
        """After unstaging there is nothing left, so no empty commit should be made."""
        before = git("rev-parse", "HEAD", cwd=self.root).stdout.strip()
        self._write_secrets()
        gitsnapshot.snapshot("only secrets changed", self.root)
        self.assertEqual(git("rev-parse", "HEAD", cwd=self.root).stdout.strip(), before)

    def test_ordinary_workspace_state_is_untouched_by_the_guard(self) -> None:
        """The guard is narrow: it must not start dropping the spec, which is meant to be
        committed."""
        (self.workspace / "current").mkdir()
        (self.workspace / "current" / "SPEC.md").write_text("# the spec\n")
        gitsnapshot.snapshot("spec update", self.root)
        self.assertIn(f"{WORKSPACE_REL}/current/SPEC.md", self._tracked())

    def test_already_tracked_secret_is_reported_loudly(self) -> None:
        """Unstaging cannot fix a file that is already in history, so the operator has to
        be told, with the command to run."""
        self._write_secrets()
        git("add", "-f", f"{WORKSPACE_REL}/accounts.json", cwd=self.root)
        git("commit", "-qm", "oops, leaked", cwd=self.root)

        with _capture_stderr() as err:
            gitsnapshot.snapshot("next turn", self.root)
        message = err.getvalue()
        self.assertIn("credential-bearing state is tracked in git", message)
        self.assertIn("git rm --cached", message)
        self.assertIn("accounts.json", message)

    def test_pathspecs_cover_every_sensitive_file_plus_tmp(self) -> None:
        specs = sensitive_pathspecs(WORKSPACE_REL)
        for name in SENSITIVE_WORKSPACE_FILES:
            self.assertIn(f"{WORKSPACE_REL}/{name}", specs)
        self.assertIn(f"{WORKSPACE_REL}/*.tmp", specs)


class GitignoreEntriesTest(unittest.TestCase):
    def test_every_sensitive_file_is_also_listed_for_gitignore(self) -> None:
        """Defence in depth: the snapshot guard is primary, but the ignore block must not
        silently fall behind it either."""
        entries = " ".join(GITIGNORE_ENTRIES)
        for name in SENSITIVE_WORKSPACE_FILES:
            self.assertIn(name, entries, name)

    def test_missing_entries_detected_in_a_pre_existing_gitignore(self) -> None:
        """The old bug: an .gitignore written before accounts.json existed mentioned the
        workspace, so `init` skipped it entirely and reported nothing to do."""
        legacy = "\n".join(
            [
                "__pycache__/",
                f"{WORKSPACE_REL}/sessions/",
                f"{WORKSPACE_REL}/sessions.json",
                f"{WORKSPACE_REL}/roadmap/",
            ]
        )
        missing = missing_gitignore_entries(legacy, WORKSPACE_REL)
        self.assertIn(f"{WORKSPACE_REL}/accounts.json", missing)
        self.assertIn(f"{WORKSPACE_REL}/costing.json", missing)
        # And it must not re-add what is already there.
        self.assertNotIn(f"{WORKSPACE_REL}/sessions/", missing)

    def test_a_complete_gitignore_needs_nothing(self) -> None:
        complete = "\n".join(
            entry.format(workspace_rel=WORKSPACE_REL) for entry in GITIGNORE_ENTRIES
        )
        self.assertEqual(missing_gitignore_entries(complete, WORKSPACE_REL), [])

    def test_substring_matches_do_not_count_as_covered(self) -> None:
        """`other/pm_studio/workspace/accounts.json` must not satisfy the requirement for
        `pm_studio/workspace/accounts.json`."""
        sneaky = f"somewhere/else/{WORKSPACE_REL}/accounts.json"
        missing = missing_gitignore_entries(sneaky, WORKSPACE_REL)
        self.assertIn(f"{WORKSPACE_REL}/accounts.json", missing)


class _capture_stderr:
    """Minimal stderr capture - the module writes warnings with print(file=sys.stderr)."""

    def __enter__(self):
        import io
        import sys

        self._orig = sys.stderr
        self._buf = io.StringIO()
        sys.stderr = self._buf
        return self._buf

    def __exit__(self, *exc):
        import sys

        sys.stderr = self._orig
        return False


if __name__ == "__main__":
    unittest.main()
