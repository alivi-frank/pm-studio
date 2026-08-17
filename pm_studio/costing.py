"""Time and cost attribution.

**This is a distribution mechanism, not a stopwatch.** It does not try to measure how
long anybody worked. It takes a capacity somebody declares - "Dana works 40 hours a
week" - and splits it across projects in proportion to the activity signals that person
generated. The output is the shape a timesheet has:

    Dana, week 2026-W31: 10h Signup rewrite, 20h Billing, 10h Unplanned work

Two consequences of doing it this way, both deliberate:

1. **The totals reconcile.** They always sum to the declared capacity, because capacity
   is the input and signals only decide proportions. Nobody can argue with the
   denominator, which is what makes the numbers usable for chargeback conversations.
2. **It is explicitly an approximation.** A signal count is not a clock. So an admin can
   override any figure, and both the derived and the override are kept - see
   `WeekReport`.

Two cost streams are tracked and **never conflated**:

- **Labor** - distributed human hours x an hourly rate. Estimated.
- **Agent** - token/API spend reported by the Claude CLI. Measured, not estimated.

Conflating them would be wrong in an obvious way: a dev agent grinding for 20 minutes
while its user is at lunch is 20 minutes of machine time and zero minutes of labor.

Rollup follows the work model (see portfolio.py): project totals are exact and additive
up to Initiative. They stop there - an initiative can serve several goals, so
goal-level figures overlap and must never be summed.

Rates, capacities and the roster are the **deployment's own data**: they live in the
consumer's workspace (git-ignored) or their local config, never in this package.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import CONFIG

COSTING_PATH = CONFIG.workspace_dir / "costing.json"
ACTIVITY_PATH = CONFIG.workspace_dir / "activity.jsonl"

DEFAULT_CAPACITY_HOURS = 40.0

# Signal kinds. The weight of each is what turns raw events into proportions; they are
# deliberately in the same ballpark, because the point is a defensible split rather
# than a precise measurement of effort.
KIND_PM_TURN = "pm_turn"
KIND_DEV_TASK = "dev_task"
KIND_REVIEW = "review"
SIGNAL_KINDS = (KIND_PM_TURN, KIND_DEV_TASK, KIND_REVIEW)

# A dispatched dev task represents more human intent than a single chat turn (it is a
# decision to build something), so it carries more weight in the split. These are
# defaults a deployment can tune via [costing] weights.
DEFAULT_WEIGHTS: dict[str, float] = {
    KIND_PM_TURN: 1.0,
    KIND_DEV_TASK: 3.0,
    KIND_REVIEW: 1.0,
}

# Where unattributed effort is parked in a report, when a signal has no project at all.
UNATTRIBUTED = "__unattributed__"


class CostingError(Exception):
    """A rejected costing operation. The message is safe to show a user."""


def agent_usage(data: dict) -> dict:
    """Pulls the measured spend out of a `claude --output-format json` response.

    The CLI has always reported `total_cost_usd` and `usage`; the package simply
    discarded them. This is the *measured* stream - it is never mixed with the
    estimated labour split.
    """
    usage = data.get("usage") or {}
    return {
        "cost_usd": float(data.get("total_cost_usd") or 0.0),
        "input_tokens": int(usage.get("input_tokens") or 0),
        "output_tokens": int(usage.get("output_tokens") or 0),
    }


# ---- weeks ----


def week_key(timestamp: float) -> str:
    """ISO year-week, e.g. "2026-W31". Weeks are the reporting period because that is
    the unit capacity is declared in ("40 hours a week")."""
    moment = datetime.fromtimestamp(timestamp, tz=timezone.utc)
    year, week, _ = moment.isocalendar()
    return f"{year}-W{week:02d}"


def week_bounds(key: str) -> tuple[float, float]:
    """[start, end) epoch seconds for an ISO week key."""
    try:
        year_part, week_part = key.split("-W")
        year, week = int(year_part), int(week_part)
    except (ValueError, AttributeError) as exc:
        raise CostingError(f"Not a valid week: {key!r}. Expected e.g. 2026-W31.") from exc
    try:
        start = datetime.fromisocalendar(year, week, 1).replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise CostingError(f"Not a valid week: {key!r}.") from exc
    return start.timestamp(), (start + timedelta(days=7)).timestamp()


def current_week(now: float | None = None) -> str:
    return week_key(now if now is not None else time.time())


# ---- records ----


@dataclass
class RosterEntry:
    """One person's cost inputs. `rate_per_hour = None` means "use the blended rate",
    which is the whole point of supporting a blended rate: an organization that will
    not put individual salaries in a tool can still get project cost."""

    user_id: str
    rate_per_hour: float | None = None
    capacity_hours_per_week: float = DEFAULT_CAPACITY_HOURS
    updated_at: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "RosterEntry":
        return cls(**data)


@dataclass
class Signal:
    """One activity event. Appended to activity.jsonl and never rewritten."""

    at: float
    user_id: str
    kind: str
    project_id: str | None = None
    session_id: str | None = None
    # Measured agent spend for this event, from the Claude CLI's own JSON output.
    # Zero for a purely human event.
    agent_cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Signal":
        return cls(
            at=float(data.get("at", 0.0)),
            user_id=str(data.get("user_id", "")),
            kind=str(data.get("kind", "")),
            project_id=data.get("project_id"),
            session_id=data.get("session_id"),
            agent_cost_usd=float(data.get("agent_cost_usd", 0.0) or 0.0),
            input_tokens=int(data.get("input_tokens", 0) or 0),
            output_tokens=int(data.get("output_tokens", 0) or 0),
        )


@dataclass
class UserWeek:
    """One person's distributed week."""

    user_id: str
    capacity_hours: float
    rate_per_hour: float | None
    rate_source: str  # "individual" | "blended" | "none"
    # project_id (or UNATTRIBUTED) -> derived hours from the signal split.
    derived_hours: dict[str, float] = field(default_factory=dict)
    # The same map after any admin override for this user/week replaces it.
    effective_hours: dict[str, float] = field(default_factory=dict)
    labor_cost: dict[str, float] = field(default_factory=dict)
    signal_counts: dict[str, int] = field(default_factory=dict)
    overridden: bool = False

    @property
    def total_hours(self) -> float:
        return round(sum(self.effective_hours.values()), 4)

    @property
    def total_labor_cost(self) -> float:
        return round(sum(self.labor_cost.values()), 4)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["total_hours"] = self.total_hours
        data["total_labor_cost"] = self.total_labor_cost
        return data


