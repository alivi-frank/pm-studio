import json
import threading
import time
import uuid
from dataclasses import asdict, dataclass
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

    def to_dict(self) -> dict:
        return asdict(self)

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
            by_product.setdefault(item.product, []).append(item.to_dict())
        for product_items in by_product.values():
            product_items.sort(key=lambda i: i["created_at"])
        return by_product

    def list_product(self, product: str) -> list[dict]:
        return sorted(
            (i.to_dict() for i in self._items.values() if i.product == product),
            key=lambda i: i["created_at"],
        )

    def get(self, item_id: str) -> RoadmapItem | None:
        return self._items.get(item_id)

    def list_by_project(self, project_id: str) -> list[dict]:
        return sorted(
            (i.to_dict() for i in self._items.values() if i.project_id == project_id),
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
            (i.to_dict() for i in self._items.values() if i.project_id is None),
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
    ) -> RoadmapItem:
        if product not in PRODUCTS:
            raise ValueError(f"Unknown product: {product}")
        origin = origin_product or product
        if origin not in PRODUCTS:
            raise ValueError(f"Unknown origin_product: {origin}")
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
        )
        with self._lock:
            self._items[item.id] = item
            self._save_product(product)
        self._notify({"type": "roadmap_item_upserted", "item": item.to_dict()})
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
    ) -> RoadmapItem:
        with self._lock:
            item = self._items.get(item_id)
            if item is None:
                raise KeyError(f"Unknown roadmap item: {item_id}")
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
        self._notify({"type": "roadmap_item_upserted", "item": item.to_dict()})
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
        self._notify({"type": "roadmap_item_upserted", "item": item.to_dict()})
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
        self._notify({"type": "roadmap_item_upserted", "item": item.to_dict()})
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
        self._notify({"type": "roadmap_item_upserted", "item": item.to_dict()})
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
                for i in items
            )
            lines.append(f"- {PRODUCTS[product]}: {summary}")
        return "\n".join(lines)
