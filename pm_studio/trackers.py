"""External issue trackers (Jira, Azure DevOps): the synced ticket catalog.

A roadmap change can be linked 1:1 to one ticket in one configured tracker. The link
itself lives on the roadmap item (`tracker_id` + `ticket_key`, see roadmap.py); this
module owns everything *about* the ticket - its type, title, state and URL - and it is
the only place that talks to a tracker's API.

Why the split, rather than copying the type onto the roadmap item: the type is the
tracker's fact, not ours. Denormalising it would mean every sync had to rewrite every
linked item and a missed write would leave a card claiming "Bug" for something long since
converted to a Story. Instead the item stores only the link, this store holds one entry
per ticket, and the join happens at read time (server.py's `_with_ticket`). One sync
updates one place.

Three deliberate properties:

- **The network is a seam, not a scattering.** Every HTTP call goes through the module
  level `_request`, which tests replace. No test needs a live Jira.
- **A sync never raises at the caller.** Each tracker's outcome - ok, or an error string -
  is recorded in its own status entry and surfaced in the UI. A tracker being down must
  not take out the roadmap board, which is useful without it.
- **Tokens never leave this module.** They go into an Authorization header and nowhere
  else: not into the catalog, not into a status entry, not into an error message. See
  `_scrub`, which is applied to every error before it is stored.
"""

from __future__ import annotations

import base64
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .config import CONFIG, TrackerConfig

CATALOG_PATH = CONFIG.workspace_dir / "trackers.json"

HTTP_TIMEOUT_SECONDS = 20
# Bounds on one sync. A tracker with a hundred thousand issues must not be able to hang the
# sync thread or blow up memory; hitting the cap sets `truncated` on the tracker's status,
# which the board reports, rather than silently trimming its view.
#
# 150 pages rather than the 40 this started at: a mid-size Jira project sits in the low
# thousands of issues, which brushed against a 4,000 cap - and a limit a normal project
# reaches is a limit that will start quietly dropping the oldest tickets out of the link
# picker. At roughly 4ms per ticket a full 15,000 is about a minute on a background thread,
# affordable at a 15-minute interval; memory is a few MB of dicts.
PAGE_SIZE = 100
MAX_PAGES = 150
MAX_TICKETS_PER_TRACKER = PAGE_SIZE * MAX_PAGES

# ---- canonical types -------------------------------------------------------------
#
# Jira and ADO disagree about names (ADO's "Product Backlog Item" and Jira's "Story" are
# the same rung; ADO has "Feature" between Epic and Story, Jira usually does not), and a
# deployment can add custom types on top. So a raw type name is normalised onto this
# small vocabulary for colour-coding, while `raw_type` keeps whatever the tracker
# actually said - that is what the UI shows as the label, so a custom type is never
# relabelled into a lie.
#
# The colours themselves live in roadmap.html keyed on these slugs: one palette, defined
# where light/dark variants are expressible.

TYPE_EPIC = "epic"
TYPE_FEATURE = "feature"
TYPE_STORY = "story"
TYPE_TASK = "task"
TYPE_BUG = "bug"
TYPE_SPIKE = "spike"
TYPE_SUBTASK = "subtask"
TYPE_OTHER = "other"

CANONICAL_TYPES: tuple[str, ...] = (
    TYPE_EPIC,
    TYPE_FEATURE,
    TYPE_STORY,
    TYPE_TASK,
    TYPE_BUG,
    TYPE_SPIKE,
    TYPE_SUBTASK,
    TYPE_OTHER,
)

# Lowercased raw type name -> canonical slug. Covers the Jira and ADO defaults plus the
# spellings teams commonly rename them to.
TYPE_ALIASES: dict[str, str] = {
    # Epic level
    "epic": TYPE_EPIC,
    "initiative": TYPE_EPIC,
    "theme": TYPE_EPIC,
    # Feature level (ADO ships this rung; Jira instances often add it)
    "feature": TYPE_FEATURE,
    "new feature": TYPE_FEATURE,
    "capability": TYPE_FEATURE,
    "improvement": TYPE_FEATURE,
    "enhancement": TYPE_FEATURE,
    # Story level
    "story": TYPE_STORY,
    "user story": TYPE_STORY,
    "product backlog item": TYPE_STORY,
    "requirement": TYPE_STORY,
    # Task level
    "task": TYPE_TASK,
    "chore": TYPE_TASK,
    "issue": TYPE_TASK,
    "work item": TYPE_TASK,
    # Defects
    "bug": TYPE_BUG,
    "defect": TYPE_BUG,
    "incident": TYPE_BUG,
    "problem": TYPE_BUG,
    # Research
    "spike": TYPE_SPIKE,
    "research": TYPE_SPIKE,
    "investigation": TYPE_SPIKE,
    # Children
    "subtask": TYPE_SUBTASK,
    "sub-task": TYPE_SUBTASK,
    "sub task": TYPE_SUBTASK,
    "test case": TYPE_SUBTASK,
}


