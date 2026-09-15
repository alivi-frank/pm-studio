"""The one record everything else is derived from."""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

# Signal kinds. A kind names WHAT happened; `source` names WHERE it was observed.
KIND_COMMIT = "commit"          # a non-merge commit
KIND_MERGE = "merge"            # a merge commit (PR landing, branch sync)
KIND_STATUS = "status"          # ticket workflow transition
KIND_ASSIGNEE = "assignee"      # ticket handed to someone
KIND_COMMENT = "comment"        # ticket comment
KIND_WORKLOG = "worklog"        # explicit logged time (minutes carried)
KIND_FIELD = "field"            # other ticket field edit (estimate, sprint, ...)
KIND_CREATED = "created"        # ticket created
KIND_PR_OPENED = "pr_opened"
KIND_PR_MERGED = "pr_merged"
KIND_PR_REVIEW = "pr_review"
KIND_AI_TURN = "ai_turn"        # a PM Studio agent turn or dev task (cost carried)
KIND_MEETING = "meeting"        # calendar (adapter)
KIND_MESSAGE = "message"        # email / chat (adapter)

# Default effort weight per kind, in minutes-equivalent. These decide the SPLIT of a
# person's day across what they touched - never the total, which is capacity or the
# explicit worklog. Tunable per deployment ([signals.weights]) and by the judge.
DEFAULT_WEIGHTS: dict[str, float] = {
    KIND_COMMIT: 45.0,
    KIND_MERGE: 10.0,
    KIND_STATUS: 15.0,
    KIND_ASSIGNEE: 5.0,
    KIND_COMMENT: 10.0,
    KIND_WORKLOG: 0.0,      # carries its own minutes; weight is not used
    KIND_FIELD: 3.0,
    KIND_CREATED: 10.0,
    KIND_PR_OPENED: 30.0,
    KIND_PR_MERGED: 10.0,
    KIND_PR_REVIEW: 25.0,
    KIND_AI_TURN: 20.0,
    KIND_MEETING: 0.0,      # carries its own minutes
    KIND_MESSAGE: 5.0,
}

# Ticket references in free text. Jira keys are PROJECT-123; ADO ids are bare numbers
# introduced by '#', 'AB#', or the words ticket/task/bug/story/feature - the shapes
# the CapAdmin team actually writes ("ticket #12099", "task #12594", "#12309").
JIRA_KEY_RE = re.compile(r"\b([A-Z][A-Z0-9]{1,9}-\d{1,6})\b")
ADO_ID_RE = re.compile(
    r"(?:\bAB#|(?:\b(?:ticket|task|bug|story|feature|pbi|item|wi)s?\s*#?\s*)|#)(\d{3,6})\b",
    re.IGNORECASE,
)
# Pull request numbers are not tickets: "Merged PR 13309" / "pull request #67".
PR_NUMBER_RE = re.compile(r"\b(?:merged\s+)?(?:PR|pull request)\s*#?\s*(\d{1,6})\b", re.IGNORECASE)
AI_TRAILER_RE = re.compile(
    r"co-authored-by:\s*(claude|copilot|cursor|codex|gpt|gemini|devin|aider)|"
    r"generated with \[?claude|🤖 generated",
    re.IGNORECASE,
)


def extract_ticket_refs(text: str, *, jira_projects: set[str] | None = None) -> tuple[list[str], list[str]]:
    """(jira_keys, ado_ids) mentioned in `text`. PR numbers are stripped first so
    "Merged PR 13309" never yields work item 13309. Jira keys are filtered to the
    projects the deployment syncs when that set is given - "UTF-8" and "SHA-256" are
    not tickets."""
    if not text:
        return [], []
    cleaned = PR_NUMBER_RE.sub(" ", text)
    jira = []
    for key in JIRA_KEY_RE.findall(cleaned):
        project = key.split("-", 1)[0]
        if jira_projects is not None and project not in jira_projects:
            continue
        if key not in jira:
            jira.append(key)
    ado = []
    for ident in ADO_ID_RE.findall(cleaned):
        ident = str(int(ident))
        if ident not in ado:
            ado.append(ident)
    return jira, ado


