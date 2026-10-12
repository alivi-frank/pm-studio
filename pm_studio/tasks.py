import contextlib
import json
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Callable

from . import gitsnapshot, judge, roadmap, session_checkout
from . import agent_mcp
from .agent_env import subprocess_env
from .config import CONFIG, DEV_INSTRUCTIONS_NAME, LOCAL_DIR_NAME
from .agent import _stream_claude, progress_events
from .costing import agent_usage
from . import models as models_module
from .models import DEFAULT_MODEL

DEV_AGENT_TIMEOUT_SECONDS = 1800
# How a dev agent's `claude` process is run: streamed, so its steps reach the task card
# live. A module attribute so tests can stand in for the CLI.
run_agent = _stream_claude
# The steps a task record keeps (newest last), and how much of each.
MAX_PROGRESS_STEPS = 40
MAX_STEP_CHARS = 240
# A quick task only looks: it brings up what it needs, checks, captures evidence and
# reports - so it gets a tighter leash than a build task.
QUICK_TASK_TIMEOUT_SECONDS = 900

# Files a dev task leaves for the stakeholder (screenshots, logs, reports) go to
# <tasks_dir>/<task id>/ and are listed on the task record as "artifacts". Bounded so a
# task that dumps a build tree there cannot bloat the record or the chat.
MAX_ARTIFACTS = 50
ARTIFACTS_ENV = "PM_STUDIO_ARTIFACTS_DIR"
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp")

QUICK_TASK_PREAMBLE = """[QUICK TASK - look, don't build. Start what you need (a local \
environment, a browser), check what is asked, capture evidence, and report briefly. Do not \
change product code, do not commit, do not push, and do not open pull requests; if what you \
find needs a code change, say so in your report instead of making it.]

"""

# Every dev task: when what the task depends on is not reachable, the useful result is a
# fast, precise report - not an agent improvising another way in (one once searched the
# whole disk for MCP configuration files after a database connection was refused).
UNAVAILABLE_SOURCE_RULE = """

[If a tool or data source this task needs is unavailable - an MCP server that fails to \
connect, a service that will not start, missing credentials - retry at most once, then stop \
and report the literal error and what would fix it. Never search the filesystem for \
configuration files, credentials or connection strings, and never try another way to reach \
the same data that you were not given.]"""


def ensure_system_code(worktree: Path, system: str) -> list[str]:
    """Makes `system`'s code present in a session worktree, where submodules start as
    empty folders (see session_checkout). Idempotent and cheap once done; a no-op for
    the primary checkout, for an unknown system, or for one with no `path`. Raises
    session_checkout.CheckoutError when the system has submodules and none of them could
    be prepared."""
    spec = roadmap.SYSTEMS.get(system) if system else None
    if spec is None or not spec.path:
        return []
    links = tuple(spec.local.link_from_primary) if spec.local is not None else ()
    return session_checkout.ensure_system_checkout(CONFIG.repo_root, worktree, spec.path, links)


def list_artifacts(directory: Path) -> list[dict]:
    """[{name, size}] for the files under `directory` (posix-relative names, sorted),
    at most MAX_ARTIFACTS. Empty when the directory does not exist."""
    if not directory.is_dir():
        return []
    files = sorted(p for p in directory.rglob("*") if p.is_file())
    return [
        {"name": p.relative_to(directory).as_posix(), "size": p.stat().st_size}
        for p in files[:MAX_ARTIFACTS]
    ]


def validate_dispatch_system(system: str) -> str | None:
    """Why a dev-task dispatch's `system` is unacceptable, or None when it's fine.

    Mirrors the roadmap store's attribution rules (see roadmap.RoadmapStore.create):
    once [systems] is declared, every dev task names the one system whose code it will
    change - that is what routes the system's non-negotiable git workflow rules into
    the dev agent's prompt, so an unattributed dispatch would silently run without
    them. With no [systems] declared there is no taxonomy to attribute to, and naming
    one is refused rather than quietly ignored - the same honesty call the store makes.
    Reads roadmap.SYSTEMS through the module so tests that swap the taxonomy in place
    are honored here too."""
    declared = roadmap.SYSTEMS
    if not declared:
        if system:
            return (
                f'no [systems] are declared in this deployment, so "system" cannot be '
                f"used on a dev task (got {system!r})"
            )
        return None
    valid = ", ".join(declared)
    if not system:
        return (
            '"system" is required: every dev task names the one system whose code it '
            f"will change. Valid ids: {valid}"
        )
    if system not in declared:
        return f"unknown system {system!r}. Valid ids: {valid}"
    return None