def canonical_type(raw_type: str | None) -> str:
    """Maps a tracker's own type name onto CANONICAL_TYPES.

    Unknown names land on "other" rather than being dropped or guessed at - paired with
    `raw_type` on the Ticket, that renders as a neutrally-coloured badge still carrying
    the tracker's own label, which is the honest outcome for a custom issue type.
    """
    name = (raw_type or "").strip().lower()
    if not name:
        return TYPE_OTHER
    if name in TYPE_ALIASES:
        return TYPE_ALIASES[name]
    # Second pass for compounds like "Bug (Production)" or "Technical Task".
    for alias, slug in TYPE_ALIASES.items():
        if re.search(rf"\b{re.escape(alias)}\b", name):
            return slug
    return TYPE_OTHER


@dataclass
class Ticket:
    """One ticket as of the last successful sync of its tracker."""

    tracker_id: str
    provider: str
    key: str
    # Canonical slug (CANONICAL_TYPES) used for the colour code.
    type: str
    # What the tracker itself calls this type, shown as the badge label.
    raw_type: str
    title: str
    state: str
    url: str
    synced_at: float
    # Where this ticket sits in the tracker's own hierarchy. Both None for a top-level
    # item, and additive with None defaults so a cache written before this existed still
    # loads. `parent_type` is the tracker's raw type name for the parent, which is what
    # lets a caller tell "my parent is an Epic" from "my parent is a Story" - a distinction
    # the import rules turn on, since an Epic becomes a project and a Story a change.
    parent_key: str | None = None
    parent_type: str | None = None
    # Jira's status *category* ("To Do" / "In Progress" / "Done"), which is stable across
    # the many workflow-specific names in `state` and is therefore what a bucket/status
    # mapping should key on.
    state_category: str = ""
    # The tracker-side taxonomy this ticket carries: Jira components (several), or the
    # ADO area path (one). What import routes match on (see TrackerConfig.routes). A
    # list, not a tuple, so to_dict/from_dict round-trip through JSON unchanged; empty
    # and additive-by-default so a cache written before this existed still loads.
    components: list[str] = field(default_factory=list)
    # Which tracker project the ticket belongs to - the other thing routes match on,
    # for the project-IS-the-product shape. Jira: the key's prefix; ADO: the project
    # the work item was queried from (its numeric id says nothing). Additive default
    # for pre-existing caches, same as components.
    project: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Ticket":
        known = {f: data.get(f) for f in cls.__dataclass_fields__}
        known["synced_at"] = float(known.get("synced_at") or 0.0)
        # A catalog cached before components existed stores nothing for it; None must
        # come back as "no components", not as a None that breaks every iteration.
        known["components"] = list(known.get("components") or [])
        known["project"] = str(known.get("project") or "")
        return cls(**known)  # type: ignore[arg-type]


@dataclass
class SyncStatus:
    """Per-tracker outcome of the last sync attempt, shown in the board header so a
    stale or failing tracker is visible rather than looking like an empty one."""

    tracker_id: str
    label: str
    provider: str
    configured: bool = True
    ok: bool = False
    ticket_count: int = 0
    started_at: float | None = None
    finished_at: float | None = None
    error: str | None = None
    # Set when a tracker has more tickets than MAX_TICKETS_PER_TRACKER.
    truncated: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


class TrackerError(Exception):
    """A tracker call failed. Message is already scrubbed of credentials."""


# ---- HTTP ------------------------------------------------------------------------


def _scrub(text: str, *secrets: str) -> str:
    """Removes credentials from anything destined for a log, an error field or the API.

    Belt and braces: the token is only ever put in a header, so it should not appear in
    a URL or an exception in the first place. But urllib errors quote the request, a
    misconfigured base_url could embed userinfo, and a status entry is served to the
    browser - so every error is passed through here before it is stored.
    """
    cleaned = text
    for secret in secrets:
        if secret and len(secret) >= 4:
            cleaned = cleaned.replace(secret, "***")
    # Strip any `user:pass@host` userinfo an operator put in base_url.
    cleaned = re.sub(r"://[^/\s@]+:[^/\s@]+@", "://***@", cleaned)
    return cleaned


