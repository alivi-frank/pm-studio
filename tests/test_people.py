"""Tests for ticket assignees and the people directory.

Three seams, each pinned here:

- **Parsing.** What each provider actually sends for "who is on this" - Jira's identity
  object with or without an email address, ADO's identity object AND its legacy
  `"Name <addr>"` string - lands on the Ticket in the same two fields either way.
- **Reconciliation.** One human, however many trackers name them, and - just as
  important - two humans staying two when one tracker gives two accounts the same display
  name. A wrong merge attributes somebody's work to somebody else, so the rule that
  prevents it is tested directly rather than implied.
- **The join and the load.** Which of the two possible assignees wins on a change, when a
  disagreement is reported, and what a load table counts (open work only, unassigned work
  as its own row).

No test here touches the network or a real tracker.
"""

import tempfile
import unittest
from pathlib import Path

from pm_studio import people as people_module
from pm_studio import roadmap as roadmap_module
from pm_studio.config import TrackerConfig
from pm_studio.people import (
    UNASSIGNED,
    PeopleError,
    PeopleStore,
    effective_assignee,
    normalize_name,
    workload,
)
from pm_studio.roadmap import RoadmapItem, RoadmapStore
from pm_studio.trackers import AdoClient, JiraClient, Ticket

JIRA = TrackerConfig(
    id="jira",
    provider="jira",
    label="Acme Jira",
    base_url="https://acme.atlassian.net",
    projects=("PROJ",),
    username="pm@acme.com",
    token="jira-token-secret",
)
ADO = TrackerConfig(
    id="ado",
    provider="ado",
    label="Acme ADO",
    base_url="https://dev.azure.com/acme",
    projects=("Platform",),
    token="ado-pat-secret",
    organization="acme",
)


class AssigneeParsingTest(unittest.TestCase):
    def test_jira_assignee(self) -> None:
        ticket = JiraClient(JIRA)._ticket(
            {
                "key": "PROJ-1",
                "fields": {
                    "summary": "Rate limiting",
                    "issuetype": {"name": "Story"},
                    "status": {"name": "In Progress"},
                    "assignee": {
                        "accountId": "5b10a2844c20165700ede21g",
                        "displayName": "Dana Reyes",
                        "emailAddress": "dana@example.com",
                    },
                },
            },
            now=1.0,
        )
        self.assertEqual(ticket.assignee, "Dana Reyes")
        self.assertEqual(ticket.assignee_key, "5b10a2844c20165700ede21g")
        self.assertEqual(ticket.assignee_email, "dana@example.com")

    def test_jira_private_profile_keeps_the_name(self) -> None:
        """An instance with addresses hidden sends no emailAddress. The accountId still
        identifies them; the name is what a human reads."""
        ticket = JiraClient(JIRA)._ticket(
            {
                "key": "PROJ-2",
                "fields": {
                    "summary": "Audit log",
                    "issuetype": {"name": "Task"},
                    "status": {"name": "To Do"},
                    "assignee": {"accountId": "acct-9", "displayName": "Lee Morgan"},
                },
            },
            now=1.0,
        )
        self.assertEqual(ticket.assignee, "Lee Morgan")
        self.assertEqual(ticket.assignee_key, "acct-9")
        self.assertEqual(ticket.assignee_email, "")

    def test_jira_unassigned(self) -> None:
        """`assignee: null` is the common case and must not raise."""
        ticket = JiraClient(JIRA)._ticket(
            {
                "key": "PROJ-3",
                "fields": {
                    "summary": "Nobody's yet",
                    "issuetype": {"name": "Story"},
                    "status": {"name": "To Do"},
                    "assignee": None,
                },
            },
            now=1.0,
        )
        self.assertEqual((ticket.assignee, ticket.assignee_key), ("", ""))

    def test_ado_identity_object(self) -> None:
        ticket = AdoClient(ADO)._ticket(
            {
                "id": 4102,
                "fields": {
                    "System.Title": "Retry policy",
                    "System.WorkItemType": "User Story",
                    "System.State": "Active",
                    "System.AssignedTo": {
                        "displayName": "Dana Reyes",
                        "uniqueName": "dana@example.com",
                    },
                },
            },
            project="Platform",
            now=1.0,
        )
        self.assertEqual(ticket.assignee, "Dana Reyes")
        self.assertEqual(ticket.assignee_key, "dana@example.com")
        self.assertEqual(ticket.assignee_email, "dana@example.com")

    def test_ado_legacy_identity_string(self) -> None:
        """Older on-premises collections send one string, not an object."""
        ticket = AdoClient(ADO)._ticket(
            {
                "id": 4103,
                "fields": {
                    "System.Title": "Backfill",
                    "System.WorkItemType": "Task",
                    "System.State": "New",
                    "System.AssignedTo": "Sam Okafor <sam@example.com>",
                },
            },
            project="Platform",
            now=1.0,
        )
        self.assertEqual(ticket.assignee, "Sam Okafor")
        self.assertEqual(ticket.assignee_key, "sam@example.com")
        self.assertEqual(ticket.assignee_email, "sam@example.com")

    def test_ado_identity_string_without_address(self) -> None:
        ticket = AdoClient(ADO)._ticket(
            {
                "id": 4104,
                "fields": {
                    "System.Title": "Cleanup",
                    "System.WorkItemType": "Task",
                    "System.State": "New",
                    "System.AssignedTo": "Sam Okafor",
                },
            },
            project="Platform",
            now=1.0,
        )
        # The name has to carry the identity when nothing else is offered.
        self.assertEqual((ticket.assignee, ticket.assignee_key), ("Sam Okafor", "Sam Okafor"))
        self.assertEqual(ticket.assignee_email, "")

    def test_catalog_written_before_assignees_still_loads(self) -> None:
        """The additive-field contract: a cached ticket with no assignee keys loads and
        simply reports nobody."""
        ticket = Ticket.from_dict(
            {
                "tracker_id": "jira",
                "provider": "jira",
                "key": "PROJ-9",
                "type": "story",
                "raw_type": "Story",
                "title": "Old cache entry",
                "state": "To Do",
                "url": "https://acme.atlassian.net/browse/PROJ-9",
                "synced_at": 1.0,
            }
        )
        self.assertEqual((ticket.assignee, ticket.assignee_key, ticket.assignee_email), ("", "", ""))


class ReconcileTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = PeopleStore(Path(self._tmp.name) / "people.json")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_creates_one_person_per_new_identity(self) -> None:
        report = self.store.reconcile(
            [
                ("jira", "acct-1", "Dana Reyes", "dana@example.com"),
                ("jira", "acct-2", "Lee Morgan", ""),
            ]
        )
        self.assertEqual(report, {"created": 2, "attached": 0})
        self.assertEqual(len(self.store.list_people()), 2)

    def test_reconcile_is_idempotent(self) -> None:
        rows = [("jira", "acct-1", "Dana Reyes", "dana@example.com")]
        self.store.reconcile(rows)
        self.assertEqual(self.store.reconcile(rows), {"created": 0, "attached": 0})
        self.assertEqual(len(self.store.list_people()), 1)

    def test_email_joins_two_trackers(self) -> None:
        self.store.reconcile([("jira", "acct-1", "Dana Reyes", "dana@example.com")])
        report = self.store.reconcile(
            [("ado", "dana@example.com", "Reyes, Dana", "dana@example.com")]
        )
        self.assertEqual(report, {"created": 0, "attached": 1})
        people = self.store.list_people()
        self.assertEqual(len(people), 1)
        self.assertEqual(people[0]["trackers"], ["ado", "jira"])
        # Either tracker's identity resolves to the same person.
        self.assertEqual(
            self.store.identity("jira", "acct-1").id,
            self.store.identity("ado", "dana@example.com").id,
        )

    def test_display_name_joins_across_trackers(self) -> None:
        """Both instances hiding addresses leaves the name as the only signal."""
        self.store.reconcile([("jira", "acct-1", "Dana Reyes", "")])
        report = self.store.reconcile([("ado", "ado-77", "dana reyes", "")])
        self.assertEqual(report, {"created": 0, "attached": 1})
        self.assertEqual(len(self.store.list_people()), 1)

    def test_same_display_name_within_one_tracker_stays_two_people(self) -> None:
        """Two accounts on ONE tracker sharing a display name are two humans. Merging them
        would attribute one person's work to the other, which is worse than a duplicate
        row somebody can merge by hand."""
        self.store.reconcile(
            [
                ("jira", "acct-1", "Dana Reyes", ""),
                ("jira", "acct-2", "Dana Reyes", ""),
            ]
        )
        self.assertEqual(len(self.store.list_people()), 2)

    def test_unassigned_rows_are_skipped(self) -> None:
        self.store.reconcile([("jira", "", "", "")])
        self.assertEqual(self.store.list_people(), [])

    def test_normalize_name_keeps_word_order(self) -> None:
        self.assertEqual(normalize_name("Dana O'Neil"), "dana oneil")
        self.assertEqual(normalize_name("  DANA   REYES "), "dana reyes")
        # Reordering names is how two different people become one by accident.
        self.assertNotEqual(normalize_name("Lee Morgan"), normalize_name("Morgan Lee"))

    def test_merge_and_split(self) -> None:
        self.store.reconcile(
            [
                ("jira", "acct-1", "Dana Reyes", ""),
                ("ado", "ado-77", "D. Reyes", ""),
            ]
        )
        first, second = [p["id"] for p in self.store.list_people()]
        merged = self.store.merge(first, second)
        self.assertEqual(len(merged.identities), 2)
        self.assertEqual(len(self.store.list_people()), 1)

        split = self.store.split_identity(merged.id, "ado", "ado-77")
        self.assertEqual(split.name, "D. Reyes")
        self.assertEqual(len(self.store.list_people()), 2)
        self.assertEqual(len(self.store.get(merged.id).identities), 1)

    def test_split_refuses_the_only_identity(self) -> None:
        self.store.reconcile([("jira", "acct-1", "Dana Reyes", "")])
        person = self.store.list_people()[0]
        with self.assertRaises(PeopleError):
            self.store.split_identity(person["id"], "jira", "acct-1")

    def test_delete_refuses_a_tracker_person(self) -> None:
        """They would come back on the next sync, so `inactive` is the honest retirement."""
        self.store.reconcile([("jira", "acct-1", "Dana Reyes", "")])
        person = self.store.list_people()[0]
        with self.assertRaises(PeopleError):
            self.store.delete(person["id"])
        retired = self.store.update(person["id"], status="inactive")
        self.assertEqual(retired.status, "inactive")

    def test_local_person_lifecycle(self) -> None:
        person = self.store.create_local("  Priya Raman ", "PRIYA@example.com ")
        self.assertEqual(person.name, "Priya Raman")
        self.assertEqual(person.email, "priya@example.com")
        self.assertEqual(person.source, "local")
        with self.assertRaises(PeopleError):
            self.store.create_local("Someone Else", "priya@example.com")
        with self.assertRaises(PeopleError):
            self.store.create_local("   ")
        self.store.delete(person.id)
        self.assertEqual(self.store.list_people(), [])

    def test_account_link_clears_with_empty_string(self) -> None:
        person = self.store.create_local("Priya Raman")
        self.assertEqual(self.store.update(person.id, account_id="user-1").account_id, "user-1")
        self.assertEqual(self.store.update(person.id, name="Priya R.").account_id, "user-1")
        self.assertIsNone(self.store.update(person.id, account_id="").account_id)

    def test_lookup_index_survives_every_mutation(self) -> None:
        """`identity()` reads an index rather than scanning (it runs once per change on
        every board read). Merge, split and a second reconcile all move identities between
        people, so the index has to be right after each one - a stale entry would attribute
        somebody's work to the wrong person, silently."""
        self.store.reconcile(
            [
                ("jira", "acct-1", "Dana Reyes", ""),
                ("ado", "ado-77", "D. Reyes", ""),
            ]
        )
        first, second = [p["id"] for p in self.store.list_people()]
        merged = self.store.merge(first, second)
        self.assertEqual(self.store.identity("ado", "ado-77").id, merged.id)
        self.assertEqual(self.store.identity("jira", "acct-1").id, merged.id)

        split = self.store.split_identity(merged.id, "ado", "ado-77")
        self.assertEqual(self.store.identity("ado", "ado-77").id, split.id)
        self.assertEqual(self.store.identity("jira", "acct-1").id, merged.id)

        # A retired person still resolves - their name has to keep appearing on the work
        # they did.
        self.store.update(merged.id, status="inactive")
        self.assertEqual(self.store.identity("jira", "acct-1").id, merged.id)

    def test_one_new_assignee_on_many_tickets_makes_one_person(self) -> None:
        """The within-pass index update: the second row must find what the first created."""
        self.store.reconcile(
            [
                ("jira", "acct-1", "Dana Reyes", ""),
                ("jira", "acct-1", "Dana Reyes", ""),
                ("jira", "acct-1", "Dana Reyes", ""),
            ]
        )
        self.assertEqual(len(self.store.list_people()), 1)

    def test_a_name_match_does_not_swallow_the_new_key(self) -> None:
        """Attaching by display name must RECORD the new tracker's key, or the same identity
        re-matches by name on every future sync and never settles."""
        self.store.reconcile([("jira", "acct-1", "Dana Reyes", "")])
        self.store.reconcile([("ado", "ado-77", "Dana Reyes", "")])
        self.assertEqual(self.store.reconcile([("ado", "ado-77", "Dana Reyes", "")]),
                         {"created": 0, "attached": 0})
        person = self.store.identity("ado", "ado-77")
        self.assertEqual(
            sorted((i.tracker_id, i.key) for i in person.identities),
            [("ado", "ado-77"), ("jira", "acct-1")],
        )

    def test_directory_survives_a_reload(self) -> None:
        self.store.reconcile([("jira", "acct-1", "Dana Reyes", "dana@example.com")])
        reloaded = PeopleStore(self.store._path)
        self.assertEqual(len(reloaded.list_people()), 1)
        self.assertIsNotNone(reloaded.identity("jira", "acct-1"))


class EffectiveAssigneeTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = PeopleStore(Path(self._tmp.name) / "people.json")
        self.store.reconcile([("jira", "acct-1", "Dana Reyes", "dana@example.com")])
        self.dana = self.store.identity("jira", "acct-1")
        self.priya = self.store.create_local("Priya Raman")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _ticket(self, **over) -> dict:
        return {
            "tracker_id": "jira",
            "key": "PROJ-1",
            "assignee": "Dana Reyes",
            "assignee_key": "acct-1",
            **over,
        }

    def test_tracker_assignee_when_nothing_local(self) -> None:
        assigned = effective_assignee({"assignee": None}, self._ticket(), self.store)
        self.assertEqual(assigned["person_id"], self.dana.id)
        self.assertEqual(assigned["source"], "tracker")
        self.assertFalse(assigned["conflict"])

    def test_local_assignment_wins_and_reports_the_disagreement(self) -> None:
        assigned = effective_assignee({"assignee": self.priya.id}, self._ticket(), self.store)
        self.assertEqual(assigned["person_id"], self.priya.id)
        self.assertEqual(assigned["source"], "local")
        self.assertTrue(assigned["conflict"])
        # The tracker's own answer travels alongside, so the UI can name both.
        self.assertEqual(assigned["tracker_name"], "Dana Reyes")

    def test_agreement_is_not_a_conflict(self) -> None:
        assigned = effective_assignee({"assignee": self.dana.id}, self._ticket(), self.store)
        self.assertEqual(assigned["source"], "local")
        self.assertFalse(assigned["conflict"])

    def test_unlinked_change_uses_only_the_local_assignment(self) -> None:
        assigned = effective_assignee({"assignee": self.priya.id}, None, self.store)
        self.assertEqual(assigned["person_id"], self.priya.id)
        self.assertEqual(assigned["tracker_name"], "")
        self.assertFalse(assigned["conflict"])

    def test_nobody(self) -> None:
        assigned = effective_assignee({"assignee": None}, None, self.store)
        self.assertIsNone(assigned["person_id"])
        self.assertIsNone(assigned["source"])
        self.assertEqual(assigned["name"], "")

    def test_stale_local_id_is_reported_not_swallowed(self) -> None:
        """A change naming somebody the directory no longer has must not read as
        unassigned - that looks identical to a bug in the join."""
        assigned = effective_assignee({"assignee": "gone-1234"}, None, self.store)
        self.assertIsNone(assigned["person_id"])
        self.assertEqual(assigned["assignee_id"], "gone-1234")

    def test_name_only_tracker_identity_still_resolves(self) -> None:
        """A Jira instance with private profiles on an old catalog gives a name and no
        key; the identity has to keep resolving on the name."""
        assigned = effective_assignee(
            {"assignee": None},
            self._ticket(assignee_key="", assignee="dana reyes"),
            self.store,
        )
        self.assertEqual(assigned["person_id"], self.dana.id)