class TaskRegistry:
    """Dev-task lifecycle for a single session: dispatches headless `claude -p`
    dev-agent subprocesses running at that session's worktree root (where the
    product sources live), and persists their status/result as JSON under
    <workspace_dir>/tasks/.

    `git_lock` is shared with whatever else may commit in `repo_root` (the PM's
    own turns, and - for the default session specifically - the merge flow), so
    git operations against the same worktree never interleave.
    """

    def __init__(
        self,
        session_id: str,
        workspace_dir: Path,
        repo_root: Path,
        git_lock: threading.Lock,
        model: str = DEFAULT_MODEL,
    ) -> None:
        self.session_id = session_id
        self.workspace_dir = workspace_dir
        self.repo_root = repo_root
        self.git_lock = git_lock
        self.tasks_dir = workspace_dir / "tasks"
        self._subscribers: list[Callable[[dict], None]] = []
        # Live-updatable (see SessionManager.set_model) - read fresh by _execute on
        # every dev task, so a change takes effect on the next dispatch.
        self.model = model
        self._reconcile_stale_tasks()

    def _reconcile_stale_tasks(self) -> None:
        """Called once per registry construction (i.e. server startup, or a brand-new
        session - a no-op there since it has no tasks yet). A task on disk still marked
        "running" cannot actually still be running: its thread (and the `claude`
        subprocess under it, for both start_task's background path and
        run_conflict_resolution's blocking one) dies with the old process, and nothing
        else would ever flip its status - unlike sessions.py's "merging" -> "active"
        reset, there was no equivalent here, so a task in flight when the server died
        stayed "running" forever, showing a phantom in-progress card indefinitely."""
        if not self.tasks_dir.exists():
            return
        for path in self.tasks_dir.glob("*.json"):
            task = json.loads(path.read_text())
            if task.get("status") != "running":
                continue
            task["status"] = "error"
            task["finished_at"] = time.time()
            task["result"] = (
                "Interrupted: the PM Studio server restarted while this task was still "
                "running, killing its process before it could finish. If this was a "
                "merge-conflict resolution, check `git status` in the repo by hand - "
                "the merge may still be stuck mid-conflict."
            )
            self._write_task(task)

    def subscribe(self, callback: Callable[[dict], None]) -> None:
        self._subscribers.append(callback)

    def _notify(self, task: dict) -> None:
        for callback in list(self._subscribers):
            callback(task)

    def _task_path(self, task_id: str) -> Path:
        return self.tasks_dir / f"{task_id}.json"

    def _write_task(self, task: dict) -> None:
        self.tasks_dir.mkdir(parents=True, exist_ok=True)
        self._task_path(task["id"]).write_text(json.dumps(task, indent=2))
        self._notify(task)

    def get_task(self, task_id: str) -> dict | None:
        path = self._task_path(task_id)
        if not path.exists():
            return None
        return json.loads(path.read_text())

    def list_tasks(self) -> list[dict]:
        if not self.tasks_dir.exists():
            return []
        tasks = [json.loads(path.read_text()) for path in self.tasks_dir.glob("*.json")]
        tasks.sort(key=lambda t: t["started_at"], reverse=True)
        return tasks

    def _git_head(self, repo: Path | None = None) -> str:
        """Current HEAD sha of the given repo (this registry's worktree by default),
        or "" when git can't say (no repo, no commits yet). Callers must hold
        git_lock - the sha is only meaningful relative to the commits happening
        around it."""
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo or self.repo_root), capture_output=True, text=True,
        )
        return proc.stdout.strip() if proc.returncode == 0 else ""

    def _system_repo_dirs(self, system: str) -> list[str]:
        """Repo-root-relative paths of the system's OWN git repositories inside this
        checkout: the system's `path` when it is itself a git repo, else its immediate
        children that are (the registry convention - one system folder holding e.g.
        `backend/` and `frontend/` clones). Deliberately shallow: a bounded, documented
        rule the operator can predict beats a recursive scan that might adopt a
        node_modules-vendored repo three levels down. Empty when the system declares no
        path, the path is missing (a session worktree materializes gitlinks as empty
        directories), or nothing under it is a repo - all of which fall back to judging
        the deployment repo, the pre-nested behavior."""
        spec = roadmap.SYSTEMS.get(system) if system else None
        if spec is None or not spec.path:
            return []
        base = self.repo_root / spec.path
        if not base.is_dir():
            return []
        if (base / ".git").exists():
            return [spec.path]
        return sorted(
            f"{spec.path}/{child.name}"
            for child in base.iterdir()
            if child.is_dir() and (child / ".git").exists()
        )

    def _repo_states(self, rels: list[str]) -> dict[str, dict]:
        """{rel: {"head": sha, "dirty": bool}} for each nested repo, read under the
        caller's git_lock. `dirty` (uncommitted or untracked files) is recorded because
        the deployment repo's snapshot cannot sweep work inside a nested repo into a
        commit - a task that only dirtied a nested tree would otherwise be invisible."""
        states: dict[str, dict] = {}
        for rel in rels:
            repo = self.repo_root / rel
            status = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=str(repo), capture_output=True, text=True,
            )
            states[rel] = {
                "head": self._git_head(repo),
                "dirty": status.returncode == 0 and bool(status.stdout.strip()),
            }
        return states

    def artifacts_dir(self, task_id: str) -> Path:
        """Where task `task_id` leaves files for the stakeholder (see list_artifacts).
        Under tasks_dir, which is PM bookkeeping that is never committed."""
        return self.tasks_dir / task_id

    def artifact_path(self, task_id: str, name: str) -> Path | None:
        """The file `name` of task `task_id`'s artifacts, or None when there is no such
        task or file - or when `name` would leave that task's folder (`..`, an absolute
        path, a link pointing out of it)."""
        if self.get_task(task_id) is None:
            return None
        base = self.artifacts_dir(task_id).resolve()
        path = (base / name).resolve()
        if not path.is_relative_to(base) or not path.is_file():
            return None
        return path

    def start_task(self, description: str, system: str = "", quick: bool = False) -> dict:
        """Kicks off a dev-agent turn in the background and returns immediately.

        `system` is the declared system whose code this task changes (validated by
        the dispatch endpoint via validate_dispatch_system). head_before is recorded
        now, under the git lock, so the finished task owns an exact commit range
        (head_before..head_after) - the evidence the compliance judge inspects.
        Snapshots from PM turns before or after the task never leak into that range.
        The system's nested repos are read in the background instead, AFTER the
        session's checkout of them exists (see _run_and_record).

        `quick` marks a look-don't-build task (QUICK_TASK_PREAMBLE): shorter timeout,
        same judge - which only runs if something changed anyway."""
        task_id = uuid.uuid4().hex[:8]
        with self.git_lock:
            head_before = self._git_head()
        started_at = time.time()
        task = {
            "id": task_id,
            "kind": "dev",
            "quick": quick,
            "description": description,
            "system": system,
            "status": "running",
            "started_at": started_at,
            "finished_at": None,
            "result": None,
            "head_before": head_before,
        }
        self._write_task(task)
        threading.Thread(
            target=self._run_and_record,
            args=(task_id, description, started_at, system, head_before, None, quick),
            daemon=True,
        ).start()
        return task

    def _prepare_code(self, system: str) -> tuple[list[str], str]:
        """(notes, error) from making `system`'s code present in this worktree. Never
        raises: a failed checkout is reported to the agent and on the record, and the
        agent decides whether the task can still be done."""
        try:
            return ensure_system_code(self.repo_root, system), ""
        except Exception as exc:  # CheckoutError, or git failing in a way it did not expect
            return [], str(exc)

    def run_conflict_resolution(self, description: str) -> dict:
        """Runs a dev-agent turn synchronously (blocking) to resolve a git merge
        conflict already in progress in this registry's workspace. Reuses the
        identical dev-agent subprocess mechanism as an ordinary dev task, just
        invoked inline instead of backgrounded, and does NOT trigger a
        gitsnapshot commit - the merge flow verifies the result and commits (or
        aborts) explicitly."""
        task_id = f"merge-{uuid.uuid4().hex[:8]}"
        started_at = time.time()
        self._write_task({
            "id": task_id, "kind": "merge_resolution", "description": description, "status": "running",
            "started_at": started_at, "finished_at": None, "result": None,
        })
        is_error, result_text, usage = self._execute(description)
        final = {
            "id": task_id,
            "kind": "merge_resolution",
            "description": description,
            "status": "error" if is_error else "done",
            "started_at": started_at,
            "finished_at": time.time(),
            "result": result_text,
            "agent_usage": usage,
        }
        self._write_task(final)
        return final

    def _run_and_record(
        self, task_id: str, description: str, started_at: float,
        system: str = "", head_before: str = "",
        nested_before: dict[str, dict] | None = None,
        quick: bool = False,
    ) -> None:
        checkout_error = ""
        if nested_before is None:
            # A session worktree holds the system's submodules as empty folders until
            # they are checked out; do it before reading their state, so a repo the
            # checkout creates is the task's starting point and not "work it did".
            _, checkout_error = self._prepare_code(system)
            with self.git_lock:
                nested_before = self._repo_states(self._system_repo_dirs(system))
        artifacts_dir = self.artifacts_dir(task_id)
        # The agent's steps as they happen (what it wrote on the way, which tools it used),
        # kept on the running record: writing it is what pushes the card's update, so the
        # stakeholder sees "starting the API", "npm install" instead of a bare "Running".
        progress: list[dict] = []

        def on_progress(event: dict) -> None:
            progress.append({"kind": event["kind"], "text": event["text"][:MAX_STEP_CHARS],
                             "ts": time.time()})
            del progress[:-MAX_PROGRESS_STEPS]
            running = self.get_task(task_id)
            if running is not None and running.get("status") == "running":
                running["progress"] = list(progress)
                self._write_task(running)

        is_error, result_text, usage = self._execute(
            description, system, quick=quick, artifacts_dir=artifacts_dir,
            checkout_error=checkout_error, on_progress=on_progress,
        )
        status_label = "error" if is_error else "done"
        with self.git_lock:
            gitsnapshot.snapshot(f"Dev task {task_id} ({status_label}): {description[:72]}", self.repo_root)
            head_after = self._git_head()
            # Re-list rather than reuse nested_before's keys: a repo the agent cloned
            # into the system's folder during the task is evidence too.
            nested_after = self._repo_states(self._system_repo_dirs(system))
        final = {
            "id": task_id,
            "kind": "dev",
            "quick": quick,
            "description": description,
            "system": system,
            "status": status_label,
            "started_at": started_at,
            "finished_at": time.time(),
            "result": result_text,
            # Measured token spend for this run, for cost attribution.
            "agent_usage": usage,
            "head_before": head_before,
            "head_after": head_after,
        }
        if progress:
            final["progress"] = progress
        artifacts = list_artifacts(artifacts_dir)
        if artifacts:
            final["artifacts"] = artifacts
        if checkout_error:
            final["checkout_error"] = checkout_error
        if nested_before or nested_after:
            final["system_repos_before"] = nested_before
            final["system_repos_after"] = nested_after
        # The judge runs BEFORE the final write: writing "done" is what notifies
        # subscribers, and the completion notification (which re-engages the PM) must
        # already carry the verdict - a PM told "done" without it would build on
        # unjudged work. The card shows "running" a little longer; that's the honest
        # state, since the task isn't settled until it's judged.
        verdict = self._judge(
            system, description, head_before, head_after, nested_before, nested_after
        )
        if verdict is not None:
            final["judge"] = verdict
        self._write_task(final)

    def _judge(
        self, system: str, description: str, head_before: str, head_after: str,
        nested_before: dict[str, dict] | None = None,
        nested_after: dict[str, dict] | None = None,
    ) -> dict | None:
        """The compliance verdict for a finished dev task, or None when there is
        nothing to judge: no system named, the system declares no gitflow rules, or
        nothing observable changed anywhere (errored tasks that DID land changes are
        still judged). Unverifiable-but-changed states are inconclusive, not skipped -
        rules the studio cannot verify should be visible, never silently waved through.

        When the system has its own nested repositories (see _system_repo_dirs), THEY
        are where the rules live, so each one the task touched gets its own judge run
        inside that repo - a committed range when HEAD moved, the dirty tree when the
        agent left work uncommitted - and the verdicts fold worst-wins. The deployment
        repo is deliberately NOT judged alongside them: its only change is the
        registry's own gitlink-bump snapshot, and judging bookkeeping against a
        system's branching rules manufactures false violations. The deployment-repo
        range remains the evidence only when no nested repo was touched - the
        pre-nested behavior, and still right for systems whose source lives in this
        checkout directly."""
        spec = roadmap.SYSTEMS.get(system) if system else None
        if spec is None or not spec.gitflow:
            return None
        try:
            rules = (self.repo_root / spec.gitflow).read_text()
        except OSError as exc:
            return judge.inconclusive(f"Rules file {spec.gitflow} unreadable: {exc}")

        def judged(rel: str, b_head: str, a_head: str, uncommitted: bool) -> tuple[str, dict]:
            return rel, judge.run_judge(
                repo_root=self.repo_root / rel if rel else self.repo_root,
                system_id=system,
                system_label=spec.label,
                gitflow_path=spec.gitflow,
                rules=rules,
                description=description,
                head_before=b_head,
                head_after=a_head,
                fallback_model=self.model,
                repo_rel=rel,
                uncommitted=uncommitted,
            )

        verdicts: list[tuple[str, dict]] = []
        nested_before, nested_after = nested_before or {}, nested_after or {}
        for rel in sorted(set(nested_before) | set(nested_after)):
            before = nested_before.get(rel, {})
            after = nested_after.get(rel, {})
            b_head, a_head = before.get("head", ""), after.get("head", "")
            if rel not in nested_after:
                verdicts.append((rel, judge.inconclusive(
                    f"The repository at {rel} was present when the task started and "
                    "gone when it finished - nothing left to verify the rules against."
                )))
            elif b_head and a_head and b_head != a_head:
                verdicts.append(judged(rel, b_head, a_head, uncommitted=False))
            elif not b_head and a_head and rel not in nested_before:
                verdicts.append((rel, judge.inconclusive(
                    f"The repository at {rel} appeared during the task, so there is no "
                    "before-state to bound what the agent did in it."
                )))
            elif b_head == a_head and after.get("dirty") and not before.get("dirty"):
                verdicts.append(judged(rel, b_head, a_head, uncommitted=True))
        if verdicts:
            return judge.merge_verdicts(verdicts)

        if not head_before or not head_after:
            return judge.inconclusive(
                "No commit range recorded (git rev-parse failed around the task), so "
                "the work cannot be verified against the rules."
            )
        if head_before == head_after:
            return None
        return judged("", head_before, head_after, uncommitted=False)[1]

    def _execute(
        self, description: str, system: str = "", *, quick: bool = False,
        artifacts_dir: Path | None = None, checkout_error: str = "",
        on_progress: Callable[[dict], None] | None = None,
    ) -> tuple[bool, str, dict]:
        """Runs a single headless dev-agent turn in this registry's workspace.
        Returns (is_error, result_text, agent_usage). The usage figures come straight
        from the CLI's own JSON and are MEASURED, not estimated - they are kept apart
        from any labour estimate downstream (see costing.py).

        Shared by the backgrounded start_task path and the synchronous
        conflict-resolution path (which passes no system: a merge resolution isn't
        work on any one system, and the merge flow verifies its result itself).

        The deployment's DEV_INSTRUCTIONS.md (enterprise restrictions, local
        conventions) is appended to the prompt here, at dispatch time, rather than
        stored into the task record - the persisted/displayed description stays
        exactly what the PM wrote, while every dev-agent run (including merge-conflict
        resolution) still carries the local rules. The system's gitflow rules land the
        same way, but LAST and read fresh from this worktree's own copy of the file:
        last so they win over anything above them on conflict (the wrapper says so
        outright rather than relying on position), and read-at-dispatch so an edit to
        the rules takes effect on the very next task.

        `artifacts_dir` (ordinary dev tasks; not merge resolution) is created, named in
        the prompt, exported as PM_STUDIO_ARTIFACTS_DIR and offered to [agents.mcp]
        args as ${PM_STUDIO_ARTIFACTS_DIR} - so e.g. a browser server can write its
        screenshots straight there."""
        prompt = (QUICK_TASK_PREAMBLE if quick else "") + description + UNAVAILABLE_SOURCE_RULE
        if checkout_error:
            prompt += (
                f"\n\n[Note: PM Studio could not check this system's code out into this "
                f"session's worktree: {checkout_error}. Its folders may be empty here; "
                f"report that rather than guessing at the code.]"
            )
        extra_env: dict[str, str] = {}
        if artifacts_dir is not None:
            artifacts_dir.mkdir(parents=True, exist_ok=True)
            extra_env[ARTIFACTS_ENV] = str(artifacts_dir)
            prompt += (
                f"\n\n[Files for the stakeholder: save screenshots and any other evidence "
                f"you want them to see in {artifacts_dir} (also in ${ARTIFACTS_ENV}). "
                f"They are shown on this task's card in the chat - images "
                f"({', '.join(IMAGE_SUFFIXES)}) inline. Give them descriptive file names "
                f"(e.g. login-page.png) and always pass a full ABSOLUTE path inside that "
                f"folder when a tool asks for a file name (a browser screenshot's "
                f"`filename` too): a bare name lands in the worktree, where it does not "
                f"reach the stakeholder and can end up committed. Do not put secrets, "
                f"passwords or real patient data in them.]"
            )
        if CONFIG.dev_instructions:
            prompt += (
                f"\n\n[Project-specific local instructions from "
                f"{LOCAL_DIR_NAME}/{DEV_INSTRUCTIONS_NAME} - these always apply, in "
                f"addition to the task above; where they impose restrictions, follow "
                f"them strictly:]\n{CONFIG.dev_instructions}"
            )
        spec = roadmap.SYSTEMS.get(system) if system else None
        if spec is not None and spec.local is not None and spec.local.services:
            # Only for a system that declares a local environment, so every other
            # dispatch prompt stays byte-identical. The three variables come from
            # agent_env.subprocess_env(); the server owns the processes, so whatever the
            # agent starts outlives this task (and is stopped with the server, or when
            # this session ends). `?session=` keeps the agent on its OWN session's
            # environment - the one running this worktree's branch.
            services = ", ".join(s.id for s in spec.local.services)
            q = "session=$PM_STUDIO_SESSION"
            prompt += (
                f"\n\n[Local environment: PM Studio can run the {spec.label} system "
                f"locally (services: {services}), one environment per chat session, run "
                f"from this session's worktree. Do not launch those services yourself; "
                f"ask the server, which owns the processes and starts dependencies (and "
                f"provisions resources such as a temporary database) first:\n"
                f'  curl -s "$PM_STUDIO_URL/local-env?{q}" -H "$PM_STUDIO_AGENT_HEADER"   (state of every service)\n'
                f'  curl -s -X POST "$PM_STUDIO_URL/local-env/{system}/<service>/start?{q}" -H "$PM_STUDIO_AGENT_HEADER"\n'
                f'  curl -s "$PM_STUDIO_URL/local-env/{system}/<service>/wait?timeout=180&{q}" -H "$PM_STUDIO_AGENT_HEADER"   (blocks until it is up or failed)\n'
                f'  curl -s "$PM_STUDIO_URL/local-env/{system}/<service>/logs?lines=200&{q}" -H "$PM_STUDIO_AGENT_HEADER"\n'
                f"Starting returns at once with state \"starting\"; then call wait, which "
                f"answers the moment the service is \"running\" (or \"failed\"/"
                f"\"port_in_use\", with a message) - never sleep a fixed time instead. An "
                f"environment unused for a while is stopped automatically (its status "
                f"message says so); just start it again. Ports may "
                f"be assigned per session: read each service's \"port\" and \"url\" from the "
                f"status instead of assuming the usual ones. A 409 saying the session-"
                f"environment limit is reached means other sessions are using theirs; report "
                f"it rather than stopping them. A temporary database is optional (by "
                f"default the services use the shared DEV database): create it only when "
                f"the task needs isolated data, with "
                f'curl -s -X POST "$PM_STUDIO_URL/local-env/{system}/resources/<resource>/create?{q}" '
                f'-H "$PM_STUDIO_AGENT_HEADER", then restart the service that uses it '
                f"(its status says \"restart_needed\"). Leave services running when you "
                f"finish unless the task says otherwise.]"
            )
        if spec is not None and spec.gitflow:
            try:
                gitflow_rules = (self.repo_root / spec.gitflow).read_text()
            except OSError as exc:
                # Running anyway would silently drop rules the operator declared
                # non-negotiable. Refuse the task instead, before any agent spend.
                return True, (
                    f"Refusing to run: this task is for the {spec.label} system, whose "
                    f"non-negotiable git workflow rules ({spec.gitflow}) could not be "
                    f"read from this worktree ({exc}). Restore the file (or fix the "
                    f"`gitflow` path in [systems.{system}]) and dispatch again."
                ), {}
            prompt += (
                f"\n\n[NON-NEGOTIABLE git workflow rules for the {spec.label} system, "
                f"from {spec.gitflow}. These are mandatory for every git action in this "
                f"task and override anything above that conflicts with them. Your work "
                f"will be independently verified against them after this task "
                f"finishes:]\n{gitflow_rules}"
            )
        # Every [agents.mcp] server (agent_mcp.py), and only those: --strict-mcp-config
        # makes config.toml the single source. The temp config file lives exactly as
        # long as the agent's process.
        mcp = contextlib.ExitStack()
        mcp_args, _ = mcp.enter_context(
            agent_mcp.AGENT_MCP.launch_args(pm=False, extra_env=extra_env)
        )
        timeout = QUICK_TASK_TIMEOUT_SECONDS if quick else DEV_AGENT_TIMEOUT_SECONDS
        # [models] quick, when set: looking and screenshotting rarely needs the session's
        # biggest model, and a quick task is the stakeholder waiting on an answer.
        model = (models_module.QUICK_MODEL if quick else None) or self.model
        def on_message(message: dict) -> None:
            if on_progress is not None:
                for event in progress_events(message):
                    on_progress(event)

        try:
            returncode, data, stray, stderr = run_agent(
                [
                    "claude",
                    "-p",
                    # stream-json (needs --verbose with -p): each step reaches the task's
                    # card while it runs - see _run_and_record's progress.
                    "--output-format", "stream-json",
                    "--verbose",
                    "--permission-mode", "bypassPermissions",
                    *mcp_args,
                    "--model", model,
                ],
                # On stdin, not argv: Windows caps a command line at 32,767 characters
                # and a task with gitflow rules and dev instructions appended can pass it.
                prompt,
                # Repo root, not workspace_dir: dev agents build product code, and the
                # product sources live at the root - running from the root keeps new
                # files landing in the real source tree instead of inside the PM's
                # bookkeeping folder (which cwd=workspace_dir historically caused).
                # The workspace dir is inside the repo, so no --add-dir is needed to
                # reach it.
                cwd=str(self.repo_root),
                env={**subprocess_env(self.session_id), **extra_env},
                timeout=timeout,
                on_message=on_message,
            )
        except FileNotFoundError:
            return True, "The `claude` CLI is not installed or not on PATH.", {}
        finally:
            mcp.close()

        if returncode is None:
            return True, f"Dev agent timed out after {timeout}s.", {}
        if data is None:
            detail = stray.strip()[:4000] or f"stderr: {stderr.strip()[:2000]}"
            return True, f"No result (exit {returncode}). {detail}", {}
        result_text = data.get("result") or json.dumps(data)[:4000]
        is_error = bool(data.get("is_error")) or returncode != 0
        return is_error, result_text, agent_usage(data)