def _request(
    url: str,
    *,
    headers: dict[str, str],
    method: str = "GET",
    body: dict | None = None,
    timeout: int = HTTP_TIMEOUT_SECONDS,
) -> dict:
    """The single network seam for this module; tests replace it wholesale.

    Returns the decoded JSON object. Raises TrackerError with an HTTP status prefix on
    failure so callers can branch on "404/410 -> try the older API shape".
    """
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers = {**headers, "Content-Type": "application/json"}
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        raise TrackerError(f"HTTP {exc.code}: {detail or exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise TrackerError(f"could not reach {urllib.parse.urlsplit(url).netloc}: {exc.reason}") from exc
    except TimeoutError as exc:
        raise TrackerError(f"timed out after {timeout}s") from exc
    if not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise TrackerError(f"tracker returned non-JSON ({exc})") from exc
    return parsed if isinstance(parsed, dict) else {"value": parsed}


def _http_status(error: TrackerError) -> int | None:
    match = re.match(r"HTTP (\d{3})", str(error))
    return int(match.group(1)) if match else None


def _basic_auth(username: str, token: str) -> str:
    raw = f"{username}:{token}".encode()
    return "Basic " + base64.b64encode(raw).decode()


# ---- providers -------------------------------------------------------------------


class JiraClient:
    """Jira Cloud and Server/DC.

    Two search APIs are tried in order, because Atlassian replaced one with the other and
    a deployment may be on either: the token-paginated `/rest/api/3/search/jql` (current
    Cloud), then the offset-paginated `/rest/api/{3,2}/search` (Server/DC, older Cloud).
    A 404/410 on the first is the documented signal that an instance does not have it.
    """

    JQL_FIELDS = "summary,issuetype,status,parent,components"

    def __init__(self, config: TrackerConfig) -> None:
        self.config = config

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": _basic_auth(self.config.username, self.config.token),
            "Accept": "application/json",
        }

    def _jql(self) -> str:
        projects = ", ".join(f'"{p}"' for p in self.config.projects)
        # `since` bounds the pull by last activity, not creation: an old ticket still
        # being worked is current planning material, a recently-created-then-abandoned
        # one ages out on its own. Config validated the date shape at load.
        window = f' AND updated >= "{self.config.since}"' if self.config.since else ""
        return f"project in ({projects}){window} ORDER BY updated DESC"

    def browse_url(self, key: str) -> str:
        return f"{self.config.base_url}/browse/{key}"

    def _ticket(self, issue: dict, now: float) -> Ticket | None:
        key = str(issue.get("key") or "").strip().upper()
        if not key:
            return None
        fields = issue.get("fields") or {}
        raw_type = str((fields.get("issuetype") or {}).get("name") or "").strip()
        status = fields.get("status") or {}
        # Jira nests the parent's own fields, so one search call yields the parent's type
        # too - no second round trip per issue just to learn whether it is an Epic.
        parent = fields.get("parent") or {}
        parent_fields = parent.get("fields") or {}
        return Ticket(
            tracker_id=self.config.id,
            provider="jira",
            key=key,
            type=canonical_type(raw_type),
            raw_type=raw_type or "Unknown",
            title=str(fields.get("summary") or "").strip(),
            state=str(status.get("name") or "").strip(),
            url=self.browse_url(key),
            synced_at=now,
            parent_key=(str(parent.get("key")).strip().upper() if parent.get("key") else None),
            parent_type=(
                str((parent_fields.get("issuetype") or {}).get("name") or "").strip() or None
            ),
            state_category=str((status.get("statusCategory") or {}).get("name") or "").strip(),
            components=[
                str(c.get("name") or "").strip()
                for c in (fields.get("components") or [])
                if str(c.get("name") or "").strip()
            ],
            # "PROJ-123" belongs to project "PROJ" - the key encodes it, no extra field
            # needed from the API.
            project=key.rsplit("-", 1)[0] if "-" in key else "",
        )

    def fetch_catalog(self) -> tuple[list[Ticket], bool]:
        """Every issue in the configured projects. Returns (tickets, truncated)."""
        try:
            return self._fetch_token_paginated()
        except TrackerError as exc:
            if _http_status(exc) not in (404, 410):
                raise
        # Older instance: fall back to the offset-paginated endpoint, v3 then v2.
        last_error: TrackerError | None = None
        for version in (3, 2):
            try:
                return self._fetch_offset_paginated(version)
            except TrackerError as exc:
                if _http_status(exc) not in (404, 410):
                    raise
                last_error = exc
        raise last_error or TrackerError("no supported Jira search endpoint responded")

    def _fetch_token_paginated(self) -> tuple[list[Ticket], bool]:
        tickets: list[Ticket] = []
        now = time.time()
        token: str | None = None
        for _ in range(MAX_PAGES):
            params = {
                "jql": self._jql(),
                "fields": self.JQL_FIELDS,
                "maxResults": str(PAGE_SIZE),
            }
            if token:
                params["nextPageToken"] = token
            url = f"{self.config.base_url}/rest/api/3/search/jql?{urllib.parse.urlencode(params)}"
            payload = _request(url, headers=self._headers())
            issues = payload.get("issues") or []
            for issue in issues:
                ticket = self._ticket(issue, now)
                if ticket is not None:
                    tickets.append(ticket)
            token = payload.get("nextPageToken")
            if payload.get("isLast") or not token or not issues:
                return tickets, False
        return tickets, True

    def _fetch_offset_paginated(self, version: int) -> tuple[list[Ticket], bool]:
        tickets: list[Ticket] = []
        now = time.time()
        start_at = 0
        for _ in range(MAX_PAGES):
            params = {
                "jql": self._jql(),
                "fields": self.JQL_FIELDS,
                "maxResults": str(PAGE_SIZE),
                "startAt": str(start_at),
            }
            url = f"{self.config.base_url}/rest/api/{version}/search?{urllib.parse.urlencode(params)}"
            payload = _request(url, headers=self._headers())
            issues = payload.get("issues") or []
            for issue in issues:
                ticket = self._ticket(issue, now)
                if ticket is not None:
                    tickets.append(ticket)
            total = int(payload.get("total") or 0)
            start_at += len(issues)
            if not issues or start_at >= total:
                return tickets, False
        return tickets, True

    def fetch_one(self, key: str) -> Ticket | None:
        """One issue by key, for linking a ticket that the last sync did not cover."""
        params = {"fields": self.JQL_FIELDS}
        for version in (3, 2):
            url = (
                f"{self.config.base_url}/rest/api/{version}/issue/"
                f"{urllib.parse.quote(key)}?{urllib.parse.urlencode(params)}"
            )
            try:
                payload = _request(url, headers=self._headers())
            except TrackerError as exc:
                status = _http_status(exc)
                if status == 404 and version == 2:
                    return None
                if status in (404, 410):
                    continue
                raise
            return self._ticket(payload, time.time())
        return None