class WorkloadTest(unittest.TestCase):
    def _change(self, person, bucket="now", **over) -> dict:
        return {
            "product": "web",
            "bucket": bucket,
            "status": "pending",
            "is_overdue": False,
            "assigned": {"person_id": person, "name": (person or "").title(), "status": "active"},
            **over,
        }

    def test_counts_open_work_only(self) -> None:
        rows = workload(
            [
                self._change("dana", "now"),
                self._change("dana", "next"),
                self._change("dana", "later", status="done"),
            ]
        )
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual((row["open"], row["now"], row["next"], row["later"]), (2, 1, 1, 0))
        # Finished work is reported, never counted as load.
        self.assertEqual(row["shipped"], 1)

    def test_initiative_spread_counts_distinct_initiatives(self) -> None:
        # Two changes in one initiative and one in another is a spread of two, ordered
        # biggest first - the count is what "how many things are you carrying" means.
        rows = workload(
            [
                self._change("dana", "now", project_id="p1"),
                self._change("dana", "next", project_id="p2"),
                self._change("dana", "now", project_id="p3"),
            ],
            {"p1": "Telemetry", "p2": "Telemetry", "p3": "Billing"},
        )
        self.assertEqual(
            rows[0]["initiatives"],
            [{"id": "Telemetry", "count": 2}, {"id": "Billing", "count": 1}],
        )

    def test_initiative_spread_ignores_unaligned_and_shipped_work(self) -> None:
        # An unmapped project is unaligned work: absence of an initiative, not one more.
        # Shipped work is history and never counts as load, here as everywhere else.
        rows = workload(
            [
                self._change("dana", "now", project_id="p1"),
                self._change("dana", "now", project_id="orphan"),
                self._change("dana", "now", project_id=None),
                self._change("dana", "later", project_id="p2", status="done"),
            ],
            {"p1": "Telemetry", "p2": "Billing"},
        )
        self.assertEqual(rows[0]["initiatives"], [{"id": "Telemetry", "count": 1}])

    def test_initiative_spread_is_empty_without_the_map(self) -> None:
        rows = workload([self._change("dana", "now", project_id="p1")])
        self.assertEqual(rows[0]["initiatives"], [])

    def test_overdue_and_in_progress(self) -> None:
        row = workload(
            [self._change("dana", "now", status="in_progress", is_overdue=True)]
        )[0]
        self.assertEqual((row["in_progress"], row["overdue"]), (1, 1))

    def test_unassigned_is_its_own_row_and_sorts_last(self) -> None:
        rows = workload(
            [
                self._change(None),
                self._change(None),
                self._change(None),
                self._change("dana"),
            ]
        )
        self.assertEqual([r["person_id"] for r in rows], ["dana", UNASSIGNED])
        # Last despite being the biggest pile: it is work to hand out, not somebody's load.
        self.assertEqual(rows[-1]["open"], 3)

    def test_heaviest_person_first(self) -> None:
        rows = workload(
            [self._change("lee"), self._change("dana"), self._change("dana")]
        )
        self.assertEqual([r["person_id"] for r in rows], ["dana", "lee"])

    def test_products_and_areas_are_counted_and_ordered(self) -> None:
        row = workload(
            [
                self._change("dana", product="web", system="core",
                            ticket={"components": ["Billing", "Portal"]}),
                self._change("dana", product="web", ticket={"components": ["Billing"]}),
                self._change("dana", product="platform"),
            ]
        )[0]
        self.assertEqual(row["products"], [{"id": "web", "count": 2}, {"id": "platform", "count": 1}])
        self.assertEqual(row["areas"], [{"id": "Billing", "count": 2}, {"id": "Portal", "count": 1}])
        self.assertEqual(row["systems"], [{"id": "core", "count": 1}])

    def test_a_change_with_no_join_still_counts(self) -> None:
        """Defensive: a caller that forgot `assigned` must not silently drop the change
        out of the totals."""
        rows = workload([{"product": "web", "bucket": "now", "status": "pending"}])
        self.assertEqual(rows[0]["person_id"], UNASSIGNED)


class RoadmapAssigneeTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_dir = roadmap_module.ROADMAP_DIR
        self._orig_products = roadmap_module.PRODUCTS
        roadmap_module.ROADMAP_DIR = Path(self._tmp.name)
        roadmap_module.PRODUCTS = {"web": "Web App", "platform": "Platform"}
        self.store = RoadmapStore()

    def tearDown(self) -> None:
        roadmap_module.ROADMAP_DIR = self._orig_dir
        roadmap_module.PRODUCTS = self._orig_products
        self._tmp.cleanup()

    def test_create_update_and_clear(self) -> None:
        item = self.store.create("web", "Rate limiting", assignee="  p-1  ")
        self.assertEqual(item.assignee, "p-1")
        # None = no change, "" = unassign. Same convention as owner.
        self.assertEqual(self.store.update(item.id, status="in_progress").assignee, "p-1")
        self.assertIsNone(self.store.update(item.id, assignee="").assignee)

    def test_assignee_is_not_owner(self) -> None:
        """The two fields mean different things, and setting one must never set the other -
        an assignee that read as an owner would silently stop the PM dispatching it."""
        item = self.store.create("web", "Rate limiting", assignee="p-1")
        self.assertIsNone(item.owner)
        deep = self.store.describe_own_product("web")
        self.assertNotIn("EXTERNAL", deep)

    def test_items_assigned_to(self) -> None:
        mine = self.store.create("web", "Mine", assignee="p-1")
        self.store.create("web", "Theirs", assignee="p-2")
        self.store.create("web", "Nobody's")
        self.assertEqual([i["id"] for i in self.store.items_assigned_to("p-1")], [mine.id])

    def test_pm_context_names_the_assignee(self) -> None:
        self.store.create("web", "Rate limiting", assignee="p-1")
        deep = self.store.describe_own_product("web", person_lookup=lambda pid: "Dana Reyes")
        self.assertIn("[assigned to Dana Reyes]", deep)

    def test_pm_context_falls_back_to_the_ticket(self) -> None:
        """A change nobody reassigned here still tells the PM who the tracker has on it."""
        item = self.store.create("web", "Retry policy")
        self.store.link_ticket(item.id, "jira", "PROJ-1")
        deep = self.store.describe_own_product(
            "web",
            ticket_lookup=lambda tid, key: {
                "raw_type": "Story", "state": "In Progress", "assignee": "Sam Okafor",
            },
        )
        self.assertIn("[assigned to Sam Okafor]", deep)

    def test_pm_context_says_nothing_when_nobody_is_on_it(self) -> None:
        self.store.create("web", "Unclaimed")
        self.assertNotIn("assigned to", self.store.describe_own_product("web"))

    def test_board_json_written_before_assignees_still_loads(self) -> None:
        path = Path(self._tmp.name) / "web.json"
        path.write_text(
            '[{"id": "abc", "product": "web", "title": "Old", "description": "",'
            ' "bucket": "later", "status": "pending", "origin_product": "web",'
            ' "triaged": true, "created_at": 1.0, "updated_at": 1.0}]'
        )
        reloaded = RoadmapStore()
        item = reloaded.get("abc")
        self.assertIsNotNone(item)
        self.assertIsNone(item.assignee)


