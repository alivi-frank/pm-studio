"""PM Studio's own activity: agent turns and dev tasks, with their measured spend.

These are the AI-side signals - what the tool's agents did, for whom, at what cost -
so AI investment sits in the same ledger as human effort instead of a separate page.
Read from the costing activity log (append-only JSONL) plus every session's task
records; nothing here is fetched, so the source is always configured.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from ..model import DEFAULT_WEIGHTS, KIND_AI_TURN, Signal, signal_id
from .base import CollectResult


class PMActivitySource:
    id = "pm"
    label = "PM Studio agents"
    category = "agent"

    def __init__(self, workspace_dir: Path, weights: dict[str, float] | None = None) -> None:
        self.workspace_dir = workspace_dir
        self.weights = weights or {}

    @property
    def configured(self) -> bool:
        return True

    def describe(self) -> dict:
        return {"id": self.id, "label": self.label, "category": self.category, "configured": True,
                "detail": "activity.jsonl + session task records"}

    def _iter_activity(self):
        path = self.workspace_dir / "activity.jsonl"
        if not path.is_file():
            return
        with path.open() as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue

    def _iter_tasks(self):
        candidates = [self.workspace_dir / "current" / "tasks"]
        sessions_dir = self.workspace_dir / "sessions"
        if sessions_dir.is_dir():
            for session in sessions_dir.iterdir():
                # A session worktree mirrors the primary layout under its own root.
                for nested in session.glob("*/workspace/current/tasks"):
                    candidates.append(nested)
        for folder in candidates:
            if not folder.is_dir():
                continue
            for path in sorted(folder.glob("*.json")):
                try:
                    yield json.loads(path.read_text())
                except (OSError, json.JSONDecodeError):
                    continue

    def collect(self, since: float, previous: CollectResult | None = None) -> CollectResult:
        result = CollectResult()
        weight = self.weights.get(KIND_AI_TURN, DEFAULT_WEIGHTS[KIND_AI_TURN])
        turns = 0
        cost = 0.0
        for row in self._iter_activity():
            at = float(row.get("at") or 0)
            if at < since:
                continue
            spend = float(row.get("agent_cost_usd") or 0.0)
            turns += 1
            cost += spend
            result.signals.append(Signal(
                id=signal_id("pm", "activity", at, row.get("user_id"), row.get("session_id"), row.get("kind")),
                at=at, source="pm", kind=KIND_AI_TURN,
                actor=str(row.get("user_id") or ""), actor_email="",
                refs=[], repo=None, system=None, weight=weight, minutes=None,
                meta={"activity": row.get("kind") or "pm_turn", "project_id": row.get("project_id"),
                      "session_id": row.get("session_id"), "cost_usd": spend,
                      "input_tokens": int(row.get("input_tokens") or 0),
                      "output_tokens": int(row.get("output_tokens") or 0)},
            ))
        tasks = 0
        for task in self._iter_tasks():
            at = float(task.get("started_at") or task.get("created_at") or 0)
            if not at or at < since:
                continue
            tasks += 1
            usage = task.get("agent_usage") or {}
            spend = float(usage.get("cost_usd") or 0.0)
            cost += spend
            result.signals.append(Signal(
                id=signal_id("pm", "task", task.get("id"), at),
                at=at, source="pm", kind=KIND_AI_TURN,
                actor=str(task.get("dispatched_by") or task.get("user_id") or "agent"), actor_email="",
                refs=[], repo=None, system=task.get("system") or None, weight=weight, minutes=None,
                meta={"activity": "dev_task", "task_id": task.get("id"), "status": task.get("status"),
                      "project_id": task.get("project_id"), "session_id": task.get("session_id"),
                      "cost_usd": spend, "title": str(task.get("description") or "")[:120],
                      "judge": (task.get("judge") or {}).get("verdict") if isinstance(task.get("judge"), dict) else None},
            ))
        result.facts = {"turns": turns, "dev_tasks": tasks, "agent_cost_usd": round(cost, 4), "collected_at": time.time()}
        return result
