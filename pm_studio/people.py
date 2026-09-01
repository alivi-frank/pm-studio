"""Who is doing the work: the people directory and the derived load view.

Jira and ADO each answer "who is on this ticket" in their own vocabulary - Jira with an
account id and a display name, ADO with a `uniqueName` and a display name - and the same
human is routinely both. This module owns the reconciliation: one `Person` per human,
holding every tracker identity that resolves to them, so "what is Dana working on" is one
question rather than one question per tracker.

Four properties it is deliberately built for:

- **The import creates the roster; nobody types it in.** A sync that sees an assignee no
  identity claims creates that person (see `PeopleStore.reconcile`). A directory somebody
  has to populate by hand is a directory that is stale within a week, and the trackers
  already know who is working on what.
- **A person is not an account.** Most assignees will never sign in here. `Person` is a
  name to attribute work to; `account_id` optionally links one to an `accounts.User`, which
  is what lets the costing roster's capacity and rate apply to the same human. The two are
  separate on purpose: requiring an invite before somebody's work could be shown would make
  the board lie about who is doing it.
- **Assignment made here never reaches the tracker.** A linked change's assignee is the
  tracker's fact, joined at read time like its type and state; a local `assignee` on the
  change is this deployment's own planning decision, and where the two disagree BOTH are
  reported (see `effective_assignee`). Nothing in this module writes to a tracker - the
  only write pm_studio makes is `trackers.TrackerStore.push_ticket`.
- **Load is derived, never stored.** `workload` counts open changes out of whatever the
  board says right now. A stored per-person total would be wrong the moment somebody moved
  a card, and a load figure that is wrong is worse than no load figure, because it gets
  used to hand somebody their next piece of work.
"""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable

from .config import CONFIG

PEOPLE_PATH = CONFIG.workspace_dir / "people.json"

# `inactive` is how somebody leaves without their history leaving with them: their name
# still resolves on every change they touched, they are dropped from the assignment
# affordance and from the load table. Deleting them would silently unassign work that
# somebody really did do.
PERSON_STATUSES = ("active", "inactive")

# Where work nobody is on is parked in a load report. A sentinel rather than a null key so
# the row travels through JSON and sorts with the rest - unassigned work in the `now`
# bucket is the single most useful line in the table, and it must never be the row that
# gets dropped for having no id.
UNASSIGNED = "__unassigned__"

# The buckets a load report splits open work by. Imported from roadmap would be a cycle
# (roadmap reads this module for the PM context line), and the horizon vocabulary is
# stable enough that stating it twice costs less than the import knot.
LOAD_BUCKETS = ("now", "next", "later")


class PeopleError(Exception):
    """A rejected directory operation. The message is safe to show a user."""


def normalize_name(name: str) -> str:
    """The form two spellings of one human's display name are compared in.

    Case, punctuation and inner whitespace differ between trackers for the same person
    ("Dana O'Neil" / "dana oneil" / "O'Neil, Dana" stay apart on the last one - word order
    is deliberately NOT normalized, because reordering names is how "Lee Morgan" and
    "Morgan Lee" become one person by accident).
    """
    text = (name or "").replace("'", "").replace("’", "")
    text = re.sub(r"[^\w\s]", " ", text)
    return " ".join(text.split()).casefold()


def normalize_email(email: str) -> str:
    return (email or "").strip().casefold()


def _looks_like_email(value: str) -> bool:
    return "@" in value and "." in value.rsplit("@", 1)[-1]


@dataclass
class Identity:
    """One tracker's name for one human.

    `key` is whatever that tracker offers as a STABLE handle - Jira's `accountId`, ADO's
    `uniqueName` - falling back to the display name when an instance hides both (Jira
    Cloud's privacy setting does exactly that). `display` is kept beside it because it is
    what a human recognises, and because a key like `5b10a2…` is unreadable in a UI.
    """

    tracker_id: str
    key: str
    display: str
    email: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Identity":
        known = {f: data.get(f) for f in cls.__dataclass_fields__}
        return cls(
            tracker_id=str(known.get("tracker_id") or ""),
            key=str(known.get("key") or ""),
            display=str(known.get("display") or ""),
            email=str(known.get("email") or ""),
        )


