"""Tests for the Jira and ADO history adapters, with the HTTP seam replaced."""

import unittest

from pm_studio.signals.model import KIND_ASSIGNEE, KIND_COMMENT, KIND_CREATED, KIND_FIELD, KIND_PR_MERGED, KIND_PR_OPENED, KIND_PR_REVIEW, KIND_STATUS, KIND_WORKLOG
from pm_studio.signals.sources.ado import AdoHistorySource, AdoPullRequestSource, split_identity
from pm_studio.signals.sources.jira import JiraHistorySource

JIRA_ISSUE = {
    "key": "NDT-1",
    "fields": {
        "created": "2026-01-01T09:00:00.000-0500", "updated": "2026-01-05T09:00:00.000-0500", "resolutiondate": "2026-01-05T09:00:00.000-0500",
        "statuscategorychangedate": "2026-01-05T09:00:00.000-0500",
        "reporter": {"displayName": "Ada Lovelace", "accountId": "a1"}, "issuetype": {"name": "Story"}, "status": {"name": "Done"},
        "parent": {"key": "NDT-0"}, "customfield_10016": 3,
        "worklog": {"total": 1, "worklogs": [{"id": "w1", "started": "2026-01-03T10:00:00.000-0500", "timeSpentSeconds": 5400, "author": {"displayName": "Ada Lovelace", "accountId": "a1"}}]},
        "comment": {"total": 1, "comments": [{"id": "c1", "created": "2026-01-02T10:00:00.000-0500", "author": {"displayName": "Bob", "accountId": "b1"}, "body": {"content": [{"text": "looks good"}]}}]},
    },
    "changelog": {"total": 3, "histories": [
        {"id": "h1", "created": "2026-01-02T09:00:00.000-0500", "author": {"displayName": "Ada Lovelace", "accountId": "a1"}, "items": [{"field": "status", "fromString": "To Do", "toString": "In Progress"}, {"field": "Rank", "toString": "x"}]},
        {"id": "h2", "created": "2026-01-02T09:30:00.000-0500", "author": {"displayName": "Automation for Jira"}, "items": [{"field": "assignee", "toString": "Ada Lovelace"}]},
        {"id": "h3", "created": "2026-01-05T09:00:00.000-0500", "author": {"displayName": "Ada Lovelace", "accountId": "a1"}, "items": [{"field": "status", "fromString": "In Progress", "toString": "Done"}, {"field": "Sprint", "toString": "S1"}]},
    ]},
}


class JiraSourceTest(unittest.TestCase):
    def fetch(self, url, headers):
        self.calls.append(url)
        if "/rest/api/3/status" in url:
            return {"value": [{"name": "To Do", "statusCategory": {"key": "new"}}, {"name": "In Progress", "statusCategory": {"key": "indeterminate"}}, {"name": "Done", "statusCategory": {"key": "done"}}]}
        if "/search/jql" in url:
            return {"issues": [JIRA_ISSUE], "isLast": True}
        raise AssertionError(url)

    def setUp(self) -> None:
        self.calls = []
        self.source = JiraHistorySource("jira", "https://jira.example", ("NDT",), "me@x", "tok", fetch=self.fetch)

    def test_history_becomes_signals(self) -> None:
        result = self.source.collect(0.0)
        kinds = sorted(s.kind for s in result.signals)
        self.assertEqual(kinds, sorted([KIND_CREATED, KIND_STATUS, KIND_ASSIGNEE, KIND_STATUS, KIND_FIELD, KIND_WORKLOG, KIND_COMMENT]))
        statuses = [s for s in result.signals if s.kind == KIND_STATUS]
        self.assertEqual(statuses[0].meta["to_cat"], "in_progress")
        self.assertEqual(statuses[1].meta["to_cat"], "done")
        bot = next(s for s in result.signals if s.kind == KIND_ASSIGNEE)
        self.assertEqual(bot.weight, 0.0)  # automation carries no effort
        worklog = next(s for s in result.signals if s.kind == KIND_WORKLOG)
        self.assertEqual(worklog.minutes, 90.0)
        self.assertTrue(all(s.refs == ["jira:NDT-1"] for s in result.signals))
        fact = result.facts["issues"]["NDT-1"]
        self.assertEqual(fact["parent"], "NDT-0")
        self.assertEqual(len(fact["transitions"]), 2)
        self.assertEqual(result.facts["status_categories"]["Done"], "done")
        self.assertIn("Basic ", "Basic x")

    def test_incremental_carries_over_unfetched_issues(self) -> None:
        first = self.source.collect(0.0)
        def fetch_empty(url, headers):
            if "/search/jql" in url:
                return {"issues": [], "isLast": True}
            return self.fetch(url, headers)
        second_source = JiraHistorySource("jira", "https://jira.example", ("NDT",), "me@x", "tok", fetch=fetch_empty)
        second = second_source.collect(0.0, first)
        self.assertEqual(len(second.signals), len(first.signals))
        self.assertEqual(second.facts["issues"], first.facts["issues"])
        self.assertEqual(second.facts["refetched"], 0)

    def test_since_drops_old_signals(self) -> None:
        result = self.source.collect(1_900_000_000.0)
        self.assertEqual(result.signals, [])