def is_ai_assisted(text: str) -> bool:
    return bool(AI_TRAILER_RE.search(text or ""))


def signal_id(*parts: object) -> str:
    """Stable id from the parts that make an event unique, so a refresh that re-reads
    the same history never duplicates a signal and feedback keyed on ids survives."""
    raw = "\x1f".join(str(p) for p in parts)
    return hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()[:16]


def parse_iso(value: str | None) -> float | None:
    """ISO-8601 (with offset, 'Z', or Jira's '+0000') to epoch seconds."""
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    # Jira writes +0000 without the colon; fromisoformat wants +00:00 before 3.11.
    if re.search(r"[+-]\d{4}$", text):
        text = text[:-2] + ":" + text[-2:]
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


class Clock:
    """Day/hour bucketing in one zone, so a commit at 23:30 in Miami is Miami's
    evening and not tomorrow morning in UTC."""

    def __init__(self, tz_name: str = "America/New_York") -> None:
        try:
            self.tz = ZoneInfo(tz_name)
        except Exception:  # noqa: BLE001 - an unknown zone must not kill the layer
            self.tz = timezone.utc

    def local(self, at: float) -> datetime:
        return datetime.fromtimestamp(at, self.tz)

    def day(self, at: float) -> str:
        return self.local(at).strftime("%Y-%m-%d")

    def week(self, at: float) -> str:
        iso = self.local(at).isocalendar()
        return f"{iso[0]}-W{iso[1]:02d}"

    def month(self, at: float) -> str:
        return self.local(at).strftime("%Y-%m")

    def hour(self, at: float) -> int:
        return self.local(at).hour

    def weekday(self, at: float) -> int:
        return self.local(at).weekday()

    def day_start(self, day: str) -> float:
        y, m, d = (int(p) for p in day.split("-"))
        return datetime(y, m, d, tzinfo=self.tz).timestamp()

    def day_end(self, day: str) -> float:
        return self.day_start(day) + 86400.0

    def today(self, now: float) -> str:
        return self.day(now)


@dataclass
class Signal:
    """One observable act of engineering.

    `refs` are ticket references as "<tracker_id>:<KEY>" - several when one commit
    names several tickets. `minutes` is explicit time the actor recorded (a worklog, a
    meeting); None for everything inferred. `weight` is the kind's effort weight at
    collection time (kept on the record so a tuning change is visible as a diff, not a
    silent rewrite of history)."""

    id: str
    at: float
    source: str
    kind: str
    actor: str = ""
    actor_email: str = ""
    refs: list[str] = field(default_factory=list)
    repo: str | None = None
    system: str | None = None
    weight: float = 0.0
    minutes: float | None = None
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Signal":
        known = {f: data.get(f) for f in cls.__dataclass_fields__}
        known["refs"] = list(known.get("refs") or [])
        known["meta"] = dict(known.get("meta") or {})
        known["weight"] = float(known.get("weight") or 0.0)
        known["at"] = float(known.get("at") or 0.0)
        known["actor"] = str(known.get("actor") or "")
        known["actor_email"] = str(known.get("actor_email") or "")
        known["kind"] = str(known.get("kind") or "")
        known["source"] = str(known.get("source") or "")
        known["id"] = str(known.get("id") or "")
        return cls(**known)  # type: ignore[arg-type]


def day_range(start_day: str, end_day: str) -> list[str]:
    """Inclusive list of YYYY-MM-DD strings."""
    y, m, d = (int(p) for p in start_day.split("-"))
    y2, m2, d2 = (int(p) for p in end_day.split("-"))
    cur = date(y, m, d)
    last = date(y2, m2, d2)
    out = []
    while cur <= last:
        out.append(cur.isoformat())
        cur = date.fromordinal(cur.toordinal() + 1)
    return out
