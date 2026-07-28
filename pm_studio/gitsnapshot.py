import subprocess
import sys
from pathlib import Path

from .config import CONFIG

# Where a session's SPEC.md/chat_history.json get copied before a reset or a terminate,
# so a reset/terminate never destroys anything - it only moves it out of workspace/current.
# Lives in each worktree's own checkout; a non-default session's archive entries reach
# main the same way any other commit on that branch does - via the normal merge.
ARCHIVE_PATH = CONFIG.archive_rel


def snapshot(message: str, repo_root: Path) -> None:
    """Stage and commit everything that changed in the given repo checkout (a session's
    worktree, or the primary checkout for the default session). Product sources live at
    the repo root, so a snapshot is repo-wide rather than scoped to a fixed path list -
    .gitignore (root and per-product) is what keeps runtime state (sessions.json,
    roadmap/, tasks/, chat history, node_modules, build output) out of these commits.
    Never raises - a git hiccup should never break the PM or dev-agent flow."""
    try:
        subprocess.run(
            ["git", "add", "-A"],
            cwd=repo_root, check=True, capture_output=True, text=True,
        )
        nothing_staged = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=repo_root,
        ).returncode == 0
        if nothing_staged:
            return
        subprocess.run(
            ["git", "commit", "-m", message],
            cwd=repo_root, check=True, capture_output=True, text=True,
        )
    except Exception as exc:
        print(f"[gitsnapshot] skipped ({exc})", file=sys.stderr)