REVISIONS = [
    {"id": 7, "rev": 1, "fields": {"System.Id": 7, "System.WorkItemType": "Task", "System.State": "New", "System.ChangedDate": "2026-02-01T10:00:00Z", "System.ChangedBy": "Ada Lovelace <ada@x.com>", "System.CreatedDate": "2026-02-01T10:00:00Z", "System.Title": "T"}},
    {"id": 7, "rev": 2, "fields": {"System.Id": 7, "System.WorkItemType": "Task", "System.State": "Active", "System.AssignedTo": "Bob Byte <bob@x.com>", "System.ChangedDate": "2026-02-02T10:00:00Z", "System.ChangedBy": "Ada Lovelace <ada@x.com>", "System.CreatedDate": "2026-02-01T10:00:00Z", "System.Title": "T"}},
    {"id": 7, "rev": 3, "fields": {"System.Id": 7, "System.WorkItemType": "Task", "System.State": "Active", "System.AssignedTo": "Bob Byte <bob@x.com>", "Microsoft.VSTS.Scheduling.CompletedWork": 2.5, "System.ChangedDate": "2026-02-03T10:00:00Z", "System.ChangedBy": "Bob Byte <bob@x.com>", "System.CreatedDate": "2026-02-01T10:00:00Z", "System.Title": "T"}, "commentVersionRef": {"commentId": 1}},
    {"id": 7, "rev": 4, "fields": {"System.Id": 7, "System.WorkItemType": "Task", "System.State": "Closed", "System.AssignedTo": "Bob Byte <bob@x.com>", "Microsoft.VSTS.Scheduling.CompletedWork": 2.5, "System.ChangedDate": "2026-02-04T10:00:00Z", "System.ChangedBy": "Bob Byte <bob@x.com>", "System.CreatedDate": "2026-02-01T10:00:00Z", "Microsoft.VSTS.Common.ClosedDate": "2026-02-04T10:00:00Z", "System.Title": "T2"}},
]


class AdoSourceTest(unittest.TestCase):
    def fetch(self, url, headers):
        if "workitemtypes" in url:
            return {"value": [{"name": "Task", "states": [{"name": "New", "category": "Proposed"}, {"name": "Active", "category": "InProgress"}, {"name": "Closed", "category": "Completed"}]}]}
        if "workitemrevisions" in url:
            if "continuationToken=" in url:
                return {"values": [], "isLastBatch": True, "continuationToken": "tok-2"}
            return {"values": REVISIONS, "isLastBatch": True, "continuationToken": "tok-1"}
        raise AssertionError(url)

    def test_revisions_are_diffed(self) -> None:
        source = AdoHistorySource("ado", "https://ado.example/org", ("Proj",), "pat", fetch=self.fetch)
        result = source.collect(0.0)
        kinds = [s.kind for s in sorted(result.signals, key=lambda s: s.at)]
        self.assertEqual(kinds, [KIND_CREATED, KIND_STATUS, KIND_ASSIGNEE, KIND_WORKLOG, KIND_COMMENT, KIND_STATUS])
        worklog = next(s for s in result.signals if s.kind == KIND_WORKLOG)
        self.assertEqual(worklog.minutes, 150.0)
        self.assertEqual(worklog.actor_email, "bob@x.com")  # booked to the assignee
        done = [s for s in result.signals if s.kind == KIND_STATUS][-1]
        self.assertEqual(done.meta["to_cat"], "done")
        self.assertEqual(result.facts["continuations"]["Proj"], "tok-1")
        fact = result.facts["issues"]["7"]
        self.assertEqual(fact["status_cat"], "done")
        self.assertEqual(len(fact["transitions"]), 2)

    def test_second_run_uses_the_continuation_and_keeps_history(self) -> None:
        source = AdoHistorySource("ado", "https://ado.example/org", ("Proj",), "pat", fetch=self.fetch)
        first = source.collect(0.0)
        self.assertNotIn("raw", first.facts)  # only the last state per item is kept
        self.assertEqual(first.facts["items"]["Proj\x1f7"]["rev"], 4)
        second = source.collect(0.0, first)
        self.assertEqual(len(second.signals), len(first.signals))
        self.assertEqual(second.facts["touched"], 0)

    def test_new_revision_diffs_against_stored_state(self) -> None:
        source = AdoHistorySource("ado", "https://ado.example/org", ("Proj",), "pat", fetch=self.fetch)
        first = source.collect(0.0)
        reopened = {"id": 7, "rev": 5, "fields": {**REVISIONS[3]["fields"], "System.State": "Active", "System.ChangedDate": "2026-02-05T10:00:00Z", "System.ChangedBy": "Ada Lovelace <ada@x.com>"}}
        def fetch(url, headers):
            if "workitemrevisions" in url:
                return {"values": [reopened], "isLastBatch": True, "continuationToken": "tok-3"}
            return self.fetch(url, headers)
        again = AdoHistorySource("ado", "https://ado.example/org", ("Proj",), "pat", fetch=fetch)
        second = again.collect(0.0, first)
        self.assertEqual(len(second.signals), len(first.signals) + 1)
        newest = max(second.signals, key=lambda s: s.at)
        self.assertEqual((newest.kind, newest.meta["from"], newest.meta["to"]), (KIND_STATUS, "Closed", "Active"))
        self.assertEqual(len(second.facts["issues"]["7"]["transitions"]), 3)
        self.assertEqual(second.facts["items"]["Proj\x1f7"]["rev"], 5)
        self.assertEqual(second.facts["continuations"]["Proj"], "tok-3")

    def test_identity_split(self) -> None:
        self.assertEqual(split_identity("Ada Lovelace <ada@X.com>"), ("Ada Lovelace", "ada@x.com"))
        self.assertEqual(split_identity({"displayName": "Bob", "uniqueName": "bob@x.com"}), ("Bob", "bob@x.com"))
        self.assertEqual(split_identity("Nobody"), ("Nobody", ""))


