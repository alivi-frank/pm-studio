"""Collaboration signals dropped in by any exporter: meetings, email, chat.

There is no API to the company calendar or Teams tenant from here, and there may
never be one that PM Studio should hold credentials for. So the door is a folder:
`<workspace>/signals/inbox/*.json` (an array) or `*.jsonl` (one record per line).
Anything that can write JSON - a Power Automate flow, an Outlook export script, a
Graph API job - can feed it, and the records become first-class signals attributed
exactly like commits: by the ticket keys in their subject or body.

Record shape (all optional except `at` and `kind`):

    {"kind": "meeting" | "message",
     "at": "2026-09-14T15:00:00-04:00",      # ISO or epoch
     "minutes": 30,                            # meeting duration
     "actor": "Ada Lovelace", "email": "ada@example.com",
     "attendees": [{"name": ..., "email": ...}, ...],   # one signal per attendee
     "subject": "NDT-5561 placeholder providers sync",
     "body": "...", "refs": ["jira:NDT-5561"],           # explicit refs win
     "channel": "outlook" | "teams" | "gmail" | ...,
     "id": "stable-external-id"}
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from ..model import DEFAULT_WEIGHTS, KIND_MEETING, KIND_MESSAGE, Signal, extract_ticket_refs, parse_iso, signal_id
from .base import CollectResult


class InboxSource:
    id = "inbox"
    label = "Calendar, email & chat (inbox drop)"
    category = "collaboration"

    def __init__(self, inbox_dir: Path, *, jira_projects: set[str] | None = None, ado_enabled: bool = False, weights: dict[str, float] | None = None) -> None:
        self.inbox_dir = inbox_dir
        self.jira_projects = jira_projects
        self.ado_enabled = ado_enabled
        self.weights = weights or {}

    @property
    def configured(self) -> bool:
        return self.inbox_dir.is_dir() and any(self.inbox_dir.glob("*.json*"))

    def describe(self) -> dict:
        return {"id": self.id, "label": self.label, "category": self.category, "configured": self.configured,
                "detail": f"drop JSON/JSONL exports in {self.inbox_dir.name}/ — see sources/inbox.py for the record shape",
                "path": str(self.inbox_dir)}

    def _iter_records(self):
        if not self.inbox_dir.is_dir():
            return
        for path in sorted(self.inbox_dir.iterdir()):
            try:
                if path.suffix == ".jsonl":
                    with path.open() as handle:
                        for line in handle:
                            line = line.strip()
                            if line:
                                yield path.name, json.loads(line)
                elif path.suffix == ".json":
                    data = json.loads(path.read_text())
                    for row in (data if isinstance(data, list) else [data]):
                        yield path.name, row
            except (OSError, json.JSONDecodeError, ValueError):
                continue

    def collect(self, since: float, previous: CollectResult | None = None) -> CollectResult:
        result = CollectResult()
        count = 0
        for filename, row in self._iter_records():
            if not isinstance(row, dict):
                continue
            raw_at = row.get("at") or row.get("start")
            at = float(raw_at) if isinstance(raw_at, (int, float)) else parse_iso(raw_at)
            if at is None or at < since:
                continue
            kind = KIND_MEETING if str(row.get("kind", "meeting")).lower() == "meeting" else KIND_MESSAGE
            text = f"{row.get('subject', '')}\n{row.get('body', '')}"
            refs = [str(r) for r in (row.get("refs") or []) if isinstance(r, str) and ":" in r]
            if not refs:
                jira, ado = extract_ticket_refs(text, jira_projects=self.jira_projects)
                refs = [f"jira:{k}" for k in jira] + ([f"ado:{i}" for i in ado] if self.ado_enabled else [])
            minutes = row.get("minutes")
            try:
                minutes = float(minutes) if minutes is not None else None
            except (TypeError, ValueError):
                minutes = None
            people = list(row.get("attendees") or [])
            if not people:
                people = [{"name": row.get("actor", ""), "email": row.get("email", "")}]
            ext_id = row.get("id") or signal_id(filename, at, row.get("subject"))
            for person in people:
                if not isinstance(person, dict):
                    person = {"name": str(person)}
                count += 1
                result.signals.append(Signal(
                    id=signal_id("inbox", ext_id, person.get("email") or person.get("name")),
                    at=at, source="inbox", kind=kind,
                    actor=str(person.get("name") or ""), actor_email=str(person.get("email") or "").lower(),
                    refs=refs, repo=None, system=None,
                    weight=self.weights.get(kind, DEFAULT_WEIGHTS[kind]), minutes=minutes if kind == KIND_MEETING else None,
                    meta={"subject": str(row.get("subject") or "")[:160], "channel": row.get("channel") or "", "file": filename},
                ))
        result.facts = {"records": count, "collected_at": time.time()}
        return result
