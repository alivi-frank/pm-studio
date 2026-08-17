import json
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Callable

from . import gitsnapshot, judge, roadmap
from .config import CONFIG, DEV_INSTRUCTIONS_NAME, LOCAL_DIR_NAME
from .costing import agent_usage
from .models import DEFAULT_MODEL

DEV_AGENT_TIMEOUT_SECONDS = 1800


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

    def start_task(self, description: str, system: str = "") -> dict:
        """Kicks off a dev-agent turn in the background and returns immediately.

        `system` is the declared system whose code this task changes (validated by
        the dispatch endpoint via validate_dispatch_system). head_before is recorded
        now, under the git lock, so the finished task owns an exact commit range
        (head_before..head_after) - the evidence the compliance judge inspects.
        Snapshots from PM turns before or after the task never leak into that range."""
        task_id = uuid.uuid4().hex[:8]
        nested_rels = self._system_repo_dirs(system)
        with self.git_lock:
            head_before = self._git_head()
            nested_before = self._repo_states(nested_rels)
        started_at = time.time()
        task = {
            "id": task_id,
            "kind": "dev",
            "description": description,
            "system": system,
            "status": "running",
            "started_at": started_at,
            "finished_at": None,
            "result": None,
            "head_before": head_before,
        }
        if nested_before:
            task["system_repos_before"] = nested_before
        self._write_task(task)
        threading.Thread(
            target=self._run_and_record,
            args=(task_id, description, started_at, system, head_before, nested_before),
            daemon=True,
        ).start()
        return task

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
    ) -> None:
        nested_before = nested_before or {}
        is_error, result_text, usage = self._execute(description, system)
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

    def _execute(self, description: str, system: str = "") -> tuple[bool, str, dict]:
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
        the rules takes effect on the very next task."""
        prompt = description
        if CONFIG.dev_instructions:
            prompt += (
                f"\n\n[Project-specific local instructions from "
                f"{LOCAL_DIR_NAME}/{DEV_INSTRUCTIONS_NAME} - these always apply, in "
                f"addition to the task above; where they impose restrictions, follow "
                f"them strictly:]\n{CONFIG.dev_instructions}"
            )
        spec = roadmap.SYSTEMS.get(system) if system else None
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
        try:
            proc = subprocess.run(
                [
                    "claude",
                    "-p", prompt,
                    "--output-format", "json",
                    "--permission-mode", "bypassPermissions",
                    "--model", self.model,
                ],
                # Repo root, not workspace_dir: dev agents build product code, and the
                # product sources live at the root - running from the root keeps new
                # files landing in the real source tree instead of inside the PM's
                # bookkeeping folder (which cwd=workspace_dir historically caused).
                # The workspace dir is inside the repo, so no --add-dir is needed to
                # reach it.
                cwd=str(self.repo_root),
                capture_output=True,
                text=True,
                timeout=DEV_AGENT_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            return True, f"Dev agent timed out after {DEV_AGENT_TIMEOUT_SECONDS}s.", {}
        except FileNotFoundError:
            return True, "The `claude` CLI is not installed or not on PATH.", {}

        stdout = proc.stdout.strip()
        if not stdout:
            return True, f"No output (exit {proc.returncode}). stderr: {proc.stderr.strip()[:2000]}", {}
        try:
            data = json.loads(stdout)
        except json.JSONDecodeError:
            return proc.returncode != 0, stdout[:4000], {}

        result_text = data.get("result") or json.dumps(data)[:4000]
        is_error = bool(data.get("is_error")) or proc.returncode != 0
        return is_error, result_text, agent_usage(data)
