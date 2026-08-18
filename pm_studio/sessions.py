from __future__ import annotations

import json
import subprocess
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Literal

from . import gitsnapshot
from .agent import PMAgent
from .config import CONFIG
from .models import DEFAULT_MODEL, validate_model
from .roadmap import PRODUCTS, owned_subtrees
from .tasks import TaskRegistry

REPO_ROOT = CONFIG.repo_root
SESSIONS_DIR = CONFIG.workspace_dir / "sessions"
SESSIONS_REGISTRY_PATH = CONFIG.workspace_dir / "sessions.json"

DEFAULT_SESSION_ID = "default"

Status = Literal["active", "merging", "merged", "conflict", "archived"]

# What a session is FOR - which is a different axis from every other field on it:
#
#   build    -> today's behavior: the PM specs work and dispatches dev tasks to build it.
#   research -> ideation / strategy / planning only: the PM researches, writes specs and
#               docs, and works the roadmap, but CANNOT dispatch dev tasks - no product
#               code gets written or changed from this session until the stakeholder
#               switches it to build.
#
# Enforced structurally, not by asking nicely: agent.py omits the dev-task dispatch curl
# from a research session's Bash allowlist (and tells the PM so), and server.py's
# POST /tasks/{session_id} refuses with a 403 regardless of who asks - the same
# belt-and-suspenders pairing that scopes roadmap writes.
Mode = Literal["build", "research"]
MODES: tuple[str, ...] = ("build", "research")


def validate_mode(mode: str) -> str:
    if mode not in MODES:
        raise ValueError(f"Unknown session mode: {mode!r} (valid: {', '.join(MODES)})")
    return mode

# git status --porcelain codes for unmerged (conflicted) paths.
_UNMERGED_CODES = ("UU ", "AA ", "DD ", "AU ", "UA ", "UD ", "DU ")


@dataclass
class Session:
    id: str
    name: str
    branch: str
    worktree_path: str | None
    base_branch: str | None
    created_at: float
    status: Status
    is_default: bool
    merge_result: dict | None = None
    # Outcome of the last `sync()` (main -> session) call. Distinct from merge_result:
    # a failed sync never changes `status` - it's a no-op refresh attempt, not part of
    # the session's own lifecycle - so it's tracked separately.
    sync_result: dict | None = None
    # Which product (see roadmap.PRODUCTS) this PM session is focused on - None for a
    # general/unassigned session (e.g. the default session), which gets a shallow
    # awareness digest of every product instead of deep context on one. Drives both
    # the roadmap context a PM's turns are given and which product's board its
    # curl-based roadmap writes are scoped to (see agent.py).
    product: str | None = None
    # Manual "hide from the default session list" flag - independent of `status`
    # (which describes lifecycle, not visibility). Set automatically when a session
    # reaches lifecycle status "archived" (see cleanup()/delete()/_do_terminate_inner()),
    # but can also be toggled directly via archive()/unarchive() on a still-active
    # session the user just wants out of their way.
    archived: bool = False
    # Which Claude model (see models.MODELS) this session's PM turns and dev tasks run
    # on. Defaults to Opus so pre-existing sessions.json records with no "model" key
    # (written before this field existed) keep today's behavior unchanged.
    model: str = DEFAULT_MODEL
    # What this session is for - "build" or "research" (see Mode above). Defaults to
    # "build" so pre-existing sessions.json records load unchanged with today's behavior;
    # live-switchable via SessionManager.set_mode, because ideation regularly graduates
    # into building (and a build session sometimes needs to be parked back into
    # research while a decision is pending).
    mode: str = "build"
    # Self-updating identity the PM maintains itself over the conversation (see
    # agent.py's session_meta curl + SessionManager.set_meta), the way claude.ai
    # auto-titles a chat: `title` is a ~2-5 word abbreviation shown as the session's
    # heading, `goal` a one-sentence description of what it's trying to accomplish.
    # Both nullable so old sessions.json records (written before these existed) load
    # unchanged and fall back to `name` in the UI until the PM sets them.
    title: str | None = None
    goal: str | None = None
    # Which Project in the work model (see portfolio.py) this session's work counts
    # towards. Optional and additive so older sessions.json records load unchanged.
    # This is what lets time/cost attribution place a PM turn or a dev task: sessions
    # are pinned to a PRODUCT, and a product is not a level in the work model, so
    # without this there is nothing to attribute activity to. A session with no
    # project falls back to the catch-all (see server.py's _session_project_id).
    project_id: str | None = None
    # Which Initiative (see portfolio.py) this session is working IN - the other axis of
    # scope, orthogonal to `product` and to `project_id`:
    #
    #   product       -> which boards this session may write to (a capability, enforced
    #                    by agent.py's allowlist)
    #   initiative_id -> what the session is about (its context and its attribution)
    #
    # Set for the case `product` cannot express: an initiative that cuts across several
    # integrated products, where pinning one board would be a lie about the work. Such a
    # session starts owning no board at all and adopts them as it identifies which
    # products are actually affected (see `adopted_products`). All four combinations are
    # meaningful and none is a special case: product only (a single product's PM),
    # initiative only (broad, products still to be found), both ("this board's share of
    # that initiative"), neither (the default session's shallow awareness).
    #
    # Validated by the caller, not here - the session registry has no business knowing
    # about the portfolio store, the same reason `project_id` is validated in server.py.
    # A session can outlive the initiative it names, so every reader treats an id that no
    # longer resolves as "unscoped" rather than an error.
    initiative_id: str | None = None
    # Boards this session has claimed write authority over beyond `product`, grown as an
    # initiative-scoped session works out which products it touches. Ownership is by
    # subtree, so adopting a parent adopts its children (see roadmap.owned_subtrees).
    #
    # Adoption is an explicit event rather than passive discovery because this list feeds
    # agent.py's PATCH allowlist, which is the actual enforcement boundary - a session
    # cannot drift into write access to a board nobody decided it owns.
    adopted_products: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    def owned_products(self) -> list[str]:
        """Every board this session may write to: its pinned product's subtree plus each
        adopted product's subtree. Empty for a session that owns nothing - the default
        session, and an initiative-scoped one that hasn't adopted anything yet.

        The single definition of ownership, used for the roadmap context a turn is given
        AND for the allowlist that enforces it, so the two cannot disagree about what
        this session owns.
        """
        pinned = [self.product] if self.product else []
        return owned_subtrees([*pinned, *self.adopted_products])

    @classmethod
    def from_dict(cls, data: dict) -> "Session":
        return cls(**data)


