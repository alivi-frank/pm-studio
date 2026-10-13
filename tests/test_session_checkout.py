"""Tests for session_checkout: product-code submodules materialized inside a session
worktree as linked worktrees of the primary checkout's submodule repositories.

Every test builds a real throwaway superproject with a real submodule (git is the thing
under test - mocking it would prove nothing), plus a session worktree made exactly the
way SessionManager.create makes one.
"""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pm_studio import session_checkout as sc
from pm_studio import sessions as sessions_module
from pm_studio.session_checkout import (
    KEPT_PREFIX,
    CheckoutError,
    ensure_system_checkout,
    release_system_checkouts,
)

_HAS_GIT = shutil.which("git") is not None
_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
    "GIT_CONFIG_NOSYSTEM": "1",
}


def git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-c", "protocol.file.allow=always", "-c", "core.autocrlf=false", *args],
        cwd=cwd, env=_ENV, capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {proc.stderr}")
    return proc.stdout.strip()


def write(path: Path, text: str = "x\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def is_dir_link(path: Path) -> bool:
    return os.path.islink(path) or (hasattr(os.path, "isjunction") and os.path.isjunction(path))


@unittest.skipUnless(_HAS_GIT, "git is not installed")
class SessionCheckoutTest(unittest.TestCase):
    SUB = "src/sys/app"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name).resolve()

        # the product repository ("remote" of the submodule)
        self.origin = self.base / "app-origin"
        self.origin.mkdir()
        git(self.origin, "init", "-q", "-b", "main")
        write(self.origin / "app.txt", "v1\n")
        write(self.origin / ".gitignore", "node_modules/\n")
        write(self.origin / "lib" / "a" / "index.js")
        git(self.origin, "add", "-A")
        git(self.origin, "commit", "-qm", "v1")
        self.pinned = git(self.origin, "rev-parse", "HEAD")

        # the deployment superproject (primary checkout)
        self.root = self.base / "deploy"
        self.root.mkdir()
        git(self.root, "init", "-q", "-b", "main")
        write(self.root / "README.md")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-qm", "init")
        git(self.root, "submodule", "add", "-q", str(self.origin), self.SUB)
        git(self.root, "commit", "-qm", "add submodule")

        # the primary submodule moves past the pinned commit: a session must still get the pin
        primary_sub = self.root / self.SUB
        write(primary_sub / "app.txt", "v2\n")
        git(primary_sub, "commit", "-qam", "v2")
        self.primary_head = git(primary_sub, "rev-parse", "HEAD")

        # heavy dependency dirs that only exist in the primary checkout
        write(primary_sub / "node_modules" / "pkg" / "index.js")
        write(primary_sub / "node_modules" / "pkg" / "node_modules" / "deep" / "x.js")
        write(primary_sub / "lib" / "a" / "node_modules" / "dep" / "y.js")

        # the session worktree, exactly as SessionManager.create makes it
        self.wt = self.base / "sessions" / "s1"
        git(self.root, "worktree", "add", "-q", "-b", "session/s1", str(self.wt), "main")

    def tearDown(self) -> None:
        # never let rmtree walk through a junction into the primary checkout
        try:
            release_system_checkouts(self.root, self.wt, force=True)
        except Exception:
            pass
        self._tmp.cleanup()

    def session_status(self) -> str:
        return git(self.wt, "status", "--porcelain", "--ignore-submodules=none")

    def primary_worktrees(self) -> list[str]:
        out = git(self.root / self.SUB, "worktree", "list", "--porcelain")
        return [l for l in out.splitlines() if l.startswith("worktree ")]

    # ---- ensure

    def test_fresh_session_worktree_has_empty_gitlink_dir(self) -> None:
        self.assertTrue((self.wt / self.SUB).is_dir())
        self.assertEqual(list((self.wt / self.SUB).iterdir()), [])

    def test_populates_submodule_at_pinned_commit(self) -> None:
        notes = ensure_system_checkout(self.root, self.wt, "src/sys")
        checkout = self.wt / self.SUB
        self.assertEqual((checkout / "app.txt").read_text(encoding="utf-8"), "v1\n")
        self.assertEqual(git(checkout, "rev-parse", "HEAD"), self.pinned)
        self.assertEqual(Path(git(checkout, "rev-parse", "--show-toplevel")).resolve(), checkout.resolve())
        self.assertTrue((checkout / ".git").is_file())  # a linked worktree, not a clone
        self.assertEqual(self.session_status(), "")  # the superproject sees a clean submodule
        self.assertTrue(any(self.SUB in n and self.pinned[:12] in n for n in notes), notes)
        # the primary's own checkout is untouched
        self.assertEqual(git(self.root / self.SUB, "rev-parse", "HEAD"), self.primary_head)

    def test_session_moving_the_submodule_shows_as_modified(self) -> None:
        ensure_system_checkout(self.root, self.wt, "src/sys")
        checkout = self.wt / self.SUB
        git(checkout, "switch", "-q", "-c", "feature/x")
        write(checkout / "new.txt")
        git(checkout, "add", "-A")
        git(checkout, "commit", "-qm", "feature")
        self.assertIn(self.SUB, self.session_status())
        # and the commit is immediately visible from the primary repository (shared objects/refs)
        self.assertIn("feature/x", git(self.root / self.SUB, "branch", "--list", "feature/x"))

    def test_exact_system_path_and_idempotency(self) -> None:
        ensure_system_checkout(self.root, self.wt, self.SUB)
        before = self.primary_worktrees()
        notes = ensure_system_checkout(self.root, self.wt, self.SUB)
        self.assertEqual(self.primary_worktrees(), before)
        self.assertEqual(len(before), 2)
        self.assertEqual(git(self.wt / self.SUB, "rev-parse", "HEAD"), self.pinned)
        self.assertFalse(any("checked out" in n for n in notes), notes)

    def test_path_without_submodules_is_nothing_to_do(self) -> None:
        """A system whose code is plain superproject files: the worktree already has
        it, so nothing is checked out - but the links are still made."""
        write(self.root / "src" / "plain" / "node_modules" / "pkg" / "index.js")
        write(self.wt / "src" / "plain" / "app.js")  # the system's (committed) code
        notes = ensure_system_checkout(
            self.root, self.wt, "src/plain", link_paths=("src/plain/node_modules",),
        )
        self.assertTrue(any("no submodules under 'src/plain'" in n for n in notes), notes)
        self.assertTrue(is_dir_link(self.wt / "src" / "plain" / "node_modules"))
        self.assertEqual(len(self.primary_worktrees()), 1)  # no submodule worktree added
        self.assertFalse((self.wt / self.SUB / ".git").exists())

    def test_empty_system_path_in_a_repo_without_submodules(self) -> None:
        bare = self.base / "plain-deploy"
        bare.mkdir()
        git(bare, "init", "-q", "-b", "main")
        write(bare / "README.md")
        git(bare, "add", "-A")
        git(bare, "commit", "-qm", "init")
        wt = self.base / "sessions" / "plain"
        git(bare, "worktree", "add", "-q", "-b", "session/plain", str(wt), "main")
        notes = ensure_system_checkout(bare, wt, "")
        self.assertTrue(any("no submodules under '.'" in n for n in notes), notes)

    def test_uninitialized_submodule_is_a_note_or_an_error(self) -> None:
        git(self.root, "submodule", "deinit", "-q", "-f", self.SUB)
        with self.assertRaises(CheckoutError) as ctx:
            ensure_system_checkout(self.root, self.wt, "src/sys")
        self.assertIn("not initialized", str(ctx.exception))

    def test_unavailable_pinned_commit_falls_back_to_primary_head(self) -> None:
        missing = "1234567890abcdef1234567890abcdef12345678"
        git(self.wt, "update-index", "--cacheinfo", f"160000,{missing},{self.SUB}")
        git(self.wt, "commit", "-qm", "pin a commit nobody has")
        notes = ensure_system_checkout(self.root, self.wt, "src/sys")
        self.assertEqual(git(self.wt / self.SUB, "rev-parse", "HEAD"), self.primary_head)
        self.assertTrue(any("not available locally" in n for n in notes), notes)

    def test_non_empty_target_is_left_alone(self) -> None:
        write(self.wt / self.SUB / "stray.txt")
        with self.assertRaises(CheckoutError):
            ensure_system_checkout(self.root, self.wt, "src/sys")
        self.assertTrue((self.wt / self.SUB / "stray.txt").exists())

    # ---- link_paths

    def test_glob_expansion_prunes_node_modules(self) -> None:
        found = sc._expand(self.root, f"{self.SUB}/**/node_modules")
        self.assertEqual(found, [f"{self.SUB}/lib/a/node_modules", f"{self.SUB}/node_modules"])
        self.assertEqual(sc._expand(self.root, "src/*/app/node_modules"), [f"{self.SUB}/node_modules"])
        self.assertEqual(sc._expand(self.root, "src/sys/missing"), [])

    def test_link_to_unignored_dir_stays_visible(self) -> None:
        write(self.root / self.SUB / "vendor" / "lib.js")
        ensure_system_checkout(self.root, self.wt, "src/sys", link_paths=(f"{self.SUB}/vendor",))
        self.assertTrue(is_dir_link(self.wt / self.SUB / "vendor"))
        self.assertIn("vendor", git(self.wt / self.SUB, "status", "--porcelain"))

    def test_link_paths_link_to_primary(self) -> None:
        ensure_system_checkout(
            self.root, self.wt, "src/sys", link_paths=(f"{self.SUB}/**/node_modules",),
        )
        for rel in (f"{self.SUB}/node_modules", f"{self.SUB}/lib/a/node_modules"):
            link = self.wt / rel
            self.assertTrue(is_dir_link(link), rel)
            self.assertTrue(os.path.samefile(link, self.root / rel))
        self.assertTrue((self.wt / self.SUB / "node_modules" / "pkg" / "index.js").is_file())
        self.assertEqual(git(self.wt / self.SUB, "status", "--porcelain"), "")  # ignored by the product repo
        # idempotent: a second call neither fails nor replaces the link
        ensure_system_checkout(self.root, self.wt, "src/sys", link_paths=(f"{self.SUB}/node_modules",))
        self.assertTrue(is_dir_link(self.wt / self.SUB / "node_modules"))

    def test_link_never_overwrites_a_real_directory(self) -> None:
        ensure_system_checkout(self.root, self.wt, "src/sys")
        real = self.wt / self.SUB / "node_modules"
        write(real / "mine.js")
        ensure_system_checkout(self.root, self.wt, "src/sys", link_paths=(f"{self.SUB}/node_modules",))
        self.assertFalse(is_dir_link(real))
        self.assertTrue((real / "mine.js").exists())

    # ---- release

    def test_release_removes_checkout_and_links_but_not_targets(self) -> None:
        ensure_system_checkout(
            self.root, self.wt, "src/sys", link_paths=(f"{self.SUB}/node_modules",),
        )
        notes = release_system_checkouts(self.root, self.wt)
        self.assertFalse(any(n.startswith(KEPT_PREFIX) for n in notes), notes)
        self.assertFalse(os.path.lexists(self.wt / self.SUB / "node_modules"))
        self.assertTrue((self.root / self.SUB / "node_modules" / "pkg" / "index.js").is_file())
        self.assertEqual(list((self.wt / self.SUB).iterdir()), [])  # back to the empty gitlink dir
        self.assertEqual(len(self.primary_worktrees()), 1)
        self.assertEqual(self.session_status(), "")
        # and now git lets the session worktree itself go
        git(self.root, "worktree", "remove", str(self.wt))
        self.assertFalse(self.wt.exists())
        self.assertTrue((self.root / self.SUB / "app.txt").is_file())

    def test_release_is_a_noop_without_checkouts(self) -> None:
        self.assertEqual(release_system_checkouts(self.root, self.wt), [])
        self.assertEqual(release_system_checkouts(self.root, self.base / "gone"), [])

    def test_dirty_checkout_is_kept_unless_forced(self) -> None:
        ensure_system_checkout(self.root, self.wt, "src/sys")
        write(self.wt / self.SUB / "app.txt", "edited\n")
        notes = release_system_checkouts(self.root, self.wt)
        kept = [n for n in notes if n.startswith(KEPT_PREFIX)]
        self.assertEqual(len(kept), 1, notes)
        self.assertIn("uncommitted changes", kept[0])
        self.assertEqual((self.wt / self.SUB / "app.txt").read_text(encoding="utf-8"), "edited\n")
        release_system_checkouts(self.root, self.wt, force=True)
        self.assertEqual(list((self.wt / self.SUB).iterdir()), [])
        self.assertEqual(len(self.primary_worktrees()), 1)

    def test_branchless_commit_is_kept_until_it_is_on_a_branch(self) -> None:
        ensure_system_checkout(self.root, self.wt, "src/sys")
        checkout = self.wt / self.SUB
        write(checkout / "work.txt")
        git(checkout, "add", "-A")
        git(checkout, "commit", "-qm", "detached work")
        notes = release_system_checkouts(self.root, self.wt)
        self.assertTrue(any("no branch or remote" in n for n in notes if n.startswith(KEPT_PREFIX)), notes)
        git(checkout, "branch", "feature/keep")
        notes = release_system_checkouts(self.root, self.wt)
        self.assertFalse(any(n.startswith(KEPT_PREFIX) for n in notes), notes)
        self.assertIn("feature/keep", git(self.root / self.SUB, "branch", "--list", "feature/keep"))

    # ---- wiring into SessionManager._remove_worktree

    def _session(self) -> sessions_module.Session:
        return sessions_module.Session(
            id="s1", name="t", branch="session/s1", worktree_path=str(self.wt),
            base_branch="main", created_at=0.0, status="merged", is_default=False,
        )

    def test_remove_worktree_refuses_to_lose_submodule_work(self) -> None:
        ensure_system_checkout(self.root, self.wt, "src/sys")
        write(self.wt / self.SUB / "app.txt", "edited\n")
        with mock.patch.object(sessions_module, "REPO_ROOT", self.root):
            with self.assertRaises(ValueError) as ctx:
                sessions_module.SessionManager._remove_worktree(None, self._session(), force=False)
            self.assertIn(self.SUB, str(ctx.exception))
            self.assertTrue((self.wt / self.SUB / "app.txt").exists())
            # delete() semantics: force discards
            sessions_module.SessionManager._remove_worktree(None, self._session(), force=True)
        self.assertFalse(self.wt.exists())
        self.assertEqual(len(self.primary_worktrees()), 1)

    def test_remove_worktree_releases_clean_checkouts(self) -> None:
        ensure_system_checkout(self.root, self.wt, "src/sys", link_paths=(f"{self.SUB}/node_modules",))
        with mock.patch.object(sessions_module, "REPO_ROOT", self.root):
            sessions_module.SessionManager._remove_worktree(None, self._session(), force=False)
        self.assertFalse(self.wt.exists())
        self.assertTrue((self.root / self.SUB / "node_modules" / "pkg" / "index.js").is_file())


if __name__ == "__main__":
    unittest.main()