@dataclass
class Person:
    """One human, however many trackers know them."""

    id: str
    name: str
    email: str = ""
    status: str = "active"
    # The `accounts.User` this person signs in as, when they do at all. None for the
    # majority who never will - see the module docstring.
    account_id: str | None = None
    # "tracker" for somebody a sync discovered, "local" for somebody added here. Kept
    # because it answers "why is this person in my directory" without a guess, and because
    # only a local person with no identities is safe to delete outright.
    source: str = "tracker"
    identities: list[Identity] = field(default_factory=list)
    created_at: float = 0.0
    updated_at: float = 0.0

    @property
    def is_active(self) -> bool:
        return self.status == "active"

    def to_dict(self) -> dict:
        return asdict(self)

    def to_public_dict(self) -> dict:
        """The stored shape plus what is derived from it. Mirrors RoadmapItem's split for
        the same reason: `trackers` must never reach the JSON file, where it would drift
        from `identities` the first time one was added."""
        data = self.to_dict()
        data["trackers"] = sorted({i.tracker_id for i in self.identities})
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Person":
        known = {f: data.get(f) for f in cls.__dataclass_fields__}
        known["identities"] = [
            Identity.from_dict(i) for i in (known.get("identities") or [])
        ]
        known["status"] = (
            known.get("status") if known.get("status") in PERSON_STATUSES else "active"
        )
        known["email"] = str(known.get("email") or "")
        known["source"] = str(known.get("source") or "tracker")
        known["created_at"] = float(known.get("created_at") or 0.0)
        known["updated_at"] = float(known.get("updated_at") or 0.0)
        return cls(**known)  # type: ignore[arg-type]


def effective_assignee(item: dict, ticket: dict | None, resolve) -> dict:
    """Who is on this change, and on whose authority - the read-time join.

    Two sources can name somebody, and they are not equal:

    - the change's own `assignee` (a person id), which is THIS deployment's decision;
    - the linked ticket's assignee, which is the tracker's, reconciled to a person.

    The local one wins when both exist, because a PM who reassigned work here meant it -
    but the disagreement is reported (`conflict`) rather than hidden, since a local
    assignment the tracker has not caught up with is exactly the thing somebody needs to go
    and say out loud. Nothing here writes back; see the module docstring.

    `resolve` maps a person id (or a `(tracker_id, key)` pair, via the store) to a Person.
    Injected so this stays a function of its arguments and the wire shape has one
    definition, used by both the board's join and the PM's context line.
    """
    tracker_name = str((ticket or {}).get("assignee") or "")
    tracker_person = None
    if ticket and (ticket.get("assignee_key") or tracker_name):
        tracker_person = resolve.identity(
            str(ticket.get("tracker_id") or ""),
            str(ticket.get("assignee_key") or "") or tracker_name,
        )
    local_id = (item.get("assignee") or "").strip() or None
    local_person = resolve.person(local_id) if local_id else None
    person = local_person or tracker_person
    return {
        "person_id": person.id if person else None,
        # The stored id survives even when it resolves to nobody (a person deleted out
        # from under a change), so the card can say "assigned to someone unknown" instead
        # of quietly reading as unassigned.
        "assignee_id": local_id,
        "name": person.name if person else "",
        "source": ("local" if local_person else "tracker") if person else None,
        "status": person.status if person else "",
        "tracker_name": tracker_name,
        "conflict": bool(
            local_person
            and tracker_person
            and local_person.id != tracker_person.id
        ),
    }


