import json
import re
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Callable, Literal

from .config import CONFIG

# Imported for the one rule that must not be re-implemented per caller: what makes two
# spellings of a ticket key the SAME ticket. The 1:1 guarantee below is only as good as
# that definition, so it lives in one place and the store applies it itself rather than
# trusting callers to normalise first. (trackers.py imports only config - no cycle.)
from .trackers import normalize_key

ROADMAP_DIR = CONFIG.workspace_dir / "roadmap"

Bucket = Literal["now", "next", "later"]
Status = Literal["pending", "in_progress", "done"]

# The product taxonomy for this deployment, from pm_studio_local/config.toml's
# [products] table (TOML order is display order). Empty when the target repo hasn't
# declared products yet - sessions then run unpinned with no per-product boards.
PRODUCTS: dict[str, str] = CONFIG.products

# Schedule dates are stored as "YYYY-MM-DD" STRINGS, unlike created_at/updated_at/
# shipped_at, which are epoch floats. The difference is deliberate and not worth
# "tidying" into one type: those three are instants - the moment something happened -
# while a start or a target is a calendar date somebody committed to. Stored as an
# epoch, a target of 2026-09-30 is really 2026-09-30T00:00:00 in some zone, and renders
# as the 29th for every reader west of it. A date string means the same day everywhere.
_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def parse_date(value: str | None, field: str) -> str | None:
    """Normalises a schedule date for storage. "" (or whitespace) clears it to None;
    anything else must be an ISO calendar date. Raises ValueError naming the field, so
    the message is usable straight back to a PM's curl or the board.

    The regex is not redundant with date.fromisoformat: since 3.11 that also accepts
    "20260930" and full datetimes, and one stored format is what keeps every reader -
    the board, the PM context block, a JSON diff - comparing like with like.
    """
    text = (value or "").strip()
    if not text:
        return None
    if not _DATE_PATTERN.match(text):
        raise ValueError(f"{field} must be a date as YYYY-MM-DD (got {value!r})")
    try:
        date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{field} is not a real date: {text}") from exc
    return text


def _check_order(start: str | None, target: str | None) -> None:
    """Checked against the RESULTING pair, never just the incoming one - a PATCH that
    sets only `start_at` can invert an order the item already had."""
    if start and target and start > target:  # ISO dates sort lexicographically
        raise ValueError(
            f"start_at ({start}) is after target_at ({target}) - a change cannot be "
            "scheduled to finish before it begins"
        )


def _schedule_note(item: dict) -> str:
    """The schedule as a PM reads it in its context block. Says OVERDUE loudly and gives
    the day count, because "target 2026-09-30" next to a date the model has to work out
    for itself is exactly the kind of thing it will get wrong or skip."""
    start, target = item.get("start_at"), item.get("target_at")
    if not start and not target:
        return ""
    if item.get("is_overdue"):
        late = (date.today() - date.fromisoformat(target)).days
        return f" [OVERDUE - target was {target}, {late}d ago]"
    parts = []
    if start:
        parts.append(f"starts {start}")
    if target:
        parts.append(f"target {target}")
        if item.get("status") != "done":
            left = (date.fromisoformat(target) - date.today()).days
            parts.append("due today" if left == 0 else f"{left}d left")
    return f" [{', '.join(parts)}]"


class TicketAlreadyLinked(Exception):
    """Raised when a ticket is already linked to a different roadmap change.

    The link is 1:1 in both directions, and this is the half that needs enforcing: one
    item holds at most one ticket by construction (a single pair of fields), but nothing
    stops two items naming the same ticket except this check. Carries the conflicting
    item so the caller can say *which* change already owns it - "already linked" alone
    would leave the user hunting the board for it.
    """

    def __init__(self, tracker_id: str, ticket_key: str, item: "RoadmapItem") -> None:
        self.tracker_id = tracker_id
        self.ticket_key = ticket_key
        self.item = item
        super().__init__(
            f"{ticket_key} is already linked to the change \"{item.title}\" "
            f"(id {item.id}) on the {item.product} board. A ticket can be linked to "
            "one change only - unlink it there first."
        )