class SessionRuntime:
    """The live, in-process objects for one session: its PM conversation and its
    dev-task registry, sharing one git lock so their commits into the same
    worktree never interleave with each other or (for the default session) with
    a merge landing in that same checkout."""

    def __init__(self, session: Session) -> None:
        self.git_lock = threading.Lock()
        self.pm_agent = PMAgent(session, self.git_lock)
        self.task_registry = TaskRegistry(
            session.id, self.pm_agent.workspace_dir, self.pm_agent.repo_root, self.git_lock,
            model=session.model,
        )


class SessionManager:
    """Owns the session registry (git worktree lifecycle + persistence) and the
    live PMAgent/TaskRegistry pair for each session. The existing single-session
    state becomes session id "default", pinned to the primary checkout on
    `main` - no data migration required."""

    def __init__(self) -> None:
        self._registry_lock = threading.Lock()
        self._sessions: dict[str, Session] = {}
        self.runtimes: dict[str, SessionRuntime] = {}
        self._subscribers: list[Callable[[dict], None]] = []
        self._load_or_bootstrap()

    # ---- registry persistence ----

    def _load_or_bootstrap(self) -> None:
        dirty = False
        if SESSIONS_REGISTRY_PATH.exists():
            raw = json.loads(SESSIONS_REGISTRY_PATH.read_text())
            self._sessions = {sid: Session.from_dict(data) for sid, data in raw.items()}
            # Migration: sessions that reached lifecycle status "archived" before the
            # `archived` visibility flag existed predate this field entirely (it
            # defaults to False), so they'd otherwise stay stuck in the default view.
            # Backfill them as archived too.
            for sid, data in raw.items():
                if "archived" not in data and self._sessions[sid].status == "archived":
                    self._sessions[sid].archived = True
                    dirty = True
                # Migration: records written before `model` existed default to Opus via
                # the dataclass field above - just persist that default explicitly so
                # sessions.json stops being silently stale about it.
                if "model" not in data:
                    dirty = True
                # Same migration posture for `mode`: pre-existing records are build
                # sessions, persisted explicitly on first load.
                if "mode" not in data:
                    dirty = True

        if DEFAULT_SESSION_ID not in self._sessions:
            self._sessions[DEFAULT_SESSION_ID] = Session(
                id=DEFAULT_SESSION_ID,
                name=CONFIG.default_session_name,
                branch="main",
                worktree_path=str(REPO_ROOT),
                base_branch=None,
                created_at=time.time(),
                status="active",
                is_default=True,
            )
            self._save()

        for session in self._sessions.values():
            # A "merging" status only means something while its background thread
            # is alive; a fresh process start means that thread is gone, so treat
            # it as stale rather than permanently stuck.
            if session.status == "merging":
                session.status = "active"
                dirty = True
            if session.worktree_path is not None:
                self.runtimes[session.id] = self._build_runtime(session)
        if dirty:
            self._save()

    def _build_runtime(self, session: Session) -> SessionRuntime:
        """Constructs a session's live runtime and wires its PMAgent's activity hook
        back to a sessions broadcast, so a PM turn starting/ending flips the session's
        card on the list page live (see activity_of / server.py's enrichment)."""
        runtime = SessionRuntime(session)
        runtime.pm_agent.on_activity_change = lambda sid=session.id: self._broadcast_activity(sid)
        return runtime

    def _broadcast_activity(self, session_id: str) -> None:
        """Emits a session_updated event purely to refresh derived live activity (PM
        turn start/end) - the persisted Session is unchanged; server.py enriches the
        payload with activity_of before it reaches the sessions page."""
        session = self._sessions.get(session_id)
        if session is not None:
            self._broadcast({"type": "session_updated", "session": session.to_dict()})

    def _save(self) -> None:
        SESSIONS_REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
        SESSIONS_REGISTRY_PATH.write_text(
            json.dumps({sid: s.to_dict() for sid, s in self._sessions.items()}, indent=2)
        )

    # ---- broadcast (mirrors tasks.TaskRegistry's subscriber pattern) ----

    def subscribe(self, callback: Callable[[dict], None]) -> None:
        self._subscribers.append(callback)

    def _broadcast(self, event: dict) -> None:
        for callback in list(self._subscribers):
            callback(event)

    # ---- lookups ----

    def list_sessions(self) -> list[Session]:
        return sorted(self._sessions.values(), key=lambda s: s.created_at)

    def get(self, session_id: str) -> Session | None:
        return self._sessions.get(session_id)

    def get_runtime(self, session_id: str) -> SessionRuntime | None:
        return self.runtimes.get(session_id)

    def _require(self, session_id: str) -> Session:
        session = self._sessions.get(session_id)
        if session is None:
            raise KeyError(f"Unknown session: {session_id}")
        return session

    @property
    def main_git_lock(self) -> threading.Lock:
        return self.runtimes[DEFAULT_SESSION_ID].git_lock

    # ---- cross-session visibility ----

    def describe_other_active_sessions(self, exclude_id: str, tasks_per_session: int = 3) -> str:
        """Human-readable summary of what every OTHER active session is currently running
        or last did, for injection into a PM's turn (see agent.py's _with_session_context)
        so it can catch an in-flight or just-finished duplicate before dispatching more of
        it - each session is its own git worktree/branch, invisible to another session's PM
        unless handed this explicitly. Returns "" when there's nothing else active, so
        callers can skip the block entirely rather than inject "nothing else running" noise
        into every single turn."""
        lines = []
        for session in self.list_sessions():
            if session.id == exclude_id or session.status != "active":
                continue
            runtime = self.runtimes.get(session.id)
            if runtime is None:
                continue
            # Prefer the PM-maintained title over the creation-time name, and append the
            # goal when set, so each other-session line is a genuinely informative
            # one-liner of what that session is about rather than just a bare handle.
            label = session.title or session.name
            goal_suffix = f" — {session.goal}" if session.goal else ""
            tasks = runtime.task_registry.list_tasks()[:tasks_per_session]
            if not tasks:
                lines.append(f'- "{label}" ({session.id}){goal_suffix}: active, no dev tasks yet.')
                continue
            task_summary = "; ".join(
                f'{t["status"]}: "{t["description"][:100]}"' for t in tasks
            )
            lines.append(f'- "{label}" ({session.id}){goal_suffix}: {task_summary}')
        return "\n".join(lines)

    # ---- live activity (derived, never persisted into sessions.json) ----

    def activity_of(self, session_id: str) -> dict:
        """Coarse live activity for a session, orthogonal to its git-lifecycle `status`:
        what the session is doing *right now*. Returns
        {"state": "working"|"waiting"|"idle", "reason": "thinking"|"dev_task"|None,
         "detail": <short running dev-task description or None>}.

        Priority: working(dev_task) > working(thinking) > waiting > idle.
          - working/dev_task: any task in this session's registry is running (a running
            dev task supplies `detail`; a merge-conflict resolver still counts as working
            but stays generic - it isn't part of the PM's own plan).
          - working/thinking: the PM is mid-turn (pm_agent.turn_active).
          - waiting: neither of the above and the last transcript message is the PM's
            (it responded and now awaits the stakeholder) - the "your move" state.
          - idle: otherwise. Also returned for any non-active/conflict lifecycle status,
            where live activity indicators don't apply.
        Purely derived - never written to sessions.json."""
        idle = {"state": "idle", "reason": None, "detail": None}
        session = self._sessions.get(session_id)
        if session is None or session.status not in ("active", "conflict"):
            return idle
        runtime = self.runtimes.get(session_id)
        if runtime is None:
            return idle

        running = [t for t in runtime.task_registry.list_tasks() if t.get("status") == "running"]
        if running:
            dev = next((t for t in running if t.get("kind") == "dev"), None)
            detail = dev.get("description") if dev is not None else None
            return {"state": "working", "reason": "dev_task", "detail": detail}
        if runtime.pm_agent.turn_active:
            return {"state": "working", "reason": "thinking", "detail": None}
        if runtime.pm_agent.last_message_role() == "assistant":
            return {"state": "waiting", "reason": None, "detail": None}
        return idle

    # ---- session lifecycle ----

    def create(
        self,
        name: str | None,
        product: str | None = None,
        model: str | None = None,
        project_id: str | None = None,
        initiative_id: str | None = None,
        mode: str | None = None,
    ) -> Session:
        if product is not None and product not in PRODUCTS:
            raise ValueError(f"Unknown product: {product}")
        model = validate_model(model) if model is not None else DEFAULT_MODEL
        mode = validate_mode(mode) if mode is not None else "build"
        with self._registry_lock:
            session_id = uuid.uuid4().hex[:8]
            branch = f"session/{session_id}"
            worktree_path = SESSIONS_DIR / session_id
            SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

            subprocess.run(
                ["git", "worktree", "add", "-b", branch, str(worktree_path), "main"],
                cwd=REPO_ROOT, check=True, capture_output=True, text=True,
            )

            session = Session(
                id=session_id,
                name=name or (PRODUCTS[product] if product else f"Session {session_id}"),
                branch=branch,
                worktree_path=str(worktree_path),
                base_branch="main",
                created_at=time.time(),
                status="active",
                is_default=False,
                product=product,
                model=model,
                mode=mode,
                project_id=project_id,
                initiative_id=initiative_id,
            )
            self._sessions[session_id] = session
            self._save()
            self.runtimes[session_id] = self._build_runtime(session)

        self._broadcast({"type": "session_created", "session": session.to_dict()})
        return session

    def cleanup(self, session_id: str) -> Session:
        with self._registry_lock:
            session = self._require(session_id)
            if session.status != "merged":
                raise ValueError(
                    f"Session {session_id} is not merged (status={session.status}); refusing to clean up."
                )
            self._remove_worktree(session, force=False)
            session.status = "archived"
            session.archived = True
            session.worktree_path = None
            self.runtimes.pop(session_id, None)
            self._save()

        self._broadcast({"type": "session_updated", "session": session.to_dict()})
        return session

    def delete(self, session_id: str) -> Session:
        with self._registry_lock:
            session = self._require(session_id)
            if session.is_default:
                raise ValueError("Cannot delete the default session.")
            self._remove_worktree(session, force=True)
            session.status = "archived"
            session.archived = True
            session.worktree_path = None
            self.runtimes.pop(session_id, None)
            self._save()

        self._broadcast({"type": "session_updated", "session": session.to_dict()})
        return session

    def _remove_worktree(self, session: Session, force: bool) -> None:
        if not session.worktree_path:
            return
        args = ["git", "worktree", "remove"]
        if force:
            args.append("--force")
        args.append(session.worktree_path)
        subprocess.run(args, cwd=REPO_ROOT, check=True, capture_output=True, text=True)
        branch_flag = "-D" if force else "-d"
        # Best-effort: don't fail cleanup if the branch delete itself has trouble
        # (e.g. already gone) - the worktree removal above is the part that matters.
        subprocess.run(
            ["git", "branch", branch_flag, session.branch],
            cwd=REPO_ROOT, capture_output=True, text=True,
        )

    # ---- merge ----

    def merge(self, session_id: str) -> Session:
        session = self._require(session_id)
        if session.is_default:
            raise ValueError("The default session is the merge target; it can't be merged.")
        if session.status not in ("active", "conflict"):
            raise ValueError(f"Session {session_id} can't be merged from status '{session.status}'.")

        session.status = "merging"
        self._save()
        self._broadcast({"type": "session_updated", "session": session.to_dict()})

        threading.Thread(target=self._do_merge, args=(session_id,), daemon=True).start()
        return session

    def _do_merge(self, session_id: str) -> None:
        try:
            self._do_merge_inner(session_id)
        except Exception as exc:  # a merge attempt should never crash the server or hang a session
            session = self._sessions.get(session_id)
            if session is None:
                return
            session.status = "conflict"
            session.merge_result = {
                "outcome": "failed",
                "message": f"Merge failed unexpectedly: {exc}",
                "merged_at": time.time(),
            }
            self._save()
            self._broadcast({"type": "session_merged", "session": session.to_dict(), "outcome": "failed"})

    def _do_merge_inner(self, session_id: str) -> None:
        session = self._sessions[session_id]
        default_runtime = self.runtimes[DEFAULT_SESSION_ID]
        session_worktree = Path(session.worktree_path)

        # Soft precondition: refuse if default's own dev-task registry looks busy.
        # Not airtight (a 30-min subprocess could still be writing files after this
        # check passes) but cheap and covers the common case for a single local user.
        if any(t["status"] == "running" for t in default_runtime.task_registry.list_tasks()):
            session.status = "active"
            session.merge_result = {
                "outcome": "failed",
                "message": "A dev task is currently running on the default session; try again once it finishes.",
                "merged_at": time.time(),
            }
            self._save()
            self._broadcast({"type": "session_updated", "session": session.to_dict()})
            return

        # Archive before touching anything - a merge into main is always the last step
        # before a session ends, so its final spec/chat should never depend on the
        # session's own worktree surviving. Picked up by the pre-merge snapshot below.
        self.runtimes[session_id].pm_agent.archive_current("merge")

        with self.runtimes[session_id].git_lock:
            gitsnapshot.snapshot(f"Pre-merge snapshot for session {session_id}", session_worktree)

        with default_runtime.git_lock:  # this is the MAIN_GIT_LOCK - serializes with default's own commits
            gitsnapshot.snapshot(f"Pre-merge safety snapshot before merging {session_id}", REPO_ROOT)

            outcome = self._run_merge(
                worktree_root=REPO_ROOT,
                branch_to_merge=session.branch,
                commit_message=f"Merge session {session_id} ({session.name}) into main",
                conflict_label=f"session/{session_id}",
                conflict_runtime=default_runtime,
            )

        outcome["merged_at"] = time.time()
        if outcome["outcome"] == "failed":
            session.status = "conflict"
            session.merge_result = outcome
            self._save()
            self._broadcast({"type": "session_merged", "session": session.to_dict(), "outcome": "failed"})
            return

        session.status = "merged"
        session.merge_result = outcome
        self._save()
        self._broadcast({"type": "session_merged", "session": session.to_dict(), "outcome": outcome["outcome"]})

    def _run_merge(
        self,
        worktree_root: Path,
        branch_to_merge: str,
        commit_message: str,
        conflict_label: str,
        conflict_runtime: SessionRuntime,
    ) -> dict:
        """Runs `git merge --no-ff <branch_to_merge>` in worktree_root. On conflict,
        dispatches a dev task via conflict_runtime's task_registry to resolve it,
        independently verifies the result, and commits or aborts. Direction-agnostic:
        used both for merging a session's branch into main (worktree_root=REPO_ROOT,
        conflict_runtime=default) and for syncing main into a session's own worktree
        (worktree_root=that session's worktree, conflict_runtime=that session's own).
        Caller holds whatever git_lock guards worktree_root; never raises for a merge
        conflict itself, only returns an outcome dict."""
        merge_proc = subprocess.run(
            ["git", "merge", "--no-ff", branch_to_merge, "-m", commit_message],
            cwd=worktree_root, capture_output=True, text=True,
        )
        if merge_proc.returncode == 0:
            return {"outcome": "clean"}

        conflicted = subprocess.run(
            ["git", "diff", "--name-only", "--diff-filter=U"],
            cwd=worktree_root, capture_output=True, text=True,
        ).stdout.split()

        description = (
            f"This repository ({worktree_root}) is in the middle of a git merge, merging branch "
            f"'{branch_to_merge}' into the current branch. The merge stopped because of conflicts "
            f"in these files:\n" + "\n".join(f"- {f}" for f in conflicted)
            + "\n\nOpen each file, resolve the <<<<<<< / ======= / >>>>>>> conflict markers, "
            "preserving the intent of both sides as best you can, then `git add` each resolved "
            "file. Do NOT run `git commit` yourself and do NOT touch any files other than the "
            "ones listed above."
        )

        resolver_task = conflict_runtime.task_registry.run_conflict_resolution(
            f"[merge-conflict {conflict_label}] {description}"
        )

        if self._merge_still_broken(worktree_root, conflicted) or resolver_task["status"] == "error":
            subprocess.run(["git", "merge", "--abort"], cwd=worktree_root, capture_output=True, text=True)
            return {
                "outcome": "failed",
                "message": resolver_task["result"],
                "conflicted_files": conflicted,
                "resolver_task_id": resolver_task["id"],
            }

        commit_proc = subprocess.run(
            ["git", "commit", "--no-edit"], cwd=worktree_root, capture_output=True, text=True,
        )
        if commit_proc.returncode != 0:
            subprocess.run(["git", "merge", "--abort"], cwd=worktree_root, capture_output=True, text=True)
            return {
                "outcome": "failed",
                "message": f"Resolver appeared to succeed but the merge commit itself failed: {commit_proc.stderr.strip()[:2000]}",
                "conflicted_files": conflicted,
                "resolver_task_id": resolver_task["id"],
            }

        # Auto-resolved and independently verified clean - commit lands immediately
        # (confirmed product decision: no manual confirm step for this path).
        return {
            "outcome": "auto_resolved",
            "message": resolver_task["result"],
            "conflicted_files": conflicted,
            "resolver_task_id": resolver_task["id"],
        }

    @staticmethod
    def _merge_still_broken(worktree_root: Path, conflicted_files: list[str]) -> bool:
        """Independent verification, not trusting the resolver's self-report:
        checks for any remaining unmerged paths and leftover conflict markers."""
        status = subprocess.run(
            ["git", "status", "--porcelain"], cwd=worktree_root, capture_output=True, text=True,
        ).stdout
        if any(line.startswith(_UNMERGED_CODES) for line in status.splitlines()):
            return True

        for rel_path in conflicted_files:
            path = worktree_root / rel_path
            if not path.exists():
                continue
            # Only the line-start <<<<<<< / >>>>>>> markers are checked - "======="
            # alone is too common in legitimate content (README dividers, etc.) to
            # be a reliable signal on its own.
            lines = path.read_text(errors="ignore").splitlines()
            if any(line.startswith("<<<<<<<") or line.startswith(">>>>>>>") for line in lines):
                return True

        return False

    # ---- sync (main -> session) ----

    def sync(self, session_id: str) -> Session:
        """Merges main into a session's own worktree, so a long-lived session doesn't
        drift far enough from main to make its eventual merge-back painful. Unlike
        merge()/terminate(), failure never changes the session's status - a sync is a
        best-effort refresh, not part of the session's own lifecycle."""
        session = self._require(session_id)
        if session.is_default:
            raise ValueError("The default session is main; there's nothing to sync it from.")
        if session.status not in ("active", "conflict"):
            raise ValueError(f"Session {session_id} can't be synced from status '{session.status}'.")

        threading.Thread(target=self._do_sync, args=(session_id,), daemon=True).start()
        return session

    def _do_sync(self, session_id: str) -> None:
        try:
            self._do_sync_inner(session_id)
        except Exception as exc:  # a sync attempt should never crash the server or hang a session
            session = self._sessions.get(session_id)
            if session is None:
                return
            session.sync_result = {
                "outcome": "failed",
                "message": f"Sync failed unexpectedly: {exc}",
                "synced_at": time.time(),
            }
            self._save()
            self._broadcast({"type": "session_synced", "session": session.to_dict(), "outcome": "failed"})

    def _do_sync_inner(self, session_id: str) -> None:
        session = self._sessions[session_id]
        session_runtime = self.runtimes[session_id]
        session_worktree = Path(session.worktree_path)

        if any(t["status"] == "running" for t in session_runtime.task_registry.list_tasks()):
            session.sync_result = {
                "outcome": "failed",
                "message": "A dev task is currently running on this session; try again once it finishes.",
                "synced_at": time.time(),
            }
            self._save()
            self._broadcast({"type": "session_updated", "session": session.to_dict()})
            return

        with session_runtime.git_lock:
            gitsnapshot.snapshot(f"Pre-sync snapshot for session {session_id}", session_worktree)

            outcome = self._run_merge(
                worktree_root=session_worktree,
                branch_to_merge="main",
                commit_message=f"Sync main into session {session_id}",
                conflict_label=f"sync session/{session_id}",
                conflict_runtime=session_runtime,
            )

        outcome["synced_at"] = time.time()
        session.sync_result = outcome
        self._save()
        self._broadcast({"type": "session_synced", "session": session.to_dict(), "outcome": outcome["outcome"]})

    # ---- terminate (merge -> archive -> remove worktree) ----

    def terminate(self, session_id: str) -> Session:
        """The only way a non-default session ends: always merges its work into main
        first (auto-resolving conflicts the same way merge() does), and only removes
        the worktree/branch once that's landed - so a terminated session's history is
        never lost to a force-deleted, unmerged branch. On conflict, stops and leaves
        the session in "conflict" status for you to retry - there is no path that
        skips the merge."""
        session = self._require(session_id)
        if session.is_default:
            raise ValueError("The default session is the merge target; it can't be terminated.")
        if session.status not in ("active", "conflict"):
            raise ValueError(f"Session {session_id} can't be terminated from status '{session.status}'.")

        session.status = "merging"
        self._save()
        self._broadcast({"type": "session_updated", "session": session.to_dict()})

        threading.Thread(target=self._do_terminate, args=(session_id,), daemon=True).start()
        return session

    def _do_terminate(self, session_id: str) -> None:
        try:
            self._do_terminate_inner(session_id)
        except Exception as exc:  # a terminate attempt should never crash the server or hang a session
            session = self._sessions.get(session_id)
            if session is None:
                return
            session.status = "conflict"
            session.merge_result = {
                "outcome": "failed",
                "message": f"Terminate failed unexpectedly: {exc}",
                "merged_at": time.time(),
            }
            self._save()
            self._broadcast({"type": "session_merged", "session": session.to_dict(), "outcome": "failed"})

    def _do_terminate_inner(self, session_id: str) -> None:
        # Reuses the exact merge machinery merge() uses (including the archive-before-
        # merge step in _do_merge_inner) - terminate is just "merge, then don't stop".
        self._do_merge_inner(session_id)
        session = self._sessions[session_id]
        if session.status != "merged":
            return  # conflict (or the busy-precondition bail-out) - stop here, as merge() does

        with self._registry_lock:  # same lock create()/cleanup()/delete() use around registry mutation
            self._remove_worktree(session, force=False)  # safe delete: guaranteed merged by now
            session.status = "archived"
            session.archived = True
            session.worktree_path = None
            self.runtimes.pop(session_id, None)
            self._save()
        self._broadcast({"type": "session_merged", "session": session.to_dict(), "outcome": session.merge_result["outcome"]})

    # ---- archive flag (list visibility, independent of lifecycle status) ----

    def archive(self, session_id: str) -> Session:
        with self._registry_lock:
            session = self._require(session_id)
            session.archived = True
            self._save()
        self._broadcast({"type": "session_updated", "session": session.to_dict()})
        return session

    def unarchive(self, session_id: str) -> Session:
        with self._registry_lock:
            session = self._require(session_id)
            session.archived = False
            self._save()
        self._broadcast({"type": "session_updated", "session": session.to_dict()})
        return session

    # ---- self-updating title + goal (PM-maintained identity) ----

    def set_meta(self, session_id: str, title: str | None = None, goal: str | None = None) -> Session:
        """Sets the PM-maintained title and/or goal for a session (see agent.py's
        session_meta curl). Either can be updated independently - a provided value
        that's empty/whitespace-only is treated as "no change" for that field rather
        than clearing it, so the PM can never blank out its own identity by accident.
        Values are trimmed and clamped to sane lengths. Persists and broadcasts a
        `session_updated` event so the sessions list reflects it live."""
        with self._registry_lock:
            session = self._require(session_id)
            if title is not None and title.strip():
                session.title = title.strip()[:60]
            if goal is not None and goal.strip():
                session.goal = goal.strip()[:200]
            self._save()
        self._broadcast({"type": "session_updated", "session": session.to_dict()})
        return session

    # ---- scope: initiative + adopted product boards ----

    def set_initiative(self, session_id: str, initiative_id: str | None) -> Session:
        """Scopes this session to an Initiative, or clears it with None.

        Changes what the session's turns are given as context (see server.py's
        _roadmap_context_for) and where its activity is attributed when it has no project
        of its own, so the live runtime is refreshed too - effective from the next turn.
        The caller validates that the initiative exists (see set_project on why).
        """
        with self._registry_lock:
            session = self._require(session_id)
            session.initiative_id = initiative_id or None
            self._refresh_scope(session)
            self._save()
        self._broadcast({"type": "session_updated", "session": session.to_dict()})
        return session

    def adopt_product(self, session_id: str, product: str) -> Session:
        """Claims write authority over one more board, for a session that has worked out
        this product is affected by its initiative. Idempotent.

        This is the one path that widens what a session may write to after creation, which
        is why it is a deliberate call and not a side effect of mentioning a product: it
        rebuilds the PM's PATCH allowlist. A product already covered by what the session
        owns (its pinned subtree, or a subtree it has already adopted) is accepted and
        recorded as a no-op rather than rejected - the caller asked for a state, not a
        change, and the state already holds.
        """
        if product not in PRODUCTS:
            raise ValueError(f"Unknown product: {product}")
        with self._registry_lock:
            session = self._require(session_id)
            if product in session.owned_products():
                return session
            session.adopted_products = [*session.adopted_products, product]
            self._refresh_scope(session)
            self._save()
        self._broadcast({"type": "session_updated", "session": session.to_dict()})
        return session

    def release_product(self, session_id: str, product: str) -> Session:
        """Drops an adopted board again - a product that turned out not to be affected
        after all. Only ever touches `adopted_products`: the product a session was pinned
        to at creation is not something a later scope correction gets to remove."""
        with self._registry_lock:
            session = self._require(session_id)
            if product not in session.adopted_products:
                return session
            session.adopted_products = [p for p in session.adopted_products if p != product]
            self._refresh_scope(session)
            self._save()
        self._broadcast({"type": "session_updated", "session": session.to_dict()})
        return session

    def _refresh_scope(self, session: Session) -> None:
        """Pushes a scope change into the live PMAgent, so its system prompt and its
        allowlist match the new ownership without a server restart. Caller holds
        _registry_lock. No-op for a session with no runtime (archived, or worktree gone).

        Deliberately not a runtime rebuild: replacing the runtime would drop the PM's
        in-flight turn lock and its dev-task registry. Only the two derived, per-turn
        values need to change."""
        runtime = self.runtimes.get(session.id)
        if runtime is not None:
            runtime.pm_agent.set_scope(
                initiative_id=session.initiative_id,
                adopted_products=session.adopted_products,
            )

    def set_project(self, session_id: str, project_id: str | None) -> Session:
        """Points this session's work at a Project in the work model, or clears it with
        None. Drives where the session's activity signals are attributed (see
        costing.py) - the caller validates that the project exists, since the session
        registry has no business knowing about the portfolio store."""
        with self._registry_lock:
            session = self._require(session_id)
            session.project_id = project_id or None
            self._save()
        self._broadcast({"type": "session_updated", "session": session.to_dict()})
        return session

    # ---- model (per-session Claude model, live-updatable) ----

    def set_model(self, session_id: str, model: str) -> Session:
        """Changes which Claude model this session's PM turns and dev tasks run on,
        effective immediately - updates the live runtime (if one exists) in addition
        to persisting, so it applies to the very next turn/task with no server
        restart required."""
        validate_model(model)
        with self._registry_lock:
            session = self._require(session_id)
            session.model = model
            runtime = self.runtimes.get(session_id)
            if runtime is not None:
                runtime.pm_agent.set_model(model)
                runtime.task_registry.model = model
            self._save()
        self._broadcast({"type": "session_updated", "session": session.to_dict()})
        return session

    # ---- mode (build vs research, live-updatable) ----

    def set_mode(self, session_id: str, mode: str) -> Session:
        """Switches a session between build and research (see Mode), effective on the
        very next PM turn - like a scope change, this rebuilds the live PMAgent's
        system prompt and Bash allowlist together, so what the PM is told and what it
        can actually do never disagree. The server-side dispatch refusal reads the
        persisted session directly, so it flips at the same moment."""
        validate_mode(mode)
        with self._registry_lock:
            session = self._require(session_id)
            session.mode = mode
            runtime = self.runtimes.get(session_id)
            if runtime is not None:
                runtime.pm_agent.set_mode(mode)
            self._save()
        self._broadcast({"type": "session_updated", "session": session.to_dict()})
        return session