def workload(changes: Iterable[dict], initiative_of: dict | None = None) -> list[dict]:
    """Load per person, out of changes already joined by `effective_assignee`.

    Open work only. A shipped change is history: counting it would make the person who
    closed the most work look like the person with the least room for more, which is the
    exact inversion of what this table is read for. `shipped` is reported beside the counts
    so the row still says they have been busy.

    Two things come back per person because the ask is two questions at once - *how much*
    (the bucket split, plus overdue and in-flight) and *where* (products, systems and the
    tracker areas their work sits in, each with its own count so the biggest is first).
    Handing somebody work needs both: the person with the lightest `now` column is the
    wrong answer if none of their work has ever been near that area.

    Unassigned open work comes back as its own row (`UNASSIGNED`) rather than being
    dropped. On most boards it is the largest row, and it is the one worth acting on.

    `initiative_of` maps project id -> initiative title, and is what lets the row answer
    the third question a team lead asks after "how much" and "where": *how many separate
    things* is this person carrying. Someone with four open changes across four
    initiatives is in a different position from someone with four in one, and the count
    alone cannot tell them apart. Optional, because the pivot is a portfolio fact and
    this function is passed changes; without it `initiatives` is simply empty.
    """
    rows: dict[str, dict] = {}

    def row(pid: str, name: str, status: str) -> dict:
        entry = rows.get(pid)
        if entry is None:
            entry = rows[pid] = {
                "person_id": pid,
                "name": name,
                "status": status,
                "open": 0,
                "in_progress": 0,
                "overdue": 0,
                "shipped": 0,
                **{b: 0 for b in LOAD_BUCKETS},
                "products": {},
                "systems": {},
                "areas": {},
                "initiatives": {},
            }
        # A person named by one change and unnamed by another (an id that no longer
        # resolves) keeps whichever spelling was resolvable.
        if name and not entry["name"]:
            entry["name"], entry["status"] = name, status
        return entry

    for change in changes:
        assigned = change.get("assigned") or {}
        pid = assigned.get("person_id") or UNASSIGNED
        entry = row(
            pid,
            "" if pid == UNASSIGNED else str(assigned.get("name") or ""),
            "" if pid == UNASSIGNED else str(assigned.get("status") or ""),
        )
        if change.get("status") == "done":
            entry["shipped"] += 1
            continue
        entry["open"] += 1
        bucket = change.get("bucket")
        if bucket in LOAD_BUCKETS:
            entry[bucket] += 1
        if change.get("status") == "in_progress":
            entry["in_progress"] += 1
        if change.get("is_overdue"):
            entry["overdue"] += 1
        if change.get("product"):
            key = str(change["product"])
            entry["products"][key] = entry["products"].get(key, 0) + 1
        if change.get("system"):
            key = str(change["system"])
            entry["systems"][key] = entry["systems"].get(key, 0) + 1
        for area in (change.get("ticket") or {}).get("components") or []:
            if area:
                entry["areas"][area] = entry["areas"].get(area, 0) + 1
        # Unaligned work is deliberately not counted as an initiative: it is the absence
        # of one, and folding it in would inflate the spread of whoever holds the most
        # untriaged work - the opposite of what this number is read for.
        title = (initiative_of or {}).get(change.get("project_id"))
        if title:
            entry["initiatives"][title] = entry["initiatives"].get(title, 0) + 1

    def spread(counts: dict[str, int]) -> list[dict]:
        return [
            {"id": key, "count": n}
            for key, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        ]

    out = []
    for entry in rows.values():
        out.append(
            {
                **entry,
                "products": spread(entry["products"]),
                "systems": spread(entry["systems"]),
                "areas": spread(entry["areas"]),
                "initiatives": spread(entry["initiatives"]),
            }
        )
    # Heaviest first, so the table reads as a queue to rebalance; the unassigned row sorts
    # last whatever its size, because it is a pile to hand out rather than a person's load.
    out.sort(
        key=lambda r: (
            r["person_id"] == UNASSIGNED,
            -r["open"],
            -r["now"],
            r["name"].casefold(),
        )
    )
    return out