@dataclass
class RoadmapItem:
    id: str
    product: str
    title: str
    description: str
    bucket: Bucket
    status: Status
    # Which product first proposed this item. Equal to `product` for an item a
    # product's own PM created; different when another product's PM suggested it as a
    # cross-product handoff (see RoadmapStore.create) - that's what `triaged` tracks.
    origin_product: str
    # True once the OWNING product's PM (or the stakeholder) has accepted a
    # cross-product suggestion into its real plan. Always True for an item whose
    # origin_product == product (nothing to triage - it's already theirs).
    triaged: bool
    created_at: float
    updated_at: float
    shipped_at: float | None = None
    # Free-text name of the person/team doing this work when it is NOT built through
    # this PM system's dev agents (e.g. "Design team", "Alice"). None/"" = built here.
    # An externally-owned item is tracked on the board - the PM keeps its status
    # current from stakeholder reports but never dispatches dev work for it.
    owner: str | None = None
    # The single parent Project in the work model (see portfolio.py). A change has
    # exactly one, which is what keeps cost attribution unambiguous. None means the
    # change predates the work model or the deployment isn't using it - so existing
    # boards keep loading untouched, and this stays additive rather than a migration.
    project_id: str | None = None
    # The 1:1 link to one ticket in one configured external tracker (see trackers.py).
    # Only the reference is stored - the ticket's type, title, state and URL come from
    # the synced catalog at read time, so a sync updates one place instead of having to
    # rewrite every linked item. Both None (the default) for an unlinked change, which
    # is what keeps existing roadmap JSON loading untouched.
    tracker_id: str | None = None
    ticket_key: str | None = None
    # The schedule, as "YYYY-MM-DD" (see parse_date above). Both optional and both
    # independently optional: a change can have a target with no start ("due by the
    # 30th", a milestone), a start with no target ("began on the 1st, no committed end"),
    # or neither - in which case the timeline falls back to the Now/Next/Later horizon
    # and says so. Undated is the default and stays a first-class state: this board's
    # unit of planning is the horizon, and dates are the sharper thing you reach for
    # when a change actually has a commitment behind it.
    start_at: str | None = None
    target_at: str | None = None

    @property
    def is_overdue(self) -> bool:
        """Past its target and not shipped. Derived, never stored - a stored flag would
        be wrong by morning."""
        return bool(
            self.target_at
            and self.status != "done"
            and self.target_at < date.today().isoformat()
        )

    def to_dict(self) -> dict:
        """The STORED shape - exactly the fields, so `from_dict(to_dict(x)) == x` and
        the JSON on disk round-trips."""
        return asdict(self)

    def to_public_dict(self) -> dict:
        """The shape every reader outside the store gets: stored fields plus what is
        derived from them. Same split as portfolio.py's Initiative/Project, and the
        reason is the same - `is_overdue` must never reach the JSON file, where it would
        be stale by morning and would break `from_dict`."""
        data = self.to_dict()
        data["is_overdue"] = self.is_overdue
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "RoadmapItem":
        return cls(**data)