class AdoClient:
    """Azure DevOps Boards.

    Two calls per project, which is how the ADO API is shaped: WIQL returns ids only,
    then ids are hydrated in batches of at most `BATCH_SIZE` (a documented server limit,
    not a choice).
    """

    API_VERSION = "7.0"
    BATCH_SIZE = 200
    FIELDS = (
        "System.Id",
        "System.Title",
        "System.WorkItemType",
        "System.State",
        # ADO exposes the parent id but NOT the parent's type in this projection, so
        # parent_type is resolved from the catalog after a sync (see _link_ado_parents)
        # rather than costing one extra call per work item.
        "System.Parent",
        # The area path is ADO's component taxonomy - what import routes match on.
        "System.AreaPath",
    )

    def __init__(self, config: TrackerConfig) -> None:
        self.config = config

    def _headers(self) -> dict[str, str]:
        # ADO personal access tokens authenticate as an empty username.
        return {
            "Authorization": _basic_auth("", self.config.token),
            "Accept": "application/json",
        }

    def work_item_url(self, project: str, key: str) -> str:
        base = self.config.base_url
        if project:
            return f"{base}/{urllib.parse.quote(project)}/_workitems/edit/{key}"
        return f"{base}/_workitems/edit/{key}"

    def _ticket(self, item: dict, project: str, now: float) -> Ticket | None:
        key = str(item.get("id") or "").strip()
        if not key:
            return None
        fields = item.get("fields") or {}
        raw_type = str(fields.get("System.WorkItemType") or "").strip()
        parent = fields.get("System.Parent")
        return Ticket(
            tracker_id=self.config.id,
            provider="ado",
            key=key,
            type=canonical_type(raw_type),
            raw_type=raw_type or "Unknown",
            title=str(fields.get("System.Title") or "").strip(),
            state=str(fields.get("System.State") or "").strip(),
            url=self.work_item_url(project, key),
            synced_at=now,
            parent_key=(str(parent).strip() if parent else None),
            # ADO has no status-category concept; its State is already the coarse value.
            state_category=str(fields.get("System.State") or "").strip(),
            # One area path per work item, as a one-element list so import routes match
            # Jira components and ADO areas identically.
            components=(
                [str(fields.get("System.AreaPath") or "").strip()]
                if str(fields.get("System.AreaPath") or "").strip()
                else []
            ),
            # An ADO work item's numeric id says nothing about its project, so the
            # project it was queried from travels with the ticket.
            project=project,
        )

    def fetch_catalog(self) -> tuple[list[Ticket], bool]:
        tickets: list[Ticket] = []
        truncated = False
        now = time.time()
        for project in self.config.projects:
            ids = self._query_ids(project)
            if len(ids) > MAX_TICKETS_PER_TRACKER:
                ids = ids[:MAX_TICKETS_PER_TRACKER]
                truncated = True
            for start in range(0, len(ids), self.BATCH_SIZE):
                batch = ids[start : start + self.BATCH_SIZE]
                for item in self._hydrate(batch):
                    ticket = self._ticket(item, project, now)
                    if ticket is not None:
                        tickets.append(ticket)
        return tickets, truncated

    def _query_ids(self, project: str) -> list[str]:
        url = (
            f"{self.config.base_url}/{urllib.parse.quote(project)}"
            f"/_apis/wit/wiql?api-version={self.API_VERSION}&$top={MAX_TICKETS_PER_TRACKER}"
        )
        # Escaping: a project name with an apostrophe would otherwise break the WIQL
        # string literal (WIQL doubles single quotes, like SQL).
        safe_project = project.replace("'", "''")
        # Same activity bound as JiraClient._jql; the date shape was validated at
        # config load, so it can be spliced into the WIQL literal safely.
        window = (
            f"AND [System.ChangedDate] >= '{self.config.since}' "
            if self.config.since
            else ""
        )
        query = (
            "SELECT [System.Id] FROM WorkItems "
            f"WHERE [System.TeamProject] = '{safe_project}' "
            f"{window}"
            "ORDER BY [System.ChangedDate] DESC"
        )
        payload = _request(url, headers=self._headers(), method="POST", body={"query": query})
        return [str(w.get("id")) for w in (payload.get("workItems") or []) if w.get("id")]

    def _hydrate(self, ids: list[str]) -> list[dict]:
        params = {
            "ids": ",".join(ids),
            "fields": ",".join(self.FIELDS),
            "api-version": self.API_VERSION,
        }
        url = f"{self.config.base_url}/_apis/wit/workitems?{urllib.parse.urlencode(params)}"
        payload = _request(url, headers=self._headers())
        return list(payload.get("value") or [])

    def fetch_one(self, key: str) -> Ticket | None:
        params = {"fields": ",".join(self.FIELDS), "api-version": self.API_VERSION}
        url = (
            f"{self.config.base_url}/_apis/wit/workitems/"
            f"{urllib.parse.quote(key)}?{urllib.parse.urlencode(params)}"
        )
        try:
            payload = _request(url, headers=self._headers())
        except TrackerError as exc:
            if _http_status(exc) == 404:
                return None
            raise
        fields = payload.get("fields") or {}
        project = str(fields.get("System.TeamProject") or "")
        if not project and self.config.projects:
            project = self.config.projects[0]
        return self._ticket(payload, project, time.time())