class _StubTrackerStore:
    """The catalog the sync pass reads, without a tracker. Mirrors the stub in
    test_tracker_import.py - same two methods, for the same reason."""

    def __init__(self, tickets):
        self._tickets = tickets

    def tickets_of(self, tracker_id):
        return [t for t in self._tickets if t.tracker_id == tracker_id]

    def lookup(self, tracker_id, key):
        return next(
            (t for t in self._tickets if t.tracker_id == tracker_id and t.key == key), None
        )


def _assigned_ticket(key, assignee, assignee_key, tracker_id="jira", email="") -> Ticket:
    return Ticket(
        tracker_id=tracker_id,
        provider="jira",
        key=key,
        type="story",
        raw_type="Story",
        title=f"Ticket {key}",
        state="In Progress",
        url=f"https://x/{key}",
        synced_at=1.0,
        state_category="In Progress",
        project=key.rsplit("-", 1)[0] if "-" in key else "",
        assignee=assignee,
        assignee_key=assignee_key,
        assignee_email=email,
    )


class SyncAndJoinTest(unittest.TestCase):
    """The two server-side halves: the sync pass that fills the directory, and the
    read-time join that puts `assigned` on every change the board and the API hand out."""

    TRACKER = TrackerConfig(
        id="jira", provider="jira", label="Jira", base_url="https://x",
        projects=("PROJ",), token="t",
    )

    def setUp(self) -> None:
        import dataclasses

        from pm_studio import server as server_module

        self.server = server_module
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_roadmap = (roadmap_module.ROADMAP_DIR, roadmap_module.PRODUCTS)
        roadmap_module.ROADMAP_DIR = Path(self._tmp.name)
        roadmap_module.PRODUCTS = {"web": "Web App"}
        self.roadmap = RoadmapStore()
        self.people = PeopleStore(Path(self._tmp.name) / "people.json")
        self._orig_server = (
            server_module.CONFIG,
            server_module.tracker_store,
            server_module.roadmap_store,
            server_module.people_store,
        )
        server_module.CONFIG = dataclasses.replace(
            server_module.CONFIG, trackers=(self.TRACKER,)
        )
        server_module.roadmap_store = self.roadmap
        server_module.people_store = self.people

    def tearDown(self) -> None:
        (roadmap_module.ROADMAP_DIR, roadmap_module.PRODUCTS) = self._orig_roadmap
        (
            self.server.CONFIG,
            self.server.tracker_store,
            self.server.roadmap_store,
            self.server.people_store,
        ) = self._orig_server
        self._tmp.cleanup()

    def _sync(self, tickets) -> None:
        self.server.tracker_store = _StubTrackerStore(tickets)
        self.server._reconcile_people()

    def test_sync_fills_the_directory_from_assignees(self) -> None:
        self._sync(
            [
                _assigned_ticket("PROJ-1", "Dana Reyes", "acct-1", email="dana@example.com"),
                _assigned_ticket("PROJ-2", "Dana Reyes", "acct-1", email="dana@example.com"),
                _assigned_ticket("PROJ-3", "Lee Morgan", "acct-2"),
                _assigned_ticket("PROJ-4", "", ""),
            ]
        )
        names = sorted(p["name"] for p in self.people.list_people())
        self.assertEqual(names, ["Dana Reyes", "Lee Morgan"])

    def test_sync_never_removes_anybody(self) -> None:
        """An assignee who drops out of the sync window has left the query, not the
        history of the work they did."""
        self._sync([_assigned_ticket("PROJ-1", "Dana Reyes", "acct-1")])
        self._sync([_assigned_ticket("PROJ-2", "Lee Morgan", "acct-2")])
        self.assertEqual(len(self.people.list_people()), 2)

    def test_join_reports_the_tracker_assignee_on_a_linked_change(self) -> None:
        ticket = _assigned_ticket("PROJ-1", "Dana Reyes", "acct-1")
        self._sync([ticket])
        item = self.roadmap.create("web", "Rate limiting")
        self.roadmap.link_ticket(item.id, "jira", "PROJ-1")
        joined = self.server._with_ticket(self.roadmap.get(item.id).to_public_dict())
        self.assertEqual(joined["assigned"]["name"], "Dana Reyes")
        self.assertEqual(joined["assigned"]["source"], "tracker")

    def test_join_on_an_unlinked_change(self) -> None:
        """Every change carries `assigned`, linked or not - an endpoint that returned the
        key sometimes would have the board rendering a different card per response."""
        self.server.tracker_store = _StubTrackerStore([])
        item = self.roadmap.create("web", "Local only")
        joined = self.server._with_ticket(self.roadmap.get(item.id).to_public_dict())
        self.assertIn("assigned", joined)
        self.assertIsNone(joined["assigned"]["person_id"])

    def test_assignee_validation(self) -> None:
        from fastapi import HTTPException

        person = self.people.create_local("Priya Raman")
        self.assertEqual(self.server._validated_assignee(person.id), person.id)
        # None leaves it alone, "" clears it - the same three-state convention as owner.
        self.assertIsNone(self.server._validated_assignee(None))
        self.assertEqual(self.server._validated_assignee(""), "")
        # An id the directory does not know is refused, not stored: stored, it would render
        # as "assigned to somebody unknown" on that card forever.
        with self.assertRaises(HTTPException) as caught:
            self.server._validated_assignee("nope-1234")
        self.assertEqual(caught.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
