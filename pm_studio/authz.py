"""What each role is allowed to do, plus the audit trail of who did it.

One table, one enforcement helper. The capability matrix lives here rather than being
scattered across endpoint bodies so that "what can a reviewer actually do?" is a
question you answer by reading a single dict instead of grepping the server.

Two rules shape the matrix:

- **Reads are open.** Everyone who can sign in can see the whole roadmap and every
  session. Transparency is the deployment default, so there is no per-user filtering
  of the board - what a role changes is what you may *do*, not what you may see.
- **Dispatching a dev agent is a code-execution boundary, not a UI affordance.** Dev
  agents run with bypassed permissions inside the repo, so `dispatch_dev_task` is
  effectively "may run arbitrary code on this host". It is granted to `pm` and `admin`
  and to nobody else, and it is checked on the HTTP path rather than hidden in the
  page, because the page is not what an attacker would use.

In personal mode none of this is consulted: there is no identity to authorize, and the
server behaves exactly as it did before accounts existed.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from .accounts import ROLES, Role
from .config import CONFIG

AUDIT_LOG_PATH = CONFIG.workspace_dir / "audit.jsonl"

Capability = Literal[
    "view",
    "run_session",
    "manage_session_lifecycle",
    "dispatch_dev_task",
    "manage_roadmap",
    "manage_users",
    "view_cost",
]

# role -> the capabilities it holds. Admin deliberately holds everything: the owner is
# the person who set the instance up and must never be able to lock themselves out of
# part of their own deployment.
CAPABILITIES: dict[Capability, tuple[Role, ...]] = {
    # Signing in at all is enough to read. See the transparency note above.
    "view": ("admin", "pm", "reviewer", "viewer"),
    # Driving a PM conversation: sending turns, switching model, retitling, resetting.
    "run_session": ("admin", "pm"),
    # Creating, merging, syncing, archiving, terminating, deleting sessions.
    "manage_session_lifecycle": ("admin", "pm"),
    # The code-execution boundary. See above.
    "dispatch_dev_task": ("admin", "pm"),
    # Writing to the roadmap board (create/update/move/delete items).
    "manage_roadmap": ("admin", "pm"),
    # The roster: invites, roles, enabling and disabling people.
    "manage_users": ("admin",),
    # Rates, capacities and the cost report. Admin-only: this is compensation data,
    # and it stays the narrowest grant in the matrix even though the roadmap it hangs
    # off is visible to everyone.
    "view_cost": ("admin",),
}

# A human explanation per capability, used in 403 bodies so a viewer who pokes at an
# endpoint gets told what would have been needed instead of a bare "Forbidden".
CAPABILITY_LABELS: dict[Capability, str] = {
    "view": "view this instance",
    "run_session": "work in PM sessions",
    "manage_session_lifecycle": "create or end sessions",
    "dispatch_dev_task": "dispatch dev agents",
    "manage_roadmap": "change the roadmap",
    "manage_users": "manage people",
    "view_cost": "see cost data",
}


def role_has(role: str, capability: Capability) -> bool:
    return role in CAPABILITIES[capability]


def capabilities_of(role: str) -> list[str]:
    """Everything this role holds - handed to the UI so pages can hide controls that
    would only 403. The server still enforces every one of them independently."""
    return [name for name, roles in CAPABILITIES.items() if role in roles]


def describe_matrix() -> dict[str, list[str]]:
    """The whole matrix, for the roster page and the docs."""
    return {role: capabilities_of(role) for role in ROLES}


@dataclass
class AuditEntry:
    at: float
    actor_id: str
    actor_email: str
    actor_role: str
    action: str
    target: str
    detail: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


class AuditLog:
    """Append-only JSONL of consequential actions.

    Deliberately not the roadmap/session JSON pattern: this file only ever grows and is
    never rewritten, so appending a line is the whole write path and a crash cannot
    corrupt earlier entries. It answers "who dispatched that agent?" - the question
    that has no answer at all once more than one person can.

    Reads/writes are only meaningful in enterprise mode; in personal mode there is a
    single trusted user and `record` is never called.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._lock = threading.Lock()
        self._path = path or AUDIT_LOG_PATH

    def record(
        self, actor, action: str, target: str = "", detail: str = ""
    ) -> AuditEntry:
        entry = AuditEntry(
            at=time.time(),
            actor_id=getattr(actor, "id", "unknown"),
            actor_email=getattr(actor, "email", ""),
            actor_role=getattr(actor, "role", ""),
            action=action,
            target=target,
            detail=detail,
        )
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a") as handle:
                handle.write(json.dumps(entry.to_dict()) + "\n")
        return entry

    def tail(self, limit: int = 200) -> list[dict]:
        """Most recent entries, newest first. Reads the whole file - fine for a log
        that grows a line per consequential action on a single-team instance, and it
        keeps the reader trivially correct."""
        if not self._path.exists():
            return []
        lines = self._path.read_text().splitlines()
        entries = []
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                # A truncated final line (killed mid-append) must not break the view.
                continue
            if len(entries) >= limit:
                break
        return entries