def client_for(config: TrackerConfig) -> JiraClient | AdoClient:
    return JiraClient(config) if config.provider == "jira" else AdoClient(config)


# ---- key parsing -----------------------------------------------------------------

_JIRA_KEY = re.compile(r"\b([A-Z][A-Z0-9_]+-\d+)\b")
_ADO_ID = re.compile(r"^\d+$")


def parse_reference(text: str, trackers: tuple[TrackerConfig, ...]) -> tuple[str, str] | None:
    """Turns what a human pasted into `(tracker_id, key)`, or None.

    Accepts a full ticket URL (Jira `/browse/PROJ-1`, ADO `/_workitems/edit/42`, and the
    `?selectedIssue=` form Jira boards produce) or a bare key. A bare key is attributed by
    matching its project prefix against each tracker's configured `projects`, which is the
    second reason that list is required config: it is what makes `PROJ-123` unambiguous
    when two Jira instances are connected.
    """
    raw = (text or "").strip()
    if not raw or not trackers:
        return None

    # A URL: prefer the tracker whose base_url it belongs to, so the same key existing in
    # two instances resolves to the one actually linked.
    if "://" in raw:
        host = urllib.parse.urlsplit(raw).netloc.lower()
        candidates = [
            t for t in trackers if urllib.parse.urlsplit(t.base_url).netloc.lower() == host
        ]
        for tracker in candidates or list(trackers):
            key = _key_from_url(raw, tracker)
            if key:
                return tracker.id, key
        return None

    upper = raw.upper()
    jira_match = _JIRA_KEY.search(upper)
    if jira_match:
        key = jira_match.group(1)
        prefix = key.rsplit("-", 1)[0]
        for tracker in trackers:
            if tracker.provider == "jira" and prefix in {p.upper() for p in tracker.projects}:
                return tracker.id, key
        # Prefix not in any configured project list: still resolvable if exactly one Jira
        # tracker is connected, since there is then no ambiguity to protect against.
        jira = [t for t in trackers if t.provider == "jira"]
        if len(jira) == 1:
            return jira[0].id, key
        return None

    if _ADO_ID.match(raw):
        ado = [t for t in trackers if t.provider == "ado"]
        if len(ado) == 1:
            return ado[0].id, raw
        return None
    return None


