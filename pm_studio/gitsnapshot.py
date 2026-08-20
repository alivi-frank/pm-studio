import subprocess
import sys
from pathlib import Path

from .config import CONFIG

# Where a session's SPEC.md/chat_history.json get copied before a reset or a terminate,
# so a reset/terminate never destroys anything - it only moves it out of workspace/current.
# Lives in each worktree's own checkout; a non-default session's archive entries reach
# main the same way any other commit on that branch does - via the normal merge.
ARCHIVE_PATH = CONFIG.archive_rel

# Server-owned state that must NEVER end up in a commit, whatever .gitignore says:
#
#   accounts.json  - password hashes and live login tokens
#   costing.json   - pay rates
#   audit.jsonl    - who did what, by name and email
#   activity.jsonl - per-person activity signals
#   trackers.json  - ticket titles pulled from the deployment's Jira/ADO
#
# trackers.json holds no credential of ours, but it is a cache of ANOTHER system's private
# data - issue titles and states from someone's Jira. It earns its place here on cost:
# never committing it costs exactly one re-sync, while committing it puts a third party's
# ticket titles in this repo's history permanently.
#
# A snapshot is repo-wide (`git add -A`), so before this list existed the only thing
# standing between a password hash and a pushed commit was the operator happening to have
# the right lines in .gitignore. That is the wrong shape for a credential boundary: a
# deployment that upgrades the package inherits a .gitignore written before these files
# existed, and would commit them on its very next PM turn. They are now unstaged
# unconditionally, so the guarantee holds with no config change and without re-running
# `init`. .gitignore still lists them too - this is the belt to that braces.
SENSITIVE_WORKSPACE_FILES = (
    "accounts.json",
    "costing.json",
    "audit.jsonl",
    "activity.jsonl",
    "trackers.json",
    # The people directory: real names and email addresses, reconciled out of the
    # assignees on another organisation's tickets. Same category as trackers.json and a
    # sharper case of it - a ticket title is their work, this is their identity.
    "people.json",
)


def sensitive_pathspecs(workspace_rel: str | None = None) -> list[str]:
    """Repo-root-relative pathspecs for state that must never be committed, including
    the `.tmp` siblings the stores write before an atomic replace - a snapshot landing
    inside that window would otherwise catch one."""
    rel = workspace_rel or CONFIG.workspace_rel
    specs = [f"{rel}/{name}" for name in SENSITIVE_WORKSPACE_FILES]
    specs.append(f"{rel}/*.tmp")
    return specs


def _unstage_sensitive(repo_root: Path) -> None:
    """Drops the credential-bearing files from the index after `git add -A`.

    `git reset -- <pathspec>` restores those entries from HEAD, so a file that is
    untracked (the normal case) simply leaves the index. Runs quietly and never raises:
    the point is only that the commit below cannot carry them.
    """
    subprocess.run(
        ["git", "reset", "-q", "--", *sensitive_pathspecs()],
        cwd=repo_root, capture_output=True, text=True,
    )


def _warn_if_already_tracked(repo_root: Path) -> None:
    """If one of these files is ALREADY committed, unstaging cannot help - it is in
    history. Say so loudly, with the command to fix it, rather than silently continuing
    to carry a leaked hash."""
    result = subprocess.run(
        ["git", "ls-files", "--", *sensitive_pathspecs()],
        cwd=repo_root, capture_output=True, text=True,
    )
    tracked = [line for line in result.stdout.splitlines() if line.strip()]
    if not tracked:
        return
    print(
        "[gitsnapshot] WARNING: credential-bearing state is tracked in git: "
        + ", ".join(tracked)
        + "\n[gitsnapshot] It is already in history, so ignoring it now is not enough. Run:"
        + "".join(f"\n    git rm --cached {path}" for path in tracked)
        + "\n[gitsnapshot] then commit, and treat anything in it as exposed - have users"
        " reset their passwords, and restart the server to mint a new agent token.",
        file=sys.stderr,
    )


def snapshot(message: str, repo_root: Path) -> None:
    """Stage and commit everything that changed in the given repo checkout (a session's
    worktree, or the primary checkout for the default session). Product sources live at
    the repo root, so a snapshot is repo-wide rather than scoped to a fixed path list -
    .gitignore (root and per-product) is what keeps ordinary runtime state (sessions.json,
    roadmap/, tasks/, chat history, node_modules, build output) out of these commits.

    Credential-bearing state does NOT rely on that: see SENSITIVE_WORKSPACE_FILES, which
    is unstaged here unconditionally.

    Never raises - a git hiccup should never break the PM or dev-agent flow."""
    try:
        subprocess.run(
            ["git", "add", "-A"],
            cwd=repo_root, check=True, capture_output=True, text=True,
        )
        _unstage_sensitive(repo_root)
        _warn_if_already_tracked(repo_root)
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
