"""The default session follows the primary checkout when the repo moves.

The failure this guards against: a repo moved (or re-cloned) to another path keeps the
old path in sessions.json, so the PM agent runs in a directory that is not this repo and
every gitsnapshot fails with `git add -A` exit 128.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pm_studio import sessions
from pm_studio.sessions import DEFAULT_SESSION_ID, Session, SessionManager


class DefaultSessionPathTest(unittest.TestCase):
    def test_stale_default_path_is_repointed_at_the_repo_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = Path(tmp) / "sessions.json"
            repo_root = Path(tmp) / "repo"
            stale = Session(
                id=DEFAULT_SESSION_ID, name="Default", branch="main",
                worktree_path=str(Path(tmp) / "old-location"), base_branch=None,
                created_at=0.0, status="active", is_default=True,
            )
            registry.write_text(json.dumps({DEFAULT_SESSION_ID: stale.to_dict()}))
            with mock.patch.object(sessions, "SESSIONS_REGISTRY_PATH", registry), \
                 mock.patch.object(sessions, "REPO_ROOT", repo_root), \
                 mock.patch.object(SessionManager, "_build_runtime", lambda self, s: None):
                manager = SessionManager()
            self.assertEqual(manager._sessions[DEFAULT_SESSION_ID].worktree_path, str(repo_root))
            saved = json.loads(registry.read_text())
            self.assertEqual(saved[DEFAULT_SESSION_ID]["worktree_path"], str(repo_root))


if __name__ == "__main__":
    unittest.main()