class PeopleStore:
    """The directory for one deployment.

    Mirrors RoadmapStore/PortfolioStore conventions (single lock, whole-file JSON write) so
    the three stores behave the same way under the same server.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._lock = threading.Lock()
        self._path = path or PEOPLE_PATH
        self._people: dict[str, Person] = {}
        # Identity -> person id, in the two ways a ticket can name somebody. Rebuilt on
        # every write rather than searched on every read: `identity()` is called once per
        # change on every board read, so a scan of the directory there costs
        # changes x people x identities per request - which on a board of several hundred
        # imported changes and a directory of every assignee two trackers ever had is the
        # kind of quiet quadratic that only shows up in production.
        self._by_key: dict[tuple[str, str], str] = {}
        self._by_display: dict[tuple[str, str], str] = {}
        self._load()

    # ---- persistence ----

    def _load(self) -> None:
        if not self._path.exists():
            return
        raw = json.loads(self._path.read_text())
        self._people = {
            p["id"]: Person.from_dict(p) for p in raw.get("people", []) if p.get("id")
        }
        self._reindex_locked()

    def _reindex_locked(self) -> None:
        """The lookup tables, from scratch. Whole-rebuild rather than incremental upkeep:
        a merge, a split and a reconcile all move identities between people, and three
        hand-maintained deltas is three chances for the index to disagree with the
        directory it is an index of."""
        self._by_key = {}
        self._by_display = {}
        for person in self._people.values():
            for identity in person.identities:
                self._by_key[(identity.tracker_id, identity.key)] = person.id
                display = normalize_name(identity.display)
                if display:
                    # First writer wins, matching the old scan order - two identities on
                    # one tracker sharing a display name are two accounts, and the KEY is
                    # what tells them apart, so the display index is only ever the
                    # fallback for a ticket that carries no key at all.
                    self._by_display.setdefault((identity.tracker_id, display), person.id)

    def _save_locked(self) -> None:
        self._reindex_locked()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(
                {"people": [p.to_dict() for p in self._people.values()]},
                indent=2,
            )
        )

    # ---- reads ----

    def list_people(self) -> list[dict]:
        with self._lock:
            people = list(self._people.values())
        people.sort(key=lambda p: (not p.is_active, p.name.casefold(), p.id))
        return [p.to_public_dict() for p in people]

    def get(self, person_id: str | None) -> Person | None:
        if not person_id:
            return None
        with self._lock:
            return self._people.get(person_id)

    # `effective_assignee` takes a resolver rather than this store, so these two are the
    # adapter - named methods rather than a tuple of lambdas so the shape is greppable.
    def person(self, person_id: str | None) -> Person | None:
        return self.get(person_id)

    def identity(self, tracker_id: str, key: str) -> Person | None:
        """The person a tracker identity resolves to, or None if no sync has seen it.

        Matched on the key first and the display name second: a catalog written before
        assignees were synced carries no key at all, and a Jira instance with private
        profiles never sends one, so a name-only identity has to keep resolving.
        """
        if not tracker_id or not key:
            return None
        with self._lock:
            person_id = self._by_key.get((tracker_id, key)) or self._by_display.get(
                (tracker_id, normalize_name(key))
            )
            return self._people.get(person_id) if person_id else None

    # ---- writes ----

    def reconcile(self, identities: Iterable[tuple[str, str, str, str]]) -> dict:
        """Fold every `(tracker_id, key, display, email)` a sync saw into the directory.

        Returns `{"created": n, "attached": n}` - what a sync pass reports, in the same
        counted-not-guessed shape as the import passes.

        Matching runs most-certain-first, and stops at the first hit:

        1. **The same identity already recorded** - nothing to do.
        2. **Same email.** An address is the one handle two trackers genuinely share.
        3. **Same normalized display name, from a DIFFERENT tracker.** Deliberately never
           within one tracker: two accounts on one Jira with one display name are two
           accounts, and merging them would attribute one person's work to another. Across
           trackers it is the only signal left when an instance hides email addresses, and
           a wrong merge there is repairable by hand (`split_identity`), where a missing one
           silently halves everybody's load.

        Nothing is ever removed: an assignee who stops appearing has left the tracker's
        window, not the history of the work they did.
        """
        report = {"created": 0, "attached": 0}
        now = time.time()
        with self._lock:
            for tracker_id, key, display, email in identities:
                tracker_id = (tracker_id or "").strip()
                key = (key or "").strip()
                display = (display or "").strip()
                email = normalize_email(email)
                if not tracker_id or not (key or display):
                    continue
                key = key or display
                if self._identity_owner_locked(tracker_id, key) is not None:
                    continue
                person = self._match_locked(tracker_id, key, display, email)
                if person is None:
                    person = Person(
                        id=uuid.uuid4().hex[:8],
                        name=display or key,
                        email=email or (key if _looks_like_email(key) else ""),
                        source="tracker",
                        created_at=now,
                        updated_at=now,
                    )
                    self._people[person.id] = person
                    report["created"] += 1
                else:
                    report["attached"] += 1
                person.identities.append(
                    Identity(
                        tracker_id=tracker_id, key=key, display=display, email=email
                    )
                )
                # A directory entry created before an address was known gets one now; an
                # address already there is never overwritten - it may have been corrected
                # by hand, and this pass has no way to know it was.
                if email and not person.email:
                    person.email = email
                person.updated_at = now
                # Within the pass, so the second ticket naming the same new assignee sees
                # the identity the first one created rather than making a second person.
                self._by_key[(tracker_id, key)] = person.id
                if normalize_name(display):
                    self._by_display.setdefault(
                        (tracker_id, normalize_name(display)), person.id
                    )
            if report["created"] or report["attached"]:
                self._save_locked()
        return report

    def _identity_owner_locked(self, tracker_id: str, key: str) -> Person | None:
        # The exact-key index only - a display-name hit is a MATCH to be attached, not an
        # identity already recorded, and treating it as one would drop the new key on the
        # floor and re-match it by name on every future sync.
        person_id = self._by_key.get((tracker_id, key))
        return self._people.get(person_id) if person_id else None

    def _match_locked(
        self, tracker_id: str, key: str, display: str, email: str
    ) -> Person | None:
        candidate_email = email or (key if _looks_like_email(key) else "")
        if candidate_email:
            for person in self._people.values():
                if normalize_email(person.email) == candidate_email:
                    return person
                if any(
                    normalize_email(i.email) == candidate_email
                    for i in person.identities
                ):
                    return person
        needle = normalize_name(display or key)
        if not needle:
            return None
        for person in self._people.values():
            # Same tracker, same name: two accounts. See the docstring.
            if any(i.tracker_id == tracker_id for i in person.identities):
                continue
            if normalize_name(person.name) == needle or any(
                normalize_name(i.display) == needle for i in person.identities
            ):
                return person
        return None

    def create_local(self, name: str, email: str = "") -> Person:
        """Somebody the trackers do not know about: a designer, a partner team's lead, the
        person a locally-planned change is for before the ticket exists."""
        name = (name or "").strip()
        if not name:
            raise PeopleError("A person needs a name.")
        email = normalize_email(email)
        now = time.time()
        with self._lock:
            if email and any(
                normalize_email(p.email) == email for p in self._people.values()
            ):
                raise PeopleError(f"{email} is already in the directory.")
            person = Person(
                id=uuid.uuid4().hex[:8],
                name=name,
                email=email,
                source="local",
                created_at=now,
                updated_at=now,
            )
            self._people[person.id] = person
            self._save_locked()
        return person

    def update(
        self,
        person_id: str,
        *,
        name: str | None = None,
        email: str | None = None,
        status: str | None = None,
        account_id: str | None = None,
    ) -> Person:
        """Every field optional; None leaves it alone. `email` and `account_id` follow the
        ""-clears convention the roadmap store uses for `owner`."""
        with self._lock:
            person = self._people.get(person_id)
            if person is None:
                raise PeopleError(f"Unknown person: {person_id}")
            if name is not None:
                cleaned = name.strip()
                if not cleaned:
                    raise PeopleError("A person needs a name.")
                person.name = cleaned
            if email is not None:
                person.email = normalize_email(email)
            if status is not None:
                if status not in PERSON_STATUSES:
                    raise PeopleError(
                        f"Unknown status: {status!r}. Must be one of: "
                        + ", ".join(PERSON_STATUSES)
                    )
                person.status = status
            if account_id is not None:
                person.account_id = account_id.strip() or None
            person.updated_at = time.time()
            self._save_locked()
        return person

    def merge(self, into_id: str, from_id: str) -> Person:
        """Fold one directory entry into another and delete the emptied one.

        The repair for a reconciliation that could not know: two entries that are one
        human, because both trackers hid the email and the display names differ. Identities
        move; the surviving entry keeps its own name, email and account link, since the
        caller picked which of the two to keep by choosing `into_id`.

        Local ASSIGNMENTS pointing at the absorbed id are not this store's to rewrite - the
        caller re-points them (see server.py's merge endpoint), because the changes live in
        the roadmap store and a half-done merge must fail loudly there rather than quietly
        here.
        """
        if into_id == from_id:
            raise PeopleError("Cannot merge a person into themselves.")
        with self._lock:
            into = self._people.get(into_id)
            source = self._people.get(from_id)
            if into is None:
                raise PeopleError(f"Unknown person: {into_id}")
            if source is None:
                raise PeopleError(f"Unknown person: {from_id}")
            existing = {(i.tracker_id, i.key) for i in into.identities}
            for identity in source.identities:
                if (identity.tracker_id, identity.key) not in existing:
                    into.identities.append(identity)
            if not into.email and source.email:
                into.email = source.email
            if into.account_id is None:
                into.account_id = source.account_id
            into.updated_at = time.time()
            del self._people[from_id]
            self._save_locked()
        return into

    def split_identity(self, person_id: str, tracker_id: str, key: str) -> Person:
        """Pull one tracker identity out of a person and give it its own entry.

        The other half of the repair: a name-match across trackers that turned out to be
        two different people. The new entry keeps the identity's own display name, so the
        board immediately reads the way the tracker does.
        """
        now = time.time()
        with self._lock:
            person = self._people.get(person_id)
            if person is None:
                raise PeopleError(f"Unknown person: {person_id}")
            match = next(
                (
                    i
                    for i in person.identities
                    if i.tracker_id == tracker_id and i.key == key
                ),
                None,
            )
            if match is None:
                raise PeopleError(
                    f"{person.name} has no {tracker_id} identity {key!r} to split off."
                )
            if len(person.identities) == 1:
                raise PeopleError(
                    f"{match.display or key} is {person.name}'s only tracker identity - "
                    "there is nothing to split it away from."
                )
            person.identities.remove(match)
            person.updated_at = now
            split = Person(
                id=uuid.uuid4().hex[:8],
                name=match.display or key,
                email=match.email,
                source="tracker",
                identities=[match],
                created_at=now,
                updated_at=now,
            )
            self._people[split.id] = split
            self._save_locked()
        return split

    def delete(self, person_id: str) -> None:
        """Only ever for a local entry no tracker knows about. Anybody a sync discovered
        would simply come back on the next pass, so `inactive` is the honest way to retire
        them - see PERSON_STATUSES."""
        with self._lock:
            person = self._people.get(person_id)
            if person is None:
                raise PeopleError(f"Unknown person: {person_id}")
            if person.identities:
                raise PeopleError(
                    f"{person.name} is linked to "
                    f"{len(person.identities)} tracker identity(ies) and would return on "
                    "the next sync. Set them inactive instead."
                )
            del self._people[person_id]
            self._save_locked()