def _key_from_url(url: str, tracker: TrackerConfig) -> str | None:
    if tracker.provider == "jira":
        match = re.search(r"/browse/([A-Za-z][A-Za-z0-9_]+-\d+)", url)
        if match:
            return match.group(1).upper()
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        for param in ("selectedIssue", "issueKey", "issue"):
            values = query.get(param)
            if values and _JIRA_KEY.match(values[0].upper()):
                return values[0].upper()
        return None
    match = re.search(r"/_workitems/(?:edit|view)/(\d+)", url)
    if match:
        return match.group(1)
    match = re.search(r"[?&]workitem=(\d+)", url)
    return match.group(1) if match else None


# ---- store -----------------------------------------------------------------------


@dataclass
class _State:
    tickets: dict[str, Ticket] = field(default_factory=dict)  # "tracker_id\x00key" -> Ticket
    statuses: dict[str, SyncStatus] = field(default_factory=dict)
    last_sync_at: float | None = None


def _catalog_key(tracker_id: str, key: str) -> str:
    return f"{tracker_id}\x00{normalize_key(tracker_id, key)}"


def normalize_key(tracker_id: str, key: str) -> str:
    """The canonical spelling of a ticket key, which is what 1:1 uniqueness is checked on.

    Jira keys are case-insensitive in practice (`proj-1` and `PROJ-1` are the same issue),
    so they are upper-cased; ADO ids are digits and left alone. Without this, the same
    ticket pasted in two cases would link to two different roadmap items and quietly
    break the 1:1 guarantee.
    """
    stripped = (key or "").strip()
    tracker = CONFIG.tracker(tracker_id)
    if tracker is not None and tracker.provider == "ado":
        return stripped
    return stripped.upper()


