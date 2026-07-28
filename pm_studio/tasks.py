import json
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Callable

from . import gitsnapshot
from .config import CONFIG, DEV_INSTRUCTIONS_NAME, LOCAL_DIR_NAME
from .models import DEFAULT_MODEL

DEV_AGENT_TIMEOUT_SECONDS = 1800


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

    def start_task(self, description: str) -> dict:
        """Kicks off a dev-agent turn in the background and returns immediately."""
        task_id = uuid.uuid4().hex[:8]
        started_at = time.time()
        task = {
            "id": task_id,
            "kind": "dev",
            "description": description,
            "status": "running",
            "started_at": started_at,
            "finished_at": None,
            "result": None,
        }
        self._write_task(task)
        threading.Thread(
            target=self._run_and_record, args=(task_id, description, started_at), daemon=True
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
        is_error, result_text = self._execute(description)
        final = {
            "id": task_id,
            "kind": "merge_resolution",
            "description": description,
            "status": "error" if is_error else "done",
            "started_at": started_at,
            "finished_at": time.time(),
            "result": result_text,
        }
        self._write_task(final)
        return final

    def _run_and_record(self, task_id: str, description: str, started_at: float) -> None:
        is_error, result_text = self._execute(description)
        final = {
            "id": task_id,
            "kind": "dev",
            "description": description,
            "status": "error" if is_error else "done",
            "started_at": started_at,
            "finished_at": time.time(),
            "result": result_text,
        }
        self._write_task(final)
        status_label = "error" if is_error else "done"
        with self.git_lock:
            gitsnapshot.snapshot(f"Dev task {task_id} ({status_label}): {description[:72]}", self.repo_root)

    def _execute(self, description: str) -> tuple[bool, str]:
        """Runs a single headless dev-agent turn in this registry's workspace.
        Returns (is_error, result_text). Shared by the backgrounded start_task
        path and the synchronous conflict-resolution path.

        The deployment's DEV_INSTRUCTIONS.md (enterprise restrictions, local
        conventions) is appended to the prompt here, at dispatch time, rather than
        stored into the task record - the persisted/displayed description stays
        exactly what the PM wrote, while every dev-agent run (including merge-conflict
        resolution) still carries the local rules."""
        prompt = description
        if CONFIG.dev_instructions:
            prompt += (
                f"\n\n[Project-specific local instructions from "
                f"{LOCAL_DIR_NAME}/{DEV_INSTRUCTIONS_NAME} - these always apply, in "
                f"addition to the task above; where they impose restrictions, follow "
                f"them strictly:]\n{CONFIG.dev_instructions}"
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
            return True, f"Dev agent timed out after {DEV_AGENT_TIMEOUT_SECONDS}s."
        except FileNotFoundError:
            return True, "The `claude` CLI is not installed or not on PATH."

        stdout = proc.stdout.strip()
        if not stdout:
            return True, f"No output (exit {proc.returncode}). stderr: {proc.stderr.strip()[:2000]}"
        try:
            data = json.loads(stdout)
        except json.JSONDecodeError:
            return proc.returncode != 0, stdout[:4000]

        result_text = data.get("result") or json.dumps(data)[:4000]
        is_error = bool(data.get("is_error")) or proc.returncode != 0
        return is_error, result_text