class AdoPullRequestTest(unittest.TestCase):
    def test_second_run_queries_incrementally_and_carries_unchanged_prs(self) -> None:
        pr = {"pullRequestId": 5, "title": "#12099 fix", "sourceRefName": "refs/heads/x", "targetRefName": "refs/heads/master", "status": "completed", "creationDate": "2026-03-01T10:00:00Z", "closedDate": "2026-03-02T10:00:00Z", "createdBy": {"displayName": "Ada", "uniqueName": "ada@x.com"}, "repository": {"name": "R"}, "reviewers": []}
        urls = []
        def fetch(url, headers):
            urls.append(url)
            return {"value": [pr]} if "queryTimeRangeType=created" in url and "status=all" in url else {"value": []}
        source = AdoPullRequestSource("ado", "https://ado.example/org", ("Proj",), "pat", fetch=fetch)
        first = source.collect(0.0)
        self.assertEqual(len(first.signals), 2)
        self.assertEqual(len(urls), 1)  # first run: one created-since query
        urls.clear()
        def fetch_quiet(url, headers):
            urls.append(url)
            return {"value": []}
        again = AdoPullRequestSource("ado", "https://ado.example/org", ("Proj",), "pat", fetch=fetch_quiet)
        second = again.collect(0.0, first)
        self.assertEqual(len(urls), 4)  # created / completed / abandoned since watermark + active
        self.assertTrue(any("queryTimeRangeType=closed" in u for u in urls))
        self.assertEqual(len(second.signals), 2)  # carried over
        self.assertEqual(second.facts["carried"], 2)

    def test_prs_become_open_merge_review_signals(self) -> None:
        def fetch(url, headers):
            return {"value": [{"pullRequestId": 5, "title": "ticket #12099 fix", "sourceRefName": "refs/heads/12099/feat", "targetRefName": "refs/heads/master", "status": "completed",
                               "creationDate": "2026-03-01T10:00:00Z", "closedDate": "2026-03-02T10:00:00Z", "createdBy": {"displayName": "Ada", "uniqueName": "ada@x.com"},
                               "repository": {"name": "Services.Claims"},
                               "reviewers": [{"displayName": "Bob", "uniqueName": "bob@x.com", "vote": 10}, {"displayName": "Ada", "uniqueName": "ada@x.com", "vote": 10}, {"displayName": "Team", "uniqueName": "t", "vote": 10, "isContainer": True}]}]}
        source = AdoPullRequestSource("ado", "https://ado.example/org", ("Proj",), "pat", repo_resolver=lambda name: ("src/capadmin/" + name, "capadmin-stack"), fetch=fetch)
        result = source.collect(0.0)
        kinds = sorted(s.kind for s in result.signals)
        self.assertEqual(kinds, sorted([KIND_PR_OPENED, KIND_PR_MERGED, KIND_PR_REVIEW]))
        self.assertTrue(all(s.refs == ["ado:12099"] for s in result.signals))
        merged = next(s for s in result.signals if s.kind == KIND_PR_MERGED)
        self.assertEqual(merged.meta["hours_open"], 24.0)
        self.assertEqual(merged.repo, "src/capadmin/Services.Claims")
        review = next(s for s in result.signals if s.kind == KIND_PR_REVIEW)
        self.assertEqual(review.actor_email, "bob@x.com")  # the author's own vote is not a review