class RoadmapStore:
    """Shared, server-owned roadmap state: one JSON file per product under
    workspace/roadmap/, deliberately analogous to sessions.py's sessions.json - read
    and written only by this always-running process, never inside a per-session git
    worktree. Every PM session is its own worktree/branch and would otherwise see its
    own stale copy; routing all reads/writes through this single in-process store (via
    HTTP, from a PM's `curl` calls) means every session sees and edits the same board
    instead of a copy that could drift and need merging."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: dict[str, RoadmapItem] = {}
        self._subscribers: list[Callable[[dict], None]] = []
        self._load()

    def _path(self, product: str) -> Path:
        return ROADMAP_DIR / f"{product}.json"

    def _load(self) -> None:
        ROADMAP_DIR.mkdir(parents=True, exist_ok=True)
        for product in PRODUCTS:
            path = self._path(product)
            if not path.exists():
                continue
            for raw in json.loads(path.read_text()):
                item = RoadmapItem.from_dict(raw)
                self._items[item.id] = item

    def _save_product(self, product: str) -> None:
        ROADMAP_DIR.mkdir(parents=True, exist_ok=True)
        items = [i.to_dict() for i in self._items.values() if i.product == product]
        items.sort(key=lambda i: i["created_at"])
        self._path(product).write_text(json.dumps(items, indent=2))

    # ---- broadcast (mirrors tasks.TaskRegistry's / SessionManager's subscriber pattern) ----

    def subscribe(self, callback: Callable[[dict], None]) -> None:
        self._subscribers.append(callback)

    def _notify(self, event: dict) -> None:
        for callback in list(self._subscribers):
            callback(event)

    # ---- reads ----

    def list_all(self) -> dict[str, list[dict]]:
        by_product: dict[str, list[dict]] = {p: [] for p in PRODUCTS}
        for item in self._items.values():
            by_product.setdefault(item.product, []).append(item.to_public_dict())
        for product_items in by_product.values():
            product_items.sort(key=lambda i: i["created_at"])
        return by_product

    def list_product(self, product: str) -> list[dict]:
        return sorted(
            (i.to_public_dict() for i in self._items.values() if i.product == product),
            key=lambda i: i["created_at"],
        )

    def get(self, item_id: str) -> RoadmapItem | None:
        return self._items.get(item_id)

    def list_by_project(self, project_id: str) -> list[dict]:
        return sorted(
            (i.to_public_dict() for i in self._items.values() if i.project_id == project_id),
            key=lambda i: i["created_at"],
        )

    def count_by_project(self, project_id: str) -> int:
        """How many changes hang off one project - what the server checks before
        allowing that project to be deleted."""
        return sum(1 for i in self._items.values() if i.project_id == project_id)

    def item_for_ticket(self, tracker_id: str, ticket_key: str) -> RoadmapItem | None:
        """Which change holds this ticket, if any - the read side of the 1:1 link."""
        wanted = normalize_key(tracker_id, ticket_key)
        return next(
            (
                i
                for i in self._items.values()
                if i.tracker_id == tracker_id
                and i.ticket_key
                and normalize_key(tracker_id, i.ticket_key) == wanted
            ),
            None,
        )

    def linked_ticket_refs(self) -> list[tuple[str, str]]:
        """Every (tracker_id, key) currently linked, for the sync to keep fresh."""
        return [
            (i.tracker_id, i.ticket_key)
            for i in self._items.values()
            if i.tracker_id and i.ticket_key
        ]

    def unassigned_items(self) -> list[dict]:
        """Changes with no parent project. Reported, not blocked: alignment is a
        practice the board surfaces rather than a validation that stops work."""
        return sorted(
            (i.to_public_dict() for i in self._items.values() if i.project_id is None),
            key=lambda i: i["created_at"],
        )

    # ---- writes ----

    def create(
        self,
        product: str,
        title: str,
        description: str = "",
        bucket: Bucket = "later",
        status: Status = "pending",
        origin_product: str | None = None,
        owner: str | None = None,
        project_id: str | None = None,
        start_at: str | None = None,
        target_at: str | None = None,
    ) -> RoadmapItem:
        if product not in PRODUCTS:
            raise ValueError(f"Unknown product: {product}")
        origin = origin_product or product
        if origin not in PRODUCTS:
            raise ValueError(f"Unknown origin_product: {origin}")
        start = parse_date(start_at, "start_at")
        target = parse_date(target_at, "target_at")
        _check_order(start, target)
        now = time.time()
        item = RoadmapItem(
            id=uuid.uuid4().hex[:8],
            product=product,
            title=title,
            description=description,
            bucket=bucket,
            status=status,
            origin_product=origin,
            # Deliberately derived, never taken from caller input - a cross-product
            # suggestion always lands untriaged, whatever the caller's payload says,
            # so a PM can't hand its own item a false "already accepted" status on
            # someone else's board.
            triaged=(origin == product),
            created_at=now,
            updated_at=now,
            shipped_at=now if status == "done" else None,
            owner=(owner or "").strip() or None,
            project_id=(project_id or "").strip() or None,
            start_at=start,
            target_at=target,
        )
        with self._lock:
            self._items[item.id] = item
            self._save_product(product)
        self._notify({"type": "roadmap_item_upserted", "item": item.to_public_dict()})
        return item

    def update(
        self,
        item_id: str,
        bucket: Bucket | None = None,
        status: Status | None = None,
        triaged: bool | None = None,
        title: str | None = None,
        description: str | None = None,
        owner: str | None = None,
        project_id: str | None = None,
        start_at: str | None = None,
        target_at: str | None = None,
    ) -> RoadmapItem:
        with self._lock:
            item = self._items.get(item_id)
            if item is None:
                raise KeyError(f"Unknown roadmap item: {item_id}")
            # Both dates resolved and checked BEFORE anything is written, so a PATCH
            # carrying an impossible schedule leaves the item exactly as it was rather
            # than half-applied. Same convention as owner/project_id: None = no change,
            # "" = clear.
            start = item.start_at if start_at is None else parse_date(start_at, "start_at")
            target = item.target_at if target_at is None else parse_date(target_at, "target_at")
            _check_order(start, target)
            item.start_at = start
            item.target_at = target
            if bucket is not None:
                item.bucket = bucket
            if status is not None:
                item.status = status
                item.shipped_at = time.time() if status == "done" else None
            if triaged is not None:
                item.triaged = triaged
            if title is not None:
                item.title = title
            if description is not None:
                item.description = description
            if owner is not None:
                # "" (an explicit empty string) clears external ownership back to
                # "built here"; None means "no change", like every other field.
                item.owner = owner.strip() or None
            if project_id is not None:
                # Same convention: "" detaches the change from its project (making it
                # unaligned), None leaves it alone.
                item.project_id = project_id.strip() or None
            item.updated_at = time.time()
            self._save_product(item.product)
        self._notify({"type": "roadmap_item_upserted", "item": item.to_public_dict()})
        return item

    def link_ticket(
        self, item_id: str, tracker_id: str, ticket_key: str
    ) -> RoadmapItem:
        """Links a change to one tracker ticket, 1:1.

        Raises TicketAlreadyLinked if a DIFFERENT change already holds it. Re-linking the
        same pair is a no-op rather than an error - a PM re-sending the same PATCH must not
        get a conflict against the item itself.

        The check and the write happen under one lock hold, so two concurrent links to the
        same ticket cannot both pass their check and leave two items pointing at it.
        """
        key = normalize_key(tracker_id, ticket_key)
        if not tracker_id or not key:
            raise ValueError("tracker_id and ticket_key are both required to link a ticket")
        with self._lock:
            item = self._items.get(item_id)
            if item is None:
                raise KeyError(f"Unknown roadmap item: {item_id}")
            for other in self._items.values():
                if other.id == item_id or other.tracker_id != tracker_id or not other.ticket_key:
                    continue
                if normalize_key(tracker_id, other.ticket_key) == key:
                    raise TicketAlreadyLinked(tracker_id, key, other)
            item.tracker_id = tracker_id
            item.ticket_key = key
            item.updated_at = time.time()
            self._save_product(item.product)
        self._notify({"type": "roadmap_item_upserted", "item": item.to_public_dict()})
        return item

    def unlink_ticket(self, item_id: str) -> RoadmapItem:
        """Clears the link. Idempotent: unlinking an unlinked change is not an error, so a
        PM clearing a field it already cleared doesn't fail a turn."""
        with self._lock:
            item = self._items.get(item_id)
            if item is None:
                raise KeyError(f"Unknown roadmap item: {item_id}")
            item.tracker_id = None
            item.ticket_key = None
            item.updated_at = time.time()
            self._save_product(item.product)
        self._notify({"type": "roadmap_item_upserted", "item": item.to_public_dict()})
        return item

    def move(
        self,
        item_id: str,
        to_product: str,
        triaged: bool = False,
        bucket: Bucket | None = None,
        status: Status | None = None,
        title: str | None = None,
        description: str | None = None,
    ) -> RoadmapItem:
        """Reassigns an item to a different product in place - same id/created_at/
        history, unlike a delete+recreate. `origin_product` is set to wherever it just
        moved FROM (so the destination board can show "moved from X" the same way it
        shows "suggested by X"), and it lands untriaged by default - a PM-initiated
        move (see agent.py's ROADMAP_GUIDANCE_TEMPLATE) still needs the destination's
        review, same as any cross-product suggestion. The board UI passes
        triaged=True for a stakeholder-initiated move instead, since a human picking
        the destination directly needs no second confirmation step."""
        with self._lock:
            item = self._items.get(item_id)
            if item is None:
                raise KeyError(f"Unknown roadmap item: {item_id}")
            if to_product not in PRODUCTS:
                raise ValueError(f"Unknown product: {to_product}")
            if to_product == item.product:
                raise ValueError(f"Item {item_id} is already on {to_product}")
            from_product = item.product
            item.origin_product = from_product
            item.product = to_product
            item.triaged = triaged
            if bucket is not None:
                item.bucket = bucket
            if status is not None:
                item.status = status
                item.shipped_at = time.time() if status == "done" else None
            if title is not None:
                item.title = title
            if description is not None:
                item.description = description
            item.updated_at = time.time()
            self._save_product(from_product)
            self._save_product(to_product)
        # Two events, not one "moved" type: every client already knows how to handle
        # a delete (remove from the old product's columns) and an upsert (add to the
        # new one) - no new message type needed on the board's websocket handler.
        self._notify({"type": "roadmap_item_deleted", "id": item_id, "product": from_product})
        self._notify({"type": "roadmap_item_upserted", "item": item.to_public_dict()})
        return item

    def delete(self, item_id: str) -> None:
        with self._lock:
            item = self._items.pop(item_id, None)
            if item is None:
                raise KeyError(f"Unknown roadmap item: {item_id}")
            self._save_product(item.product)
        self._notify({"type": "roadmap_item_deleted", "id": item_id, "product": item.product})

    # ---- PM context injection ----

    def describe_own_product(
        self,
        product: str,
        ticket_lookup: Callable[[str, str], dict | None] | None = None,
    ) -> str:
        """Full-depth view of one product's roadmap - every item, every bucket/status,
        full description - for injection into that product's own PM session. Flags any
        untriaged cross-product suggestion waiting on this PM to accept or drop.

        `ticket_lookup` resolves (tracker_id, key) to the synced ticket so the PM sees what
        each change is tracked as in Jira/ADO. Passed in rather than imported so this store
        keeps knowing nothing about the tracker catalog; omitted (as in tests) the linked
        key is still reported, just without its type.
        """
        items = [i for i in self.list_product(product) if i["status"] != "done"]
        if not items:
            return f"{PRODUCTS[product]} roadmap has no open items right now."
        lines = [f"{PRODUCTS[product]} roadmap (full detail):"]
        for i in items:
            flag = ""
            if i["origin_product"] != product and not i["triaged"]:
                origin_label = PRODUCTS.get(i["origin_product"], i["origin_product"])
                flag = f" [UNTRIAGED suggestion from {origin_label} - accept or drop it]"
            if i.get("owner"):
                flag += (
                    f' [EXTERNAL - owned by {i["owner"]}: track it, never dispatch '
                    "dev work for it]"
                )
            flag += _schedule_note(i)
            if i.get("ticket_key"):
                ticket = (
                    ticket_lookup(i["tracker_id"], i["ticket_key"])
                    if ticket_lookup
                    else None
                )
                if ticket:
                    flag += (
                        f' [tracked as {ticket["raw_type"]} {i["ticket_key"]}'
                        f' ({ticket["state"]}) in {i["tracker_id"]}]'
                    )
                else:
                    flag += f' [linked to {i["ticket_key"]} in {i["tracker_id"]}]'
            lines.append(
                f'- id {i["id"]} [{i["bucket"]}/{i["status"]}] {i["title"]}{flag}\n'
                f'  {i["description"]}'
            )
        return "\n".join(lines)

    def describe_other_products(self, exclude_product: str) -> str:
        """Shallow, title-only digest of every OTHER product's roadmap - general
        awareness without depth: just bucket/status/title, no descriptions. Passing an
        exclude_product that matches nothing (e.g. "") returns a digest of every
        product, for a session with no product of its own."""
        lines = []
        for product in PRODUCTS:
            if product == exclude_product:
                continue
            items = [i for i in self.list_product(product) if i["status"] != "done"]
            if not items:
                continue
            summary = "; ".join(
                f'[{i["bucket"]}/{i["status"]}]'
                + (f' (external: {i["owner"]})' if i.get("owner") else "")
                + f' {i["title"]}'
                # Only the fact of being late travels into another product's digest -
                # this view is awareness, and someone else's slipping date is worth
                # knowing about where their exact start date is not.
                + (" (OVERDUE)" if i.get("is_overdue") else "")
                for i in items
            )
            lines.append(f"- {PRODUCTS[product]}: {summary}")
        return "\n".join(lines)