class CostingStore:
    """Roster, overrides and the activity log for one deployment.

    The roster and overrides live in one server-owned JSON file, the same pattern as the
    other stores. The signal log is separate and append-only (JSONL): it only ever
    grows, so appending is the whole write path and a crash can't corrupt earlier
    entries - the same reasoning as the audit log.
    """

    def __init__(
        self,
        path: Path | None = None,
        activity_path: Path | None = None,
        blended_rate: float | None = None,
        default_capacity_hours: float = DEFAULT_CAPACITY_HOURS,
        weights: dict[str, float] | None = None,
        currency: str = "USD",
    ) -> None:
        self._lock = threading.Lock()
        self._path = path or COSTING_PATH
        self._activity_path = activity_path or ACTIVITY_PATH
        self.blended_rate = blended_rate
        self.default_capacity_hours = default_capacity_hours
        self.weights = {**DEFAULT_WEIGHTS, **(weights or {})}
        self.currency = currency
        self._roster: dict[str, RosterEntry] = {}
        # week -> user_id -> {project_id: hours}
        self._overrides: dict[str, dict[str, dict[str, float]]] = {}
        self._load()

    # ---- persistence ----

    def _load(self) -> None:
        if not self._path.exists():
            return
        raw = json.loads(self._path.read_text())
        self._roster = {
            r["user_id"]: RosterEntry.from_dict(r) for r in raw.get("roster", [])
        }
        self._overrides = raw.get("overrides", {})

    def _save(self) -> None:
        """Caller holds the lock. 0600: rates are compensation data."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "roster": [r.to_dict() for r in self._roster.values()],
            "overrides": self._overrides,
        }
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2))
        tmp.chmod(0o600)
        tmp.replace(self._path)

    # ---- roster ----

    def entry_for(self, user_id: str) -> RosterEntry:
        """Always returns something: an unconfigured person is on the blended rate at
        the default capacity, so a report is never blank just because nobody filled in
        a form."""
        existing = self._roster.get(user_id)
        if existing is not None:
            return existing
        return RosterEntry(
            user_id=user_id,
            rate_per_hour=None,
            capacity_hours_per_week=self.default_capacity_hours,
        )

    def list_roster(self) -> list[dict]:
        return [r.to_dict() for r in sorted(self._roster.values(), key=lambda r: r.user_id)]

    def set_entry(
        self,
        user_id: str,
        rate_per_hour: float | None = None,
        capacity_hours_per_week: float | None = None,
        clear_rate: bool = False,
    ) -> RosterEntry:
        """`clear_rate=True` drops an individual rate back to the blended one. It is a
        separate flag because None already means "leave unchanged" here."""
        with self._lock:
            entry = self._roster.get(user_id) or RosterEntry(
                user_id=user_id, capacity_hours_per_week=self.default_capacity_hours
            )
            if clear_rate:
                entry.rate_per_hour = None
            elif rate_per_hour is not None:
                if rate_per_hour < 0:
                    raise CostingError("A rate cannot be negative.")
                entry.rate_per_hour = float(rate_per_hour)
            if capacity_hours_per_week is not None:
                if not 0 < capacity_hours_per_week <= 168:
                    raise CostingError("Capacity must be between 0 and 168 hours a week.")
                entry.capacity_hours_per_week = float(capacity_hours_per_week)
            entry.updated_at = time.time()
            self._roster[user_id] = entry
            self._save()
            return entry

    def remove_entry(self, user_id: str) -> None:
        with self._lock:
            if self._roster.pop(user_id, None) is not None:
                self._save()

    def _resolve_rate(self, entry: RosterEntry) -> tuple[float | None, str]:
        if entry.rate_per_hour is not None:
            return entry.rate_per_hour, "individual"
        if self.blended_rate is not None:
            return self.blended_rate, "blended"
        # No rate anywhere: hours are still reported, cost is simply unknown. Better
        # than inventing a number.
        return None, "none"

    # ---- overrides ----

    def set_override(self, week: str, user_id: str, hours_by_project: dict[str, float]) -> None:
        """Replaces a person's whole week. Whole-week rather than per-project on
        purpose: the distribution's useful property is that it sums to a real week, and
        overriding one project in isolation would silently break that."""
        week_bounds(week)  # validates
        cleaned: dict[str, float] = {}
        for project_id, hours in hours_by_project.items():
            value = float(hours)
            if value < 0:
                raise CostingError("Hours cannot be negative.")
            if value:
                cleaned[str(project_id)] = value
        with self._lock:
            self._overrides.setdefault(week, {})[user_id] = cleaned
            self._save()

    def clear_override(self, week: str, user_id: str) -> None:
        with self._lock:
            if self._overrides.get(week, {}).pop(user_id, None) is not None:
                if not self._overrides[week]:
                    del self._overrides[week]
                self._save()

    def override_for(self, week: str, user_id: str) -> dict[str, float] | None:
        return self._overrides.get(week, {}).get(user_id)

    # ---- signals ----

    def record(
        self,
        user_id: str,
        kind: str,
        project_id: str | None = None,
        session_id: str | None = None,
        agent_cost_usd: float = 0.0,
        input_tokens: int = 0,
        output_tokens: int = 0,
        at: float | None = None,
    ) -> Signal:
        """Appends one activity signal. Unknown kinds are rejected rather than silently
        counted with zero weight, which would make effort quietly disappear."""
        if kind not in SIGNAL_KINDS:
            raise CostingError(f"Unknown signal kind: {kind!r}")
        # An empty user_id is legitimate and means "machine-only": token spend with
        # nobody at the keyboard. See distribute_week for how it is treated.
        signal = Signal(
            at=at if at is not None else time.time(),
            user_id=user_id,
            kind=kind,
            project_id=project_id,
            session_id=session_id,
            agent_cost_usd=float(agent_cost_usd or 0.0),
            input_tokens=int(input_tokens or 0),
            output_tokens=int(output_tokens or 0),
        )
        with self._lock:
            self._activity_path.parent.mkdir(parents=True, exist_ok=True)
            with self._activity_path.open("a") as handle:
                handle.write(json.dumps(signal.to_dict()) + "\n")
        return signal

    def _iter_signals(self):
        if not self._activity_path.exists():
            return
        for line in self._activity_path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                # A truncated final line (killed mid-append) must not break a report.
                continue
            yield Signal.from_dict(data)

    def signals_in_week(self, week: str) -> list[Signal]:
        start, end = week_bounds(week)
        return [s for s in self._iter_signals() if start <= s.at < end]

    def project_activity(self, recent_since: float) -> dict[str, dict]:
        """Per project: when it was last touched, and how many sessions touched it
        recently. This is the signal log read as ACTIVITY rather than cost - which is
        why it returns only timestamps and counts, never hours, rates or tokens: those
        stay behind the admin-only costing endpoints.

        What it exists for: a project in ideation has no changes by definition, so the
        board cannot read its liveness off the roadmap. But brainstorming with the PM,
        checking data, researching - those are session turns, and every one is already
        recorded here (see server._record_signal). Counting them makes working on ideas
        BE the activity instead of something the board is blind to.

        `last_at` is unbounded (so "last touched 74d ago" stays sayable long after the
        recent window empties); `recent_sessions` counts distinct sessions at or after
        `recent_since`.
        """
        activity: dict[str, dict] = {}
        recent_sessions: dict[str, set] = {}
        for signal in self._iter_signals():
            if not signal.project_id:
                continue
            entry = activity.setdefault(
                signal.project_id, {"last_at": 0.0, "recent_sessions": 0}
            )
            if signal.at > entry["last_at"]:
                entry["last_at"] = signal.at
            if signal.at >= recent_since and signal.session_id:
                recent_sessions.setdefault(signal.project_id, set()).add(signal.session_id)
        for project_id, sessions in recent_sessions.items():
            activity[project_id]["recent_sessions"] = len(sessions)
        return activity

    # ---- the distribution ----

    def distribute_week(self, week: str, user_ids: list[str] | None = None) -> dict:
        """Splits each person's declared capacity across projects by signal share.

        `user_ids` restricts/extends the report to a known roster (so somebody with a
        capacity but no activity still appears, at zero). Without it, only people who
        generated signals show up.
        """
        signals = self.signals_in_week(week)

        weight_by_user: dict[str, dict[str, float]] = {}
        counts_by_user: dict[str, dict[str, int]] = {}
        agent_cost_by_project: dict[str, float] = {}
        tokens_by_project: dict[str, dict[str, int]] = {}

        for signal in signals:
            project = signal.project_id or UNATTRIBUTED
            # A signal with no user is machine-only work - the PM auto-continuing
            # itself after a dev task finishes, with nobody at the keyboard. Its token
            # spend is real and is counted, but it must contribute NO labour weight:
            # charging somebody's week for a turn they didn't take is exactly the
            # "agent runtime is not human attention" error this module exists to avoid.
            if signal.user_id:
                weight = self.weights.get(signal.kind, 1.0)
                weight_by_user.setdefault(signal.user_id, {}).setdefault(project, 0.0)
                weight_by_user[signal.user_id][project] += weight
                counts_by_user.setdefault(signal.user_id, {}).setdefault(project, 0)
                counts_by_user[signal.user_id][project] += 1
            # Agent spend is measured, so it is summed as-is and never distributed.
            if signal.agent_cost_usd:
                agent_cost_by_project[project] = (
                    agent_cost_by_project.get(project, 0.0) + signal.agent_cost_usd
                )
            if signal.input_tokens or signal.output_tokens:
                bucket = tokens_by_project.setdefault(project, {"input": 0, "output": 0})
                bucket["input"] += signal.input_tokens
                bucket["output"] += signal.output_tokens

        people = {u for u in set(weight_by_user) | set(user_ids or []) if u}
        rows: list[UserWeek] = []
        for user_id in sorted(people):
            entry = self.entry_for(user_id)
            rate, rate_source = self._resolve_rate(entry)
            weights = weight_by_user.get(user_id, {})
            total_weight = sum(weights.values())
            derived: dict[str, float] = {}
            if total_weight:
                for project, weight in weights.items():
                    derived[project] = round(
                        entry.capacity_hours_per_week * weight / total_weight, 4
                    )
            override = self.override_for(week, user_id)
            effective = dict(override) if override is not None else dict(derived)
            labor = (
                {p: round(h * rate, 4) for p, h in effective.items()}
                if rate is not None
                else {}
            )
            rows.append(
                UserWeek(
                    user_id=user_id,
                    capacity_hours=entry.capacity_hours_per_week,
                    rate_per_hour=rate,
                    rate_source=rate_source,
                    derived_hours=derived,
                    effective_hours=effective,
                    labor_cost=labor,
                    signal_counts=counts_by_user.get(user_id, {}),
                    overridden=override is not None,
                )
            )

        by_project = self._totals_by_project(rows, agent_cost_by_project)
        return {
            "week": week,
            "currency": self.currency,
            "blended_rate": self.blended_rate,
            "weights": self.weights,
            "signal_count": len(signals),
            "users": [row.to_dict() for row in rows],
            "by_project": by_project,
            "tokens_by_project": tokens_by_project,
            "totals": {
                "hours": round(sum(r.total_hours for r in rows), 4),
                "labor_cost": round(sum(r.total_labor_cost for r in rows), 4),
                "agent_cost": round(sum(agent_cost_by_project.values()), 4),
            },
        }

    @staticmethod
    def _totals_by_project(
        rows: list[UserWeek], agent_cost_by_project: dict[str, float]
    ) -> dict[str, dict]:
        totals: dict[str, dict] = {}
        for row in rows:
            for project, hours in row.effective_hours.items():
                bucket = totals.setdefault(
                    project, {"hours": 0.0, "labor_cost": 0.0, "agent_cost": 0.0}
                )
                bucket["hours"] = round(bucket["hours"] + hours, 4)
                bucket["labor_cost"] = round(
                    bucket["labor_cost"] + row.labor_cost.get(project, 0.0), 4
                )
        for project, cost in agent_cost_by_project.items():
            bucket = totals.setdefault(
                project, {"hours": 0.0, "labor_cost": 0.0, "agent_cost": 0.0}
            )
            bucket["agent_cost"] = round(cost, 4)
        return totals

    def rollup_to_initiatives(self, by_project: dict[str, dict], portfolio) -> dict:
        """Folds project totals up to initiatives, using the work model's unique parent
        path (see portfolio.rollup_path).

        Returns `{"initiatives": {...}, "goal_note": ...}` and deliberately does NOT
        produce goal-level totals: an initiative may serve several goals, so the same
        spend would be counted under each and the totals would stop reconciling. Goal
        attribution is reported per initiative instead, labelled overlapping.
        """
        initiatives: dict[str, dict] = {}
        for project_id, bucket in by_project.items():
            if project_id == UNATTRIBUTED:
                key = UNATTRIBUTED
            else:
                path = portfolio.rollup_path(project_id)
                key = path["initiative_id"] or UNATTRIBUTED
            target = initiatives.setdefault(
                key, {"hours": 0.0, "labor_cost": 0.0, "agent_cost": 0.0, "goal_ids": []}
            )
            target["hours"] = round(target["hours"] + bucket["hours"], 4)
            target["labor_cost"] = round(target["labor_cost"] + bucket["labor_cost"], 4)
            target["agent_cost"] = round(target["agent_cost"] + bucket["agent_cost"], 4)
        for key, bucket in initiatives.items():
            if key != UNATTRIBUTED:
                bucket["goal_ids"] = portfolio.goal_ids_for_initiative(key)
        return {
            "initiatives": initiatives,
            # Carried in the payload so a client cannot render a goal total without
            # having been told, in the same breath, that it would be wrong.
            "goal_note": (
                "Cost is additive up to initiative. An initiative can serve several "
                "goals, so goal figures overlap - never sum them into a total."
            ),
        }
