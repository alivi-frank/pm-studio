"""The work model above the roadmap: Goals, Initiatives and Projects.

The shape is a single-parent chain with exactly one many-to-many relationship, sitting
on top of the roadmap items that already exist:

    Goals  <->  Initiative  ->  Project  ->  Change
                                              `- belongs to exactly ONE Product

- A **Change** is the existing roadmap item (see roadmap.py). This module adds no new
  concept for it; a change simply gains a single `project_id`.
- A **Project** belongs to exactly one Initiative. `initiative_id = None` is a real,
  reportable state called **unaligned** - allowed, so nobody is blocked mid-work, but
  surfaced so it gets fixed.
- An **Initiative** has many Projects and may serve **several Goals**. That is the only
  many-to-many relationship in the model.

Why single-parent below the initiative: it is what makes cost attribution
unambiguous. Change -> Project -> Initiative is a unique path, so those totals are
exact and additive. **The additive tree stops at Initiative**: because an initiative
can serve several goals, the same spend legitimately contributes to more than one
goal, so goal-level figures overlap and must never be summed into a grand total.
`rollup_paths()` returns that unique path per change, and `goal_ids_for_initiative()`
is deliberately separate so a caller cannot accidentally treat goals as additive.

Why Product is not a level: it hangs off the Change, not the tree. That is what gives
the cross-cutting flexibility for free - an initiative spans products because its
projects' changes each carry their own product, and a product appears in many
initiatives for the same reason. No association records needed.

Products are persistent (they come from config). Goals, Initiatives and Projects are
temporary: they open and close, so each carries a lifecycle status.

Storage mirrors roadmap.py and sessions.py: one server-owned JSON file under
`workspace/`, read and written only by the single always-running process, so every
per-session git worktree sees the same portfolio instead of its own drifting copy.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Literal

from .config import CONFIG

PORTFOLIO_PATH = CONFIG.workspace_dir / "portfolio.json"

Lifecycle = Literal["open", "closed"]
LIFECYCLES: tuple[Lifecycle, ...] = ("open", "closed")

# Defaults for the catch-all scaffold. They are only defaults - every deployment names
# its own, and the labels are the operator's, never this package's opinion about how a
# company organizes work.
DEFAULT_MAINTENANCE_GOAL = "Keep the product healthy"
DEFAULT_MAINTENANCE_INITIATIVE = "Maintenance & operations"
DEFAULT_CATCH_ALL_PROJECT = "Unplanned work"


class PortfolioError(Exception):
    """A rejected portfolio operation. The message is safe to show a user."""


def _now() -> float:
    return time.time()


def _new_id() -> str:
    return uuid.uuid4().hex[:8]


@dataclass
class Goal:
    id: str
    title: str
    description: str
    status: Lifecycle
    created_at: float
    updated_at: float
    closed_at: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Goal":
        return cls(**data)


@dataclass
class Initiative:
    id: str
    title: str
    description: str
    status: Lifecycle
    # The one many-to-many edge in the model: an initiative may serve several goals.
    # Empty means it serves none yet, which makes it unaligned.
    goal_ids: list[str] = field(default_factory=list)
    created_at: float = 0.0
    updated_at: float = 0.0
    closed_at: float | None = None
    # True for the always-open initiative that parents the catch-all project. Never
    # closed and never deleted, so unplanned work always has somewhere to land.
    is_maintenance: bool = False

    @property
    def is_unaligned(self) -> bool:
        return not self.goal_ids

    def to_dict(self) -> dict:
        return asdict(self)

    def to_public_dict(self) -> dict:
        data = self.to_dict()
        data["is_unaligned"] = self.is_unaligned
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Initiative":
        return cls(**data)


@dataclass
class Project:
    id: str
    title: str
    description: str
    status: Lifecycle
    # Exactly one initiative, or None. None is the "unaligned" state: permitted (so a
    # bug fix is never blocked on someone inventing a strategy first) but reported.
    initiative_id: str | None = None
    created_at: float = 0.0
    updated_at: float = 0.0
    closed_at: float | None = None
    # The single catch-all. Unplanned work lands here rather than floating unparented.
    # Cannot be deleted; there is at most one.
    is_catch_all: bool = False

    @property
    def is_unaligned(self) -> bool:
        return self.initiative_id is None

    def to_dict(self) -> dict:
        return asdict(self)

    def to_public_dict(self) -> dict:
        data = self.to_dict()
        data["is_unaligned"] = self.is_unaligned
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Project":
        return cls(**data)


class PortfolioStore:
    """Goals, initiatives and projects for one deployment.

    Mirrors RoadmapStore's conventions (single lock, subscriber fan-out, whole-file
    JSON writes) so the two stores behave the same way under the same server.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._lock = threading.Lock()
        self._path = path or PORTFOLIO_PATH
        self._goals: dict[str, Goal] = {}
        self._initiatives: dict[str, Initiative] = {}
        self._projects: dict[str, Project] = {}
        self._subscribers: list[Callable[[dict], None]] = []
        self._load()

    # ---- persistence ----

    def _load(self) -> None:
        if not self._path.exists():
            return
        raw = json.loads(self._path.read_text())
        self._goals = {g["id"]: Goal.from_dict(g) for g in raw.get("goals", [])}
        self._initiatives = {
            i["id"]: Initiative.from_dict(i) for i in raw.get("initiatives", [])
        }
        self._projects = {p["id"]: Project.from_dict(p) for p in raw.get("projects", [])}

    def _save(self) -> None:
        """Caller holds the lock. Written via a temp file and replaced, so a crash
        mid-write cannot leave a half-serialized portfolio behind."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "goals": [g.to_dict() for g in self._sorted(self._goals)],
            "initiatives": [i.to_dict() for i in self._sorted(self._initiatives)],
            "projects": [p.to_dict() for p in self._sorted(self._projects)],
        }
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2))
        tmp.replace(self._path)

    @staticmethod
    def _sorted(records: dict):
        return sorted(records.values(), key=lambda r: (r.created_at, r.id))

    # ---- broadcast (mirrors RoadmapStore) ----

    def subscribe(self, callback: Callable[[dict], None]) -> None:
        self._subscribers.append(callback)

    def _notify(self, event: dict) -> None:
        for callback in list(self._subscribers):
            callback(event)

    # ---- reads ----

    @property
    def is_empty(self) -> bool:
        return not (self._goals or self._initiatives or self._projects)

    def list_goals(self) -> list[dict]:
        return [g.to_dict() for g in self._sorted(self._goals)]

    def list_initiatives(self) -> list[dict]:
        return [i.to_public_dict() for i in self._sorted(self._initiatives)]

    def list_projects(self) -> list[dict]:
        return [p.to_public_dict() for p in self._sorted(self._projects)]

    def snapshot(self) -> dict:
        """Everything, in one payload - what the portfolio page and the roadmap's
        initiative pivot both read."""
        return {
            "goals": self.list_goals(),
            "initiatives": self.list_initiatives(),
            "projects": self.list_projects(),
            "catch_all_project_id": self.catch_all_project_id,
            "unaligned": self.unaligned_report(),
        }

    def get_goal(self, goal_id: str) -> Goal | None:
        return self._goals.get(goal_id)

    def get_initiative(self, initiative_id: str) -> Initiative | None:
        return self._initiatives.get(initiative_id)

    def get_project(self, project_id: str) -> Project | None:
        return self._projects.get(project_id)

    def project_exists(self, project_id: str) -> bool:
        return project_id in self._projects

    @property
    def catch_all_project_id(self) -> str | None:
        for project in self._projects.values():
            if project.is_catch_all:
                return project.id
        return None

    def unaligned_report(self) -> dict:
        """Projects with no initiative, and initiatives serving no goal. Alignment is
        mandatory as a practice, so the system reports the gap rather than blocking the
        work that created it."""
        return {
            "projects": [
                {"id": p.id, "title": p.title}
                for p in self._sorted(self._projects)
                if p.is_unaligned and p.status == "open"
            ],
            "initiatives": [
                {"id": i.id, "title": i.title}
                for i in self._sorted(self._initiatives)
                if i.is_unaligned and i.status == "open"
            ],
        }

    # ---- rollup ----

    def rollup_path(self, project_id: str | None) -> dict:
        """The unique parent chain for a change, given its project.

        Returns project/initiative ids (either may be None when unaligned). Goals are
        deliberately NOT included: an initiative can serve several, so folding them in
        here would invite double-counting downstream. Use
        `goal_ids_for_initiative` explicitly, and never sum across goals.
        """
        if project_id is None:
            return {"project_id": None, "initiative_id": None}
        project = self._projects.get(project_id)
        if project is None:
            return {"project_id": None, "initiative_id": None}
        return {"project_id": project.id, "initiative_id": project.initiative_id}

    def goal_ids_for_initiative(self, initiative_id: str | None) -> list[str]:
        """The goals an initiative serves. Overlapping by design - see this module's
        docstring on why goal-level totals are not additive."""
        if initiative_id is None:
            return []
        initiative = self._initiatives.get(initiative_id)
        return list(initiative.goal_ids) if initiative is not None else []

    def projects_of_initiative(self, initiative_id: str) -> list[str]:
        return [
            p.id for p in self._sorted(self._projects) if p.initiative_id == initiative_id
        ]

    # ---- validation helpers ----

    def _validate_status(self, status: str) -> Lifecycle:
        if status not in LIFECYCLES:
            raise PortfolioError(
                f"Unknown status: {status!r}. Must be one of: {', '.join(LIFECYCLES)}"
            )
        return status  # type: ignore[return-value]

    def _validate_title(self, title: str) -> str:
        cleaned = (title or "").strip()
        if not cleaned:
            raise PortfolioError("A title is required.")
        return cleaned

    def _validate_goal_ids(self, goal_ids) -> list[str]:
        if goal_ids is None:
            return []
        cleaned = []
        for goal_id in goal_ids:
            if goal_id not in self._goals:
                raise PortfolioError(f"Unknown goal: {goal_id}")
            if goal_id not in cleaned:
                cleaned.append(goal_id)
        return cleaned

    def _validate_initiative_id(self, initiative_id: str | None) -> str | None:
        if initiative_id is None or initiative_id == "":
            return None
        if initiative_id not in self._initiatives:
            raise PortfolioError(f"Unknown initiative: {initiative_id}")
        return initiative_id

    # ---- goals ----

    def create_goal(self, title: str, description: str = "") -> Goal:
        with self._lock:
            goal = Goal(
                id=_new_id(),
                title=self._validate_title(title),
                description=(description or "").strip(),
                status="open",
                created_at=_now(),
                updated_at=_now(),
            )
            self._goals[goal.id] = goal
            self._save()
        self._notify({"type": "portfolio_changed", "entity": "goal", "id": goal.id})
        return goal

    def update_goal(
        self,
        goal_id: str,
        title: str | None = None,
        description: str | None = None,
        status: str | None = None,
    ) -> Goal:
        with self._lock:
            goal = self._goals.get(goal_id)
            if goal is None:
                raise PortfolioError(f"Unknown goal: {goal_id}")
            if title is not None:
                goal.title = self._validate_title(title)
            if description is not None:
                goal.description = description.strip()
            if status is not None:
                goal.status = self._validate_status(status)
                goal.closed_at = _now() if goal.status == "closed" else None
            goal.updated_at = _now()
            self._save()
        self._notify({"type": "portfolio_changed", "entity": "goal", "id": goal_id})
        return goal

    def delete_goal(self, goal_id: str) -> None:
        """Refuses while any initiative still serves this goal. Silently unlinking
        would quietly change what those initiatives are for."""
        with self._lock:
            if goal_id not in self._goals:
                raise PortfolioError(f"Unknown goal: {goal_id}")
            serving = [i.title for i in self._initiatives.values() if goal_id in i.goal_ids]
            if serving:
                raise PortfolioError(
                    "Still serving this goal: "
                    + ", ".join(serving)
                    + ". Unlink them first."
                )
            del self._goals[goal_id]
            self._save()
        self._notify({"type": "portfolio_changed", "entity": "goal", "id": goal_id})

    # ---- initiatives ----

    def create_initiative(
        self,
        title: str,
        description: str = "",
        goal_ids: list[str] | None = None,
        is_maintenance: bool = False,
    ) -> Initiative:
        with self._lock:
            initiative = Initiative(
                id=_new_id(),
                title=self._validate_title(title),
                description=(description or "").strip(),
                status="open",
                goal_ids=self._validate_goal_ids(goal_ids),
                created_at=_now(),
                updated_at=_now(),
                is_maintenance=is_maintenance,
            )
            self._initiatives[initiative.id] = initiative
            self._save()
        self._notify(
            {"type": "portfolio_changed", "entity": "initiative", "id": initiative.id}
        )
        return initiative

    def update_initiative(
        self,
        initiative_id: str,
        title: str | None = None,
        description: str | None = None,
        status: str | None = None,
        goal_ids: list[str] | None = None,
    ) -> Initiative:
        with self._lock:
            initiative = self._initiatives.get(initiative_id)
            if initiative is None:
                raise PortfolioError(f"Unknown initiative: {initiative_id}")
            if title is not None:
                initiative.title = self._validate_title(title)
            if description is not None:
                initiative.description = description.strip()
            if status is not None:
                new_status = self._validate_status(status)
                if new_status == "closed" and initiative.is_maintenance:
                    # The catch-all's parent must stay open or unplanned work has
                    # nowhere aligned to land.
                    raise PortfolioError(
                        "The maintenance initiative stays open - it parents the "
                        "catch-all project."
                    )
                initiative.status = new_status
                initiative.closed_at = _now() if new_status == "closed" else None
            if goal_ids is not None:
                initiative.goal_ids = self._validate_goal_ids(goal_ids)
            initiative.updated_at = _now()
            self._save()
        self._notify(
            {"type": "portfolio_changed", "entity": "initiative", "id": initiative_id}
        )
        return initiative

    def delete_initiative(self, initiative_id: str) -> None:
        with self._lock:
            initiative = self._initiatives.get(initiative_id)
            if initiative is None:
                raise PortfolioError(f"Unknown initiative: {initiative_id}")
            if initiative.is_maintenance:
                raise PortfolioError("The maintenance initiative cannot be deleted.")
            children = [
                p.title for p in self._projects.values() if p.initiative_id == initiative_id
            ]
            if children:
                raise PortfolioError(
                    "Projects still belong to this initiative: "
                    + ", ".join(children)
                    + ". Move or delete them first."
                )
            del self._initiatives[initiative_id]
            self._save()
        self._notify(
            {"type": "portfolio_changed", "entity": "initiative", "id": initiative_id}
        )

    # ---- projects ----

    def create_project(
        self,
        title: str,
        description: str = "",
        initiative_id: str | None = None,
        is_catch_all: bool = False,
    ) -> Project:
        with self._lock:
            if is_catch_all and self.catch_all_project_id is not None:
                raise PortfolioError("A catch-all project already exists.")
            project = Project(
                id=_new_id(),
                title=self._validate_title(title),
                description=(description or "").strip(),
                status="open",
                initiative_id=self._validate_initiative_id(initiative_id),
                created_at=_now(),
                updated_at=_now(),
                is_catch_all=is_catch_all,
            )
            self._projects[project.id] = project
            self._save()
        self._notify({"type": "portfolio_changed", "entity": "project", "id": project.id})
        return project

    def update_project(
        self,
        project_id: str,
        title: str | None = None,
        description: str | None = None,
        status: str | None = None,
        initiative_id: str | None = None,
        clear_initiative: bool = False,
    ) -> Project:
        """`initiative_id` re-parents; `clear_initiative=True` makes the project
        unaligned on purpose. The two are separate because None already means "no
        change" for every other field."""
        with self._lock:
            project = self._projects.get(project_id)
            if project is None:
                raise PortfolioError(f"Unknown project: {project_id}")
            if title is not None:
                project.title = self._validate_title(title)
            if description is not None:
                project.description = description.strip()
            if status is not None:
                new_status = self._validate_status(status)
                if new_status == "closed" and project.is_catch_all:
                    raise PortfolioError(
                        "The catch-all project stays open - unplanned work lands there."
                    )
                project.status = new_status
                project.closed_at = _now() if new_status == "closed" else None
            if clear_initiative:
                project.initiative_id = None
            elif initiative_id is not None:
                project.initiative_id = self._validate_initiative_id(initiative_id)
            project.updated_at = _now()
            self._save()
        self._notify({"type": "portfolio_changed", "entity": "project", "id": project_id})
        return project

    def delete_project(self, project_id: str, change_count: int = 0) -> None:
        """`change_count` is supplied by the caller (the roadmap store owns changes, so
        this module never reaches into it) and blocks deleting a project that still has
        work under it."""
        with self._lock:
            project = self._projects.get(project_id)
            if project is None:
                raise PortfolioError(f"Unknown project: {project_id}")
            if project.is_catch_all:
                raise PortfolioError("The catch-all project cannot be deleted.")
            if change_count:
                raise PortfolioError(
                    f"{change_count} change(s) still belong to this project. "
                    "Move them first."
                )
            del self._projects[project_id]
            self._save()
        self._notify({"type": "portfolio_changed", "entity": "project", "id": project_id})

    # ---- the maintenance scaffold ----

    def ensure_maintenance_scaffold(
        self,
        goal_title: str = DEFAULT_MAINTENANCE_GOAL,
        initiative_title: str = DEFAULT_MAINTENANCE_INITIATIVE,
        project_title: str = DEFAULT_CATCH_ALL_PROJECT,
    ) -> dict:
        """Creates the goal + always-open initiative + catch-all project that unplanned
        work rolls up into, and returns their ids.

        This exists because the two rules interact: goals and initiatives are never
        auto-created, but the catch-all project needs a parent. So an operator declares
        this trio once, explicitly, and from then on a bug fix has somewhere aligned to
        land instead of floating. Idempotent - running it again returns what is already
        there rather than making a second catch-all.
        """
        existing_catch_all = self.catch_all_project_id
        if existing_catch_all is not None:
            project = self._projects[existing_catch_all]
            initiative_id = project.initiative_id
            goal_ids = self.goal_ids_for_initiative(initiative_id)
            return {
                "goal_id": goal_ids[0] if goal_ids else None,
                "initiative_id": initiative_id,
                "project_id": project.id,
                "created": False,
            }
        goal = self.create_goal(goal_title, "Keep existing work healthy and unblocked.")
        initiative = self.create_initiative(
            initiative_title,
            "Always open. Parents unplanned work so nothing is left unaligned.",
            goal_ids=[goal.id],
            is_maintenance=True,
        )
        project = self.create_project(
            project_title,
            "Where unplanned changes land when no specific project fits.",
            initiative_id=initiative.id,
            is_catch_all=True,
        )
        return {
            "goal_id": goal.id,
            "initiative_id": initiative.id,
            "project_id": project.id,
            "created": True,
        }
