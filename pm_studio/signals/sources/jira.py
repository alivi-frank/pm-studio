"""Every issue's full history from Jira Cloud: transitions, hand-offs, worklogs,
comments and field edits, straight from the changelog.

The catalog sync (`trackers.py`) keeps the CURRENT state of each ticket. This adapter
keeps the PAST: the search API with `expand=changelog` returns each issue's history
in the same page as the issue, so ~4,000 issues cost ~40-80 requests rather than
4,000. Issues whose changelog, worklog or comment list overflowed the embedded page
are topped up individually.

Incremental: the previous run's newest `updated` stamp bounds the next JQL, and the
signals of every issue NOT re-fetched are carried over from the previous result.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

from ..model import (
    DEFAULT_WEIGHTS,
    KIND_ASSIGNEE,
    KIND_COMMENT,
    KIND_CREATED,
    KIND_FIELD,
    KIND_STATUS,
    KIND_WORKLOG,
    Signal,
    parse_iso,
    signal_id,
)
from .base import CollectResult
from .http import SourceError, basic_auth, get_json, qs

PAGE_SIZE = 100
FIELDS = "summary,status,issuetype,created,updated,resolutiondate,statuscategorychangedate,reporter,creator,assignee,parent,worklog,comment,customfield_10016,priority,components"
# Changelog fields that are pure bookkeeping noise (rank drags, automation counters).
IGNORED_FIELDS = {"Rank", "Sprint Commitment Hit Rate", "timeestimate", "timeoriginalestimate", "WorklogId", "WorklogTimeSpent", "timespent", "RemoteIssueLink"}
BOT_AUTHORS = ("automation for jira", "jira automation", "bitbucket", "github", "jira service management", "herocoders", "checklists for jira", "jira outlook", "slack", "atlassian assist", "system")


def _author(node: dict | None) -> tuple[str, str, str]:
    node = node or {}
    return str(node.get("displayName") or ""), str(node.get("emailAddress") or "").lower(), str(node.get("accountId") or "")


def _is_bot(name: str) -> bool:
    lowered = name.lower()
    return any(marker in lowered for marker in BOT_AUTHORS)


def _adf_text_len(body) -> int:
    """Rough length of an Atlassian Document Format body - enough to tell a nod from
    an essay, without rendering it."""
    if isinstance(body, str):
        return len(body)
    if isinstance(body, dict):
        return sum(_adf_text_len(v) for k, v in body.items() if k in ("content", "text"))
    if isinstance(body, list):
        return sum(_adf_text_len(v) for v in body)
    return 0


class JiraHistorySource:
    id = "jira"
    label = "Jira history"
    category = "tracker"

    def __init__(self, tracker_id: str, base_url: str, projects: tuple[str, ...], username: str, token: str, *, weights: dict[str, float] | None = None, fetch=None) -> None:
        self.tracker_id = tracker_id
        self.base_url = base_url.rstrip("/")
        self.projects = tuple(projects)
        self.username = username
        self.token = token
        self.weights = weights or {}
        self._get = fetch or get_json
        self.label = f"Jira history ({', '.join(projects)})" if projects else "Jira history"

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.projects and self.username and self.token)

    def describe(self) -> dict:
        return {"id": self.id, "label": self.label, "category": self.category, "configured": self.configured,
                "detail": f"{self.base_url} · changelog + worklogs + comments", "tracker_id": self.tracker_id}

    def _headers(self) -> dict[str, str]:
        return {"Authorization": basic_auth(self.username, self.token)}

    def _w(self, kind: str) -> float:
        return self.weights.get(kind, DEFAULT_WEIGHTS[kind])

    # ---- fetching ----

    def _status_categories(self) -> dict[str, str]:
        try:
            payload = self._get(f"{self.base_url}/rest/api/3/status", self._headers())
        except SourceError:
            return {}
        out: dict[str, str] = {}
        for status in payload.get("value", []) if isinstance(payload.get("value"), list) else []:
            cat = ((status.get("statusCategory") or {}).get("key") or "").lower()
            out[str(status.get("name"))] = {"new": "todo", "indeterminate": "in_progress", "done": "done"}.get(cat, cat)
        return out

    def _search(self, since_day: str):
        jql = f"project in ({','.join(self.projects)}) AND updated >= \"{since_day}\" ORDER BY updated ASC"
        token: str | None = None
        while True:
            params = {"jql": jql, "maxResults": PAGE_SIZE, "fields": FIELDS, "expand": "changelog", "nextPageToken": token}
            payload = self._get(f"{self.base_url}/rest/api/3/search/jql?{qs(params)}", self._headers())
            issues = payload.get("issues") or []
            for issue in issues:
                yield issue
            token = payload.get("nextPageToken")
            if payload.get("isLast") or not token or not issues:
                return

    def _paged(self, url: str, key: str) -> list[dict]:
        out: list[dict] = []
        start = 0
        while True:
            payload = self._get(f"{url}{'&' if '?' in url else '?'}startAt={start}&maxResults=100", self._headers())
            rows = payload.get(key) or payload.get("values") or []
            out.extend(rows)
            total = int(payload.get("total") or 0)
            start += len(rows)
            if not rows or start >= total:
                return out

    # ---- shaping ----

    def _issue_signals(self, issue: dict, categories: dict[str, str]) -> tuple[list[Signal], dict]:
        key = issue["key"]
        f = issue.get("fields") or {}
        ref = f"{self.tracker_id}:{key}"
        signals: list[Signal] = []
        created = parse_iso(f.get("created"))
        rep_name, rep_email, rep_id = _author(f.get("reporter") or f.get("creator"))
        if created:
            signals.append(Signal(id=signal_id("jira", key, "created"), at=created, source="jira", kind=KIND_CREATED,
                                  actor=rep_name, actor_email=rep_email, refs=[ref], weight=self._w(KIND_CREATED),
                                  meta={"issue": key, "actor_key": rep_id, "type": ((f.get("issuetype") or {}).get("name") or "")}))
        # changelog
        changelog = issue.get("changelog") or {}
        histories = list(changelog.get("histories") or [])
        if int(changelog.get("total") or 0) > len(histories):
            try:
                histories = self._paged(f"{self.base_url}/rest/api/3/issue/{key}/changelog", "values")
            except SourceError:
                pass
        transitions = []
        for history in histories:
            at = parse_iso(history.get("created"))
            if at is None:
                continue
            name, email, account = _author(history.get("author"))
            bot = _is_bot(name)
            for item in history.get("items") or []:
                field_name = str(item.get("field") or "")
                if field_name in IGNORED_FIELDS:
                    continue
                if field_name == "status":
                    kind = KIND_STATUS
                    src, dst = str(item.get("fromString") or ""), str(item.get("toString") or "")
                    meta = {"issue": key, "from": src, "to": dst, "from_cat": categories.get(src, ""), "to_cat": categories.get(dst, ""), "actor_key": account, "bot": bot}
                    transitions.append({"at": at, "from": src, "to": dst, "to_cat": meta["to_cat"], "from_cat": meta["from_cat"]})
                elif field_name == "assignee":
                    kind = KIND_ASSIGNEE
                    meta = {"issue": key, "to": str(item.get("toString") or ""), "actor_key": account, "bot": bot}
                else:
                    kind = KIND_FIELD
                    meta = {"issue": key, "field": field_name, "actor_key": account, "bot": bot}
                signals.append(Signal(id=signal_id("jira", key, history.get("id"), field_name, item.get("to"), item.get("toString")), at=at, source="jira", kind=kind,
                                      actor=name, actor_email=email, refs=[ref], weight=0.0 if bot else self._w(kind), meta=meta))
        # worklogs
        worklog = f.get("worklog") or {}
        logs = list(worklog.get("worklogs") or [])
        if int(worklog.get("total") or 0) > len(logs):
            try:
                logs = self._paged(f"{self.base_url}/rest/api/3/issue/{key}/worklog", "worklogs")
            except SourceError:
                pass
        for log in logs:
            at = parse_iso(log.get("started") or log.get("created"))
            if at is None:
                continue
            name, email, account = _author(log.get("author"))
            minutes = float(log.get("timeSpentSeconds") or 0) / 60.0
            signals.append(Signal(id=signal_id("jira", key, "worklog", log.get("id")), at=at, source="jira", kind=KIND_WORKLOG,
                                  actor=name, actor_email=email, refs=[ref], weight=0.0, minutes=minutes,
                                  meta={"issue": key, "actor_key": account}))
        # comments
        comment = f.get("comment") or {}
        comments = list(comment.get("comments") or [])
        if int(comment.get("total") or 0) > len(comments):
            try:
                comments = self._paged(f"{self.base_url}/rest/api/3/issue/{key}/comment", "comments")
            except SourceError:
                pass
        for row in comments:
            at = parse_iso(row.get("created"))
            if at is None:
                continue
            name, email, account = _author(row.get("author"))
            bot = _is_bot(name)
            signals.append(Signal(id=signal_id("jira", key, "comment", row.get("id")), at=at, source="jira", kind=KIND_COMMENT,
                                  actor=name, actor_email=email, refs=[ref], weight=0.0 if bot else self._w(KIND_COMMENT),
                                  meta={"issue": key, "chars": _adf_text_len(row.get("body")), "actor_key": account, "bot": bot}))
        parent = (f.get("parent") or {}).get("key")
        fact = {
            "created": created,
            "updated": parse_iso(f.get("updated")),
            "resolved": parse_iso(f.get("resolutiondate")),
            "status_since": parse_iso(f.get("statuscategorychangedate")),
            "type": ((f.get("issuetype") or {}).get("name") or ""),
            "status": ((f.get("status") or {}).get("name") or ""),
            "status_cat": categories.get(((f.get("status") or {}).get("name") or ""), ""),
            "parent": parent,
            "points": f.get("customfield_10016"),
            "priority": ((f.get("priority") or {}).get("name") or ""),
            "transitions": transitions,
            "assignee": ((f.get("assignee") or {}).get("displayName") or ""),
        }
        return signals, fact

    def collect(self, since: float, previous: CollectResult | None = None) -> CollectResult:
        result = CollectResult()
        prev_facts = (previous.facts if previous else None) or {}
        prev_issues: dict = dict(prev_facts.get("issues") or {})
        # Incremental window: re-read anything updated since the last high-water mark
        # (minus a day of slack for clock skew), else everything since `since`.
        watermark = prev_facts.get("max_updated")
        start = max(since, float(watermark) - 86400.0) if watermark else since
        since_day = datetime.fromtimestamp(start, timezone.utc).strftime("%Y-%m-%d")
        categories = prev_facts.get("status_categories") or self._status_categories()
        refetched: set[str] = set()
        max_updated = float(watermark or 0.0)
        issues_facts: dict = {}
        pages_ok = True
        try:
            for issue in self._search(since_day):
                key = issue.get("key")
                if not key:
                    continue
                try:
                    signals, fact = self._issue_signals(issue, categories)
                except Exception as exc:  # noqa: BLE001 - one bad issue must not sink the run
                    result.notes.append(f"{key}: {exc}")
                    continue
                refetched.add(key)
                issues_facts[key] = fact
                if fact.get("updated"):
                    max_updated = max(max_updated, fact["updated"])
                result.signals.extend(s for s in signals if s.at >= since)
        except SourceError as exc:
            pages_ok = False
            result.truncated = True
            result.notes.append(f"stopped early: {exc}")
        # Carry over everything not re-fetched.
        if previous:
            for signal in previous.signals:
                issue = signal.meta.get("issue")
                if issue and issue not in refetched and signal.at >= since:
                    result.signals.append(signal)
            for key, fact in prev_issues.items():
                if key not in refetched:
                    issues_facts[key] = fact
        result.facts = {
            "issues": issues_facts,
            "status_categories": categories,
            "max_updated": max_updated if pages_ok else watermark,
            "refetched": len(refetched),
            "collected_at": time.time(),
        }
        return result