class TrackerStore:
    """The synced ticket catalog: server-owned, in memory, persisted to
    workspace/trackers.json.

    Same shape and reasoning as RoadmapStore (see its docstring): a single always-running
    process owns it, PM session worktrees never hold their own copy, and every reader goes
    through this instance so nobody sees a stale fork. The file is a cache - deleting it
    costs one sync, never data, which is why it is gitignored runtime state rather than
    something the credential boundary has to unstage.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state = _State()
        self._syncing = False
        self._load()

    # ---- persistence ----

    def _load(self) -> None:
        if not CATALOG_PATH.exists():
            return
        try:
            raw = json.loads(CATALOG_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            # A corrupt cache is not worth failing a boot over: the next sync rebuilds it.
            return
        for entry in raw.get("tickets") or []:
            try:
                ticket = Ticket.from_dict(entry)
            except (TypeError, ValueError):
                continue
            self._state.tickets[_catalog_key(ticket.tracker_id, ticket.key)] = ticket
        for entry in raw.get("statuses") or []:
            try:
                status = SyncStatus(**entry)
            except TypeError:
                continue
            self._state.statuses[status.tracker_id] = status
        self._state.last_sync_at = raw.get("last_sync_at")

    def _save_locked(self) -> None:
        CATALOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "tickets": [t.to_dict() for t in self._state.tickets.values()],
            "statuses": [s.to_dict() for s in self._state.statuses.values()],
            "last_sync_at": self._state.last_sync_at,
        }
        # Write-then-replace, like the other stores, so a crash mid-write cannot leave a
        # truncated cache behind. The `.tmp` sibling is gitignored for the same reason.
        tmp = CATALOG_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2))
        tmp.replace(CATALOG_PATH)

    # ---- reads ----

    @property
    def is_configured(self) -> bool:
        return bool(CONFIG.trackers)

    @property
    def is_syncing(self) -> bool:
        return self._syncing

    def lookup(self, tracker_id: str | None, key: str | None) -> Ticket | None:
        if not tracker_id or not key:
            return None
        with self._lock:
            return self._state.tickets.get(_catalog_key(tracker_id, key))

    def tickets_of(self, tracker_id: str) -> list[Ticket]:
        """One tracker's whole catalog, as Ticket objects - what the import pass walks.
        A snapshot under the lock, so a concurrent sync can't mutate mid-iteration."""
        with self._lock:
            return [t for t in self._state.tickets.values() if t.tracker_id == tracker_id]

    def search(
        self, query: str = "", tracker_id: str = "", limit: int = 50, canonical: str = ""
    ) -> list[dict]:
        """Candidate tickets for the link picker: substring match on key or title.

        `canonical` narrows to one canonical type slug (see CANONICAL_TYPES), applied
        before the limit so asking for epics cannot come back empty just because fifty
        stories sort ahead of them.
        """
        needle = (query or "").strip().lower()
        with self._lock:
            tickets = list(self._state.tickets.values())
        if tracker_id:
            tickets = [t for t in tickets if t.tracker_id == tracker_id]
        if canonical:
            tickets = [t for t in tickets if t.type == canonical]
        if needle:
            tickets = [
                t for t in tickets if needle in t.key.lower() or needle in t.title.lower()
            ]
        # Most recently changed first is what the tracker ordering already gives us within
        # a sync; key order is the stable tiebreak across trackers.
        tickets.sort(key=lambda t: (t.tracker_id, t.key))
        return [t.to_dict() for t in tickets[:limit]]

    def describe(self) -> dict:
        """What GET /trackers returns. Deliberately assembled from TrackerConfig fields one
        by one rather than by dumping the dataclass - `token` must never be serialised, and
        an allowlist keeps that true when someone adds a field later."""
        with self._lock:
            statuses = dict(self._state.statuses)
            last_sync_at = self._state.last_sync_at
            counts: dict[str, int] = {}
            for ticket in self._state.tickets.values():
                counts[ticket.tracker_id] = counts.get(ticket.tracker_id, 0) + 1
        trackers = []
        for config in CONFIG.trackers:
            status = statuses.get(config.id)
            trackers.append(
                {
                    "id": config.id,
                    "provider": config.provider,
                    "label": config.label,
                    "base_url": config.base_url,
                    "projects": list(config.projects),
                    "usable": config.is_usable,
                    "unusable_reason": config.unusable_reason,
                    "ticket_count": counts.get(config.id, 0),
                    "status": status.to_dict() if status else None,
                }
            )
        return {
            "configured": bool(CONFIG.trackers),
            "syncing": self._syncing,
            "last_sync_at": last_sync_at,
            "trackers": trackers,
            "types": list(CANONICAL_TYPES),
        }

    # ---- writes ----

    def resolve(self, reference: str) -> tuple[str, str] | None:
        return parse_reference(reference, CONFIG.trackers)

    def ensure_ticket(self, tracker_id: str, key: str) -> Ticket | None:
        """Returns the catalog entry, fetching the single ticket from the tracker if the
        last sync did not include it (a brand-new ticket, or one outside the configured
        projects). Returns None if the tracker has no such ticket, which is what lets the
        link endpoint reject a typo instead of storing a dead reference."""
        existing = self.lookup(tracker_id, key)
        if existing is not None:
            return existing
        config = CONFIG.tracker(tracker_id)
        if config is None or not config.is_usable:
            return None
        ticket = client_for(config).fetch_one(normalize_key(tracker_id, key))
        if ticket is None:
            return None
        with self._lock:
            self._state.tickets[_catalog_key(ticket.tracker_id, ticket.key)] = ticket
            self._save_locked()
        return ticket

    def due_tracker_ids(self, now: float | None = None) -> list[str]:
        """Which trackers the background loop should pull right now.

        A tracker is due when it has never synced or its last attempt (success OR failure -
        a failing tracker must keep retrying) is older than its own interval.
        """
        moment = now if now is not None else time.time()
        with self._lock:
            statuses = dict(self._state.statuses)
        due = []
        for config in CONFIG.trackers:
            status = statuses.get(config.id)
            last = (status.finished_at or status.started_at) if status else None
            if last is None or moment - last >= config.sync_interval_minutes * 60:
                due.append(config.id)
        return due

    def refresh_missing(self, refs: list[tuple[str, str]]) -> int:
        """Fetches any linked ticket the catalog does not hold, one call each.

        Closes the gap between "linked" and "synced": a stakeholder can legitimately link a
        ticket from a project that is not in `projects` (a one-off dependency on another
        team's board), and it would otherwise render forever as unresolved. Bounded by the
        number of linked changes, which is small by construction - one per change.
        """
        added = 0
        for tracker_id, key in refs:
            if self.lookup(tracker_id, key) is not None:
                continue
            try:
                if self.ensure_ticket(tracker_id, key) is not None:
                    added += 1
            except TrackerError:
                # An unreachable tracker is already reported by the catalog sync's own
                # status entry; one unresolvable link must not abort the rest.
                continue
        return added

    def sync(self, tracker_id: str | None = None) -> dict:
        """Pulls the configured boards. Runs on the caller's thread - the server calls it
        from a daemon thread (see server.py), matching how merges and dev tasks are run.

        Never raises: each tracker's failure is recorded in its own status entry, so one
        unreachable instance neither hides the others' results nor breaks the board.
        """
        targets = [
            t for t in CONFIG.trackers if tracker_id is None or t.id == tracker_id
        ]
        self._syncing = True
        try:
            for config in targets:
                self._sync_one(config)
            with self._lock:
                self._state.last_sync_at = time.time()
                self._save_locked()
        finally:
            self._syncing = False
        return self.describe()

    def _sync_one(self, config: TrackerConfig) -> None:
        started = time.time()
        status = SyncStatus(
            tracker_id=config.id,
            label=config.label,
            provider=config.provider,
            started_at=started,
        )
        if not config.is_usable:
            status.error = config.unusable_reason
            status.finished_at = time.time()
            with self._lock:
                self._state.statuses[config.id] = status
                self._save_locked()
            return

        try:
            tickets, truncated = client_for(config).fetch_catalog()
        except TrackerError as exc:
            status.error = _scrub(str(exc), config.token)
            status.finished_at = time.time()
            with self._lock:
                # The previous catalog is deliberately KEPT on failure: a linked card
                # showing last week's type is far better than one that suddenly claims the
                # ticket does not exist. The stale timestamp is what the UI surfaces.
                self._state.statuses[config.id] = status
                self._save_locked()
            return
        except Exception as exc:  # noqa: BLE001 - a client bug must not kill the thread
            status.error = _scrub(f"unexpected error: {exc}", config.token)
            status.finished_at = time.time()
            with self._lock:
                self._state.statuses[config.id] = status
                self._save_locked()
            return

        # ADO's work-item projection carries the parent id but not its type, so fill it in
        # from the batch we just pulled. Jira nests the parent's fields and needs none of
        # this. Anything whose parent is outside the synced projects stays None, which the
        # import rules treat the same as "no parent" - the honest answer, since we cannot
        # know whether an unseen parent is an Epic.
        if config.provider == "ado":
            by_key = {t.key: t for t in tickets}
            for ticket in tickets:
                if ticket.parent_key and ticket.parent_type is None:
                    parent = by_key.get(ticket.parent_key)
                    if parent is not None:
                        ticket.parent_type = parent.raw_type

        status.ok = True
        status.ticket_count = len(tickets)
        status.truncated = truncated
        status.finished_at = time.time()
        with self._lock:
            # Replace this tracker's slice wholesale so a ticket deleted upstream leaves
            # the catalog, while other trackers' entries are untouched.
            for catalog_key in [
                k for k, t in self._state.tickets.items() if t.tracker_id == config.id
            ]:
                del self._state.tickets[catalog_key]
            for ticket in tickets:
                self._state.tickets[_catalog_key(ticket.tracker_id, ticket.key)] = ticket
            self._state.statuses[config.id] = status
            self._save_locked()
