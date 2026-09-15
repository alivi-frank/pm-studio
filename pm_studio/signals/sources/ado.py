"""Azure DevOps: every work item revision, and every pull request.

Revisions come from the reporting feed (`wit/reporting/workitemrevisions`), which
streams the whole project's history a thousand revisions per call and hands back a
continuation token - so the second run reads only what changed. Each revision is
diffed against the item's previous one to name what happened: a state move, a
hand-off, logged hours (CompletedWork rising), a comment (a comment version ref), or
some other field edit.

Pull requests come from the git API per project, with reviewers and votes, and are
attributed by the work item ids in their title and branch names.
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone

from ..model import (
    DEFAULT_WEIGHTS,
    KIND_ASSIGNEE,
    KIND_COMMENT,
    KIND_CREATED,
    KIND_FIELD,
    KIND_PR_MERGED,
    KIND_PR_OPENED,
    KIND_PR_REVIEW,
    KIND_STATUS,
    KIND_WORKLOG,
    Signal,
    extract_ticket_refs,
    parse_iso,
    signal_id,
)
from .base import CollectResult
from urllib.parse import quote

from .http import SourceError, basic_auth, get_json, qs

REVISION_FIELDS = [
    "System.Id", "System.Rev", "System.State", "System.Reason", "System.AssignedTo", "System.ChangedDate",
    "System.ChangedBy", "System.CreatedDate", "System.CreatedBy", "System.WorkItemType", "System.Title",
    "System.Parent", "System.AreaPath", "System.IterationPath",
    "Microsoft.VSTS.Scheduling.CompletedWork", "Microsoft.VSTS.Scheduling.RemainingWork",
    "Microsoft.VSTS.Scheduling.OriginalEstimate", "Microsoft.VSTS.Scheduling.StoryPoints",
    "Microsoft.VSTS.Common.ClosedDate", "Microsoft.VSTS.Common.StateChangeDate", "Microsoft.VSTS.Common.Priority",
]
IDENTITY_RE = re.compile(r"^\s*(.*?)\s*<([^>]+)>\s*$")
STATE_CATEGORY_MAP = {"Proposed": "todo", "InProgress": "in_progress", "Resolved": "resolved", "Completed": "done", "Removed": "removed"}
PR_PAGE = 500


def split_identity(value) -> tuple[str, str]:
    """ADO renders people as "Name <email>" in the reporting feed and as objects
    elsewhere; both become (name, email)."""
    if isinstance(value, dict):
        return str(value.get("displayName") or ""), str(value.get("uniqueName") or value.get("mailAddress") or "").lower()
    text = str(value or "")
    match = IDENTITY_RE.match(text)
    if match:
        return match.group(1), match.group(2).lower()
    return text.strip(), ""


class AdoHistorySource:
    id = "ado"
    label = "Azure DevOps history"
    category = "tracker"

    def __init__(self, tracker_id: str, base_url: str, projects: tuple[str, ...], token: str, *, weights: dict[str, float] | None = None, fetch=None) -> None:
        self.tracker_id = tracker_id
        self.base_url = base_url.rstrip("/")
        self.projects = tuple(projects)
        self.token = token
        self.weights = weights or {}
        self._get = fetch or get_json
        self.label = f"Azure DevOps history ({', '.join(projects)})" if projects else "Azure DevOps history"

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.projects and self.token)

    def describe(self) -> dict:
        return {"id": self.id, "label": self.label, "category": self.category, "configured": self.configured,
                "detail": f"{self.base_url} · work item revisions (reporting feed)", "tracker_id": self.tracker_id}

    def _headers(self) -> dict[str, str]:
        return {"Authorization": basic_auth("", self.token)}

    def _w(self, kind: str) -> float:
        return self.weights.get(kind, DEFAULT_WEIGHTS[kind])

    def _state_categories(self, project: str) -> dict[str, dict[str, str]]:
        """work item type -> state -> canonical category."""
        out: dict[str, dict[str, str]] = {}
        try:
            types = self._get(f"{self.base_url}/{quote(project)}/_apis/wit/workitemtypes?api-version=7.1", self._headers())
        except SourceError:
            return out
        for wit in types.get("value") or []:
            name = wit.get("name")
            states = {}
            for state in wit.get("states") or []:
                states[str(state.get("name"))] = STATE_CATEGORY_MAP.get(str(state.get("category")), str(state.get("category") or "").lower())
            if name:
                out[str(name)] = states
        return out

    def _revisions(self, project: str, since: float, continuation: str | None):
        """Yields (revision, continuation_token) pairs; the token of the last batch is
        what the next run continues from."""
        if continuation:
            url = f"{self.base_url}/{quote(project)}/_apis/wit/reporting/workitemrevisions?{qs({'api-version': '7.1', 'continuationToken': continuation, 'fields': ','.join(REVISION_FIELDS)})}"
        else:
            start_iso = datetime.fromtimestamp(since, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            url = f"{self.base_url}/{quote(project)}/_apis/wit/reporting/workitemrevisions?{qs({'api-version': '7.1', 'startDateTime': start_iso, 'fields': ','.join(REVISION_FIELDS)})}"
        while True:
            payload = self._get(url, self._headers())
            token = payload.get("continuationToken")
            for row in payload.get("values") or []:
                yield row, token
            if payload.get("isLastBatch") or not payload.get("nextLink"):
                return
            url = payload["nextLink"]

    def _diff_signals(self, project: str, revisions: list[dict], categories: dict[str, dict[str, str]], *, seed: dict | None = None, transitions: list[dict] | None = None) -> tuple[list[Signal], dict]:
        """Signals for one work item from its ordered revisions, plus its fact. `seed`
        is the item's last known field state from an earlier run, so only the NEW
        revisions need to be present; `transitions` are the ones already recorded."""
        signals: list[Signal] = []
        revisions = sorted(revisions, key=lambda r: int(r.get("rev") or 0))
        wi_id = str(revisions[0].get("id"))
        ref = f"{self.tracker_id}:{wi_id}"
        prev: dict = dict(seed or {})
        transitions = list(transitions or [])
        wit = str(prev.get("System.WorkItemType") or "")
        for rev in revisions:
            f = rev.get("fields") or {}
            wit = str(f.get("System.WorkItemType") or wit)
            at = parse_iso(f.get("System.ChangedDate"))
            if at is None:
                continue
            name, email = split_identity(f.get("System.ChangedBy"))
            rev_no = int(rev.get("rev") or 0)
            cats = categories.get(wit, {})
            state, prev_state = str(f.get("System.State") or ""), str(prev.get("System.State") or "")
            assignee, prev_assignee = str(f.get("System.AssignedTo") or ""), str(prev.get("System.AssignedTo") or "")
            done = float(f.get("Microsoft.VSTS.Scheduling.CompletedWork") or 0.0)
            prev_done = float(prev.get("Microsoft.VSTS.Scheduling.CompletedWork") or 0.0)
            emitted = False
            if rev_no == 1 or not prev:
                signals.append(Signal(id=signal_id("ado", wi_id, rev_no, "created"), at=at, source="ado", kind=KIND_CREATED, actor=name, actor_email=email, refs=[ref], weight=self._w(KIND_CREATED), meta={"issue": wi_id, "type": wit, "project": project}))
                emitted = True
            if prev and state != prev_state:
                meta = {"issue": wi_id, "from": prev_state, "to": state, "from_cat": cats.get(prev_state, ""), "to_cat": cats.get(state, ""), "type": wit}
                transitions.append({"at": at, "from": prev_state, "to": state, "from_cat": meta["from_cat"], "to_cat": meta["to_cat"]})
                signals.append(Signal(id=signal_id("ado", wi_id, rev_no, "status"), at=at, source="ado", kind=KIND_STATUS, actor=name, actor_email=email, refs=[ref], weight=self._w(KIND_STATUS), meta=meta))
                emitted = True
            if prev and assignee != prev_assignee:
                to_name, _ = split_identity(assignee)
                signals.append(Signal(id=signal_id("ado", wi_id, rev_no, "assignee"), at=at, source="ado", kind=KIND_ASSIGNEE, actor=name, actor_email=email, refs=[ref], weight=self._w(KIND_ASSIGNEE), meta={"issue": wi_id, "to": to_name, "type": wit}))
                emitted = True
            if done > prev_done:
                # Hours booked on the item; attributed to whoever the item is assigned
                # to when that is known, else to the editor.
                owner_name, owner_email = split_identity(assignee) if assignee else (name, email)
                signals.append(Signal(id=signal_id("ado", wi_id, rev_no, "worklog"), at=at, source="ado", kind=KIND_WORKLOG, actor=owner_name or name, actor_email=owner_email or email, refs=[ref], weight=0.0, minutes=(done - prev_done) * 60.0, meta={"issue": wi_id, "type": wit, "booked_by": name}))
                emitted = True
            if rev.get("commentVersionRef"):
                signals.append(Signal(id=signal_id("ado", wi_id, rev_no, "comment"), at=at, source="ado", kind=KIND_COMMENT, actor=name, actor_email=email, refs=[ref], weight=self._w(KIND_COMMENT), meta={"issue": wi_id, "type": wit}))
                emitted = True
            if not emitted and prev:
                changed = [k.split(".")[-1] for k in f if k not in ("System.Rev", "System.ChangedDate", "System.ChangedBy", "System.AuthorizedDate", "System.RevisedDate") and f.get(k) != prev.get(k)]
                signals.append(Signal(id=signal_id("ado", wi_id, rev_no, "field"), at=at, source="ado", kind=KIND_FIELD, actor=name, actor_email=email, refs=[ref], weight=self._w(KIND_FIELD), meta={"issue": wi_id, "field": ",".join(changed[:4]) or "edit", "type": wit}))
            prev = f
        last = prev
        cats = categories.get(wit, {})
        fact = {
            "created": parse_iso(last.get("System.CreatedDate")),
            "updated": parse_iso(last.get("System.ChangedDate")),
            "resolved": parse_iso(last.get("Microsoft.VSTS.Common.ClosedDate")),
            "status_since": parse_iso(last.get("Microsoft.VSTS.Common.StateChangeDate")),
            "type": wit,
            "status": str(last.get("System.State") or ""),
            "status_cat": cats.get(str(last.get("System.State") or ""), ""),
            "parent": str(last.get("System.Parent")) if last.get("System.Parent") else None,
            "points": last.get("Microsoft.VSTS.Scheduling.StoryPoints"),
            "priority": str(last.get("Microsoft.VSTS.Common.Priority") or ""),
            "transitions": transitions,
            "assignee": split_identity(last.get("System.AssignedTo"))[0],
            "completed_work": float(last.get("Microsoft.VSTS.Scheduling.CompletedWork") or 0.0),
            "project": project,
        }
        return signals, fact

    def collect(self, since: float, previous: CollectResult | None = None) -> CollectResult:
        result = CollectResult()
        prev_facts = (previous.facts if previous else None) or {}
        continuations: dict = dict(prev_facts.get("continuations") or {})
        categories_all: dict = dict(prev_facts.get("state_categories") or {})
        issues_facts: dict = dict(prev_facts.get("issues") or {})
        # Per item we keep only its LAST field state (what the next revision is diffed
        # against), never the revision history itself - a project's full history is
        # hundreds of thousands of revisions and would make the cache unloadable.
        items: dict[str, dict] = dict(prev_facts.get("items") or {})
        if "raw" in prev_facts and not items:
            # Migration from the first cache shape, which kept every revision.
            for key, revs in (prev_facts.get("raw") or {}).items():
                if revs:
                    last = max(revs, key=lambda r: int(r.get("rev") or 0))
                    items[key] = {"rev": int(last.get("rev") or 0), "fields": last.get("fields") or {}}
        new_revs: dict[str, list[dict]] = {}
        for project in self.projects:
            if project not in categories_all:
                categories_all[project] = self._state_categories(project)
            last_token = continuations.get(project)
            try:
                for rev, token in self._revisions(project, since, continuations.get(project)):
                    wi_id = str(rev.get("id"))
                    key = f"{project}\x1f{wi_id}"
                    rev_no = int(rev.get("rev") or 0)
                    known = items.get(key, {}).get("rev", 0)
                    if rev_no <= known:
                        continue
                    new_revs.setdefault(key, []).append({"id": rev.get("id"), "rev": rev_no, "fields": rev.get("fields") or {}, "commentVersionRef": bool(rev.get("commentVersionRef"))})
                    last_token = token or last_token
            except SourceError as exc:
                result.truncated = True
                result.notes.append(f"{project}: stopped early: {exc}")
            continuations[project] = last_token
        touched_ids: set[str] = set()
        for key, revs in new_revs.items():
            project, _, wi_id = key.partition("\x1f")
            state = items.get(key)
            existing = (issues_facts.get(wi_id) or {}).get("transitions") if state else None
            try:
                signals, fact = self._diff_signals(project, revs, categories_all.get(project, {}), seed=state["fields"] if state else None, transitions=existing)
            except Exception as exc:  # noqa: BLE001
                result.notes.append(f"{wi_id}: {exc}")
                continue
            last = max(revs, key=lambda r: int(r.get("rev") or 0))
            items[key] = {"rev": int(last.get("rev") or 0), "fields": last.get("fields") or {}}
            issues_facts[wi_id] = fact
            touched_ids.add(wi_id)
            result.signals.extend(s for s in signals if s.at >= since)
        # Every earlier signal stands: revisions are immutable and new ones only add.
        if previous:
            for signal in previous.signals:
                if signal.at >= since:
                    result.signals.append(signal)
        result.facts = {
            "issues": issues_facts,
            "state_categories": categories_all,
            "continuations": continuations,
            "items": items,
            "touched": len(touched_ids),
            "collected_at": time.time(),
        }
        return result


class AdoPullRequestSource:
    id = "ado-prs"
    label = "Azure DevOps pull requests"
    category = "code"

    def __init__(self, tracker_id: str, base_url: str, projects: tuple[str, ...], token: str, *, repo_resolver=None, weights: dict[str, float] | None = None, fetch=None) -> None:
        self.tracker_id = tracker_id
        self.base_url = base_url.rstrip("/")
        self.projects = tuple(projects)
        self.token = token
        self.weights = weights or {}
        self._get = fetch or get_json
        # repo name -> (repo_rel, system) or None
        self._resolve_repo = repo_resolver or (lambda name: (None, None))

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.projects and self.token)

    def describe(self) -> dict:
        return {"id": self.id, "label": self.label, "category": self.category, "configured": self.configured,
                "detail": f"{', '.join(self.projects) or 'no projects declared ([signals] ado_pr_projects)'}"}

    def _headers(self) -> dict[str, str]:
        return {"Authorization": basic_auth("", self.token)}

    def _w(self, kind: str) -> float:
        return self.weights.get(kind, DEFAULT_WEIGHTS[kind])

    def _prs(self, project: str, since: float):
        skip = 0
        start_iso = datetime.fromtimestamp(since, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        while True:
            params = {"api-version": "7.1", "searchCriteria.status": "all", "searchCriteria.minTime": start_iso, "searchCriteria.queryTimeRangeType": "created", "$top": PR_PAGE, "$skip": skip}
            payload = self._get(f"{self.base_url}/{quote(project)}/_apis/git/pullrequests?{qs(params)}", self._headers())
            rows = payload.get("value") or []
            for row in rows:
                yield row
            if len(rows) < PR_PAGE:
                return
            skip += len(rows)

    def collect(self, since: float, previous: CollectResult | None = None) -> CollectResult:
        result = CollectResult()
        count = 0
        for project in self.projects:
            try:
                for pr in self._prs(project, since):
                    count += 1
                    pr_id = pr.get("pullRequestId")
                    repo_name = (pr.get("repository") or {}).get("name") or ""
                    repo_rel, system = self._resolve_repo(repo_name)
                    text = f"{pr.get('title', '')} {pr.get('sourceRefName', '')} {pr.get('description', '') or ''}"
                    _, ado_ids = extract_ticket_refs(text.replace("refs/heads/", ""))
                    refs = [f"{self.tracker_id}:{i}" for i in ado_ids]
                    creator, creator_email = split_identity(pr.get("createdBy"))
                    created = parse_iso(pr.get("creationDate"))
                    closed = parse_iso(pr.get("closedDate"))
                    base_meta = {"pr": pr_id, "repo_name": repo_name, "title": str(pr.get("title") or "")[:160], "status": pr.get("status"), "project": project,
                                 "source": str(pr.get("sourceRefName") or "").replace("refs/heads/", ""), "target": str(pr.get("targetRefName") or "").replace("refs/heads/", "")}
                    if created and created >= since:
                        result.signals.append(Signal(id=signal_id("ado-pr", project, pr_id, "opened"), at=created, source="ado-prs", kind=KIND_PR_OPENED, actor=creator, actor_email=creator_email, refs=refs, repo=repo_rel, system=system, weight=self._w(KIND_PR_OPENED), meta=base_meta))
                    if closed and closed >= since and pr.get("status") == "completed":
                        result.signals.append(Signal(id=signal_id("ado-pr", project, pr_id, "merged"), at=closed, source="ado-prs", kind=KIND_PR_MERGED, actor=creator, actor_email=creator_email, refs=refs, repo=repo_rel, system=system, weight=self._w(KIND_PR_MERGED), meta={**base_meta, "hours_open": round((closed - created) / 3600.0, 1) if created else None}))
                    for reviewer in pr.get("reviewers") or []:
                        vote = int(reviewer.get("vote") or 0)
                        if vote == 0 or reviewer.get("isContainer"):
                            continue
                        r_name, r_email = split_identity(reviewer)
                        if r_email == creator_email and r_email:
                            continue
                        at = closed or created
                        if at and at >= since:
                            result.signals.append(Signal(id=signal_id("ado-pr", project, pr_id, "review", r_email or r_name), at=at, source="ado-prs", kind=KIND_PR_REVIEW, actor=r_name, actor_email=r_email, refs=refs, repo=repo_rel, system=system, weight=self._w(KIND_PR_REVIEW), meta={**base_meta, "vote": vote}))
            except SourceError as exc:
                result.truncated = True
                result.notes.append(f"{project}: {exc}")
        result.facts = {"pull_requests": count, "collected_at": time.time()}
        return result
