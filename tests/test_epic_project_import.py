"""Tests for the epic→project sync passes (_sync_epic_projects and
_assign_changes_to_epic_projects).

The contracts that would rot silently, in order of how expensive the rot is:

- NO DUPLICATION: a deployment that hand-created its projects while mirroring the
  tracker's epics must come out of the first pass with those projects LINKED, not
  twinned. Evidence (a project's changes' tickets parenting to one epic) outranks
  title matching, and ambiguity is reported rather than guessed.
- Idempotency: re-running the pass imports nothing twice - the 1:1 link is the dedupe.
- One ticket, one thing: an epic held by a change is never also imported as a project,
  and the change import never consumes a ticket a project holds - even when
  import_types accidentally include the epic rung.
- Assignment fills gaps only: a change whose ticket parents to a linked epic gains
  that project when it has none, and is never moved off a project a human chose.
"""

import dataclasses
import tempfile
import time
import unittest
from pathlib import Path

from pm_studio import roadmap as roadmap_module
from pm_studio import server as server_module
from pm_studio.config import TrackerConfig, TrackerRoute
from pm_studio.portfolio import PortfolioStore
from pm_studio.roadmap import RoadmapStore
from pm_studio.trackers import Ticket, canonical_type


def _ticket(
    key,
    raw_type,
    title="T",
    state="To Do",
    category="To Do",
    parent=None,
    parent_type=None,
    project="PROJ",
):
    return Ticket(
        tracker_id="jira",
        provider="jira",
        key=key,
        type=canonical_type(raw_type),
        raw_type=raw_type,
        title=title,
        state=state,
        url=f"https://x/browse/{key}",
        synced_at=time.time(),
        parent_key=parent,
        parent_type=parent_type,
        state_category=category,
        components=[],
        project=project,
    )


class _StubTrackerStore:
    def __init__(self, tickets):
        self._tickets = list(tickets)

    def tickets_of(self, tracker_id):
        return [t for t in self._tickets if t.tracker_id == tracker_id]


class EpicProjectImportTest(unittest.TestCase):
    TRACKER = TrackerConfig(
        id="jira", provider="jira", label="Jira", base_url="https://x",
        projects=("PROJ",), token="t",
        import_types=("Story", "Epic"),
        routes=(TrackerRoute(project="PROJ", product="checkout"),),
    )

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self._orig_roadmap = (
            roadmap_module.ROADMAP_DIR,
            roadmap_module.PRODUCTS,
            roadmap_module.SYSTEMS,
            roadmap_module.PRODUCT_SYSTEMS,
        )
        roadmap_module.ROADMAP_DIR = root / "roadmap"
        roadmap_module.PRODUCTS = {"checkout": "Checkout"}
        roadmap_module.SYSTEMS = {}
        roadmap_module.PRODUCT_SYSTEMS = {}
        self._orig_server = (
            server_module.CONFIG,
            server_module.tracker_store,
            server_module.roadmap_store,
            server_module.portfolio_store,
            dict(server_module._import_report),
            dict(server_module._epic_report),
        )
        server_module.CONFIG = dataclasses.replace(
            server_module.CONFIG, trackers=(self.TRACKER,)
        )
        server_module.roadmap_store = RoadmapStore()
        server_module.portfolio_store = PortfolioStore(root / "portfolio.json")
        server_module._import_report.clear()
        server_module._epic_report.clear()

    def tearDown(self) -> None:
        (
            roadmap_module.ROADMAP_DIR,
            roadmap_module.PRODUCTS,
            roadmap_module.SYSTEMS,
            roadmap_module.PRODUCT_SYSTEMS,
        ) = self._orig_roadmap
        (
            server_module.CONFIG,
            server_module.tracker_store,
            server_module.roadmap_store,
            server_module.portfolio_store,
            import_report,
            epic_report,
        ) = self._orig_server
        server_module._import_report.clear()
        server_module._import_report.update(import_report)
        server_module._epic_report.clear()
        server_module._epic_report.update(epic_report)
        self._tmp.cleanup()

    def _run(self, tickets):
        server_module.tracker_store = _StubTrackerStore(tickets)
        server_module._sync_epic_projects()

    def _projects(self):
        return server_module.portfolio_store.list_projects()

    # ---- creation ----

    def test_a_new_open_epic_becomes_a_linked_project(self) -> None:
        self._run([_ticket("PROJ-1", "Epic", title="Checkout revamp")])
        projects = self._projects()
        self.assertEqual(len(projects), 1)
        self.assertEqual(projects[0]["title"], "Checkout revamp")
        self.assertEqual(
            (projects[0]["tracker_id"], projects[0]["ticket_key"]), ("jira", "PROJ-1")
        )
        self.assertEqual(projects[0]["initiative_id"], None)  # a human files it
        self.assertEqual(server_module._epic_report["jira"]["projects_created"], 1)

    def test_a_second_pass_imports_nothing(self) -> None:
        tickets = [_ticket("PROJ-1", "Epic", title="Checkout revamp")]
        self._run(tickets)
        self._run(tickets)
        self.assertEqual(len(self._projects()), 1)
        self.assertEqual(server_module._epic_report["jira"]["projects_created"], 0)

    def test_a_done_epic_is_history_not_a_new_project(self) -> None:
        self._run([_ticket("PROJ-1", "Epic", state="Done", category="Done")])
        self.assertEqual(self._projects(), [])
        self.assertEqual(server_module._epic_report["jira"]["skipped_done"], 1)

    def test_an_unrouted_epic_is_counted_not_imported(self) -> None:
        self._run([_ticket("OTHER-1", "Epic", project="OTHER")])
        self.assertEqual(self._projects(), [])
        self.assertEqual(server_module._epic_report["jira"]["unrouted_epics"], 1)

    def test_an_epic_held_by_a_change_is_reported_not_twinned(self) -> None:
        item = server_module.roadmap_store.create("checkout", "Imported as a change")
        server_module.roadmap_store.link_ticket(item.id, "jira", "PROJ-1")
        self._run([_ticket("PROJ-1", "Epic")])
        self.assertEqual(self._projects(), [])
        self.assertEqual(server_module._epic_report["jira"]["held_by_changes"], 1)

    # ---- the one-time cleanup: linking what already exists ----

    def test_an_existing_project_is_linked_by_its_changes_parent_epic(self) -> None:
        project = server_module.portfolio_store.create_project("Hand-made revamp")
        item = server_module.roadmap_store.create(
            "checkout", "A story", project_id=project.id
        )
        server_module.roadmap_store.link_ticket(item.id, "jira", "PROJ-2")
        self._run([
            _ticket("PROJ-1", "Epic", title="Some epic"),
            _ticket("PROJ-2", "Story", parent="PROJ-1", parent_type="Epic"),
        ])
        projects = self._projects()
        # Linked, not twinned: the epic was claimed by the evidence step, so the
        # creation step had nothing left to import.
        self.assertEqual(len(projects), 1)
        self.assertEqual(projects[0]["id"], project.id)
        self.assertEqual(projects[0]["ticket_key"], "PROJ-1")
        report = server_module._epic_report["jira"]
        self.assertEqual((report["projects_linked"], report["projects_created"]), (1, 0))

    def test_evidence_works_for_an_epic_outside_the_synced_catalog(self) -> None:
        """The parent TYPE travels on the child ticket, so a project can be linked to
        an epic the sync never pulled - it resolves via refresh_missing later."""
        project = server_module.portfolio_store.create_project("P")
        item = server_module.roadmap_store.create("checkout", "C", project_id=project.id)
        server_module.roadmap_store.link_ticket(item.id, "jira", "PROJ-2")
        self._run([_ticket("PROJ-2", "Story", parent="EXT-9", parent_type="Epic")])
        self.assertEqual(self._projects()[0]["ticket_key"], "EXT-9")

    def test_changes_spanning_two_epics_is_ambiguity_not_a_coin_toss(self) -> None:
        project = server_module.portfolio_store.create_project("Sprawling")
        for key, parent in (("PROJ-2", "EXT-1"), ("PROJ-3", "EXT-2")):
            item = server_module.roadmap_store.create(
                "checkout", key, project_id=project.id
            )
            server_module.roadmap_store.link_ticket(item.id, "jira", key)
        self._run([
            _ticket("PROJ-2", "Story", parent="EXT-1", parent_type="Epic"),
            _ticket("PROJ-3", "Story", parent="EXT-2", parent_type="Epic"),
        ])
        self.assertIsNone(self._projects()[0]["ticket_key"])
        self.assertEqual(server_module._epic_report["jira"]["ambiguous"], ["Sprawling"])

    def test_a_title_match_links_when_unique_on_both_sides(self) -> None:
        project = server_module.portfolio_store.create_project("Checkout Revamp")
        self._run([_ticket("PROJ-1", "Epic", title="checkout revamp")])
        projects = self._projects()
        self.assertEqual(len(projects), 1)
        self.assertEqual(projects[0]["id"], project.id)
        self.assertEqual(projects[0]["ticket_key"], "PROJ-1")

    def test_two_epics_sharing_a_title_neither_link_nor_import(self) -> None:
        """One of the contested epics very likely IS the unlinked project, so importing
        them anyway would twin it - they are withheld until a human links, and only
        what remains genuinely new imports on a later sync."""
        project = server_module.portfolio_store.create_project("Revamp")
        self._run([
            _ticket("PROJ-1", "Epic", title="Revamp"),
            _ticket("PROJ-2", "Epic", title="revamp"),
        ])
        projects = self._projects()
        self.assertEqual([p["id"] for p in projects], [project.id])
        self.assertIsNone(projects[0]["ticket_key"])
        report = server_module._epic_report["jira"]
        self.assertIn("Revamp", report["ambiguous"])
        self.assertEqual(report["contested_epics"], 2)

    def test_evidence_outranks_a_conflicting_title_match(self) -> None:
        """A renamed epic: the project's changes say PROJ-1, some other epic happens to
        wear the project's old title. The hierarchy wins."""
        project = server_module.portfolio_store.create_project("Old name")
        item = server_module.roadmap_store.create("checkout", "C", project_id=project.id)
        server_module.roadmap_store.link_ticket(item.id, "jira", "PROJ-2")
        self._run([
            _ticket("PROJ-1", "Epic", title="New name"),
            _ticket("PROJ-2", "Story", parent="PROJ-1", parent_type="Epic"),
            _ticket("PROJ-9", "Epic", title="Old name", state="Done", category="Done"),
        ])
        linked = server_module.portfolio_store.get_project(project.id)
        self.assertEqual(linked.ticket_key, "PROJ-1")

    # ---- assignment: changes land in their epic's project ----

    def test_an_unassigned_change_lands_in_its_epics_project(self) -> None:
        tickets = [
            _ticket("PROJ-1", "Epic", title="The epic"),
            _ticket("PROJ-2", "Story", parent="PROJ-1", parent_type="Epic"),
        ]
        item = server_module.roadmap_store.create("checkout", "A story")
        server_module.roadmap_store.link_ticket(item.id, "jira", "PROJ-2")
        self._run(tickets)
        server_module._assign_changes_to_epic_projects()
        project = server_module.portfolio_store.project_for_ticket("jira", "PROJ-1")
        self.assertEqual(server_module.roadmap_store.get(item.id).project_id, project.id)
        self.assertEqual(server_module._epic_report["jira"]["changes_assigned"], 1)

    def test_a_change_a_human_filed_elsewhere_is_left_alone(self) -> None:
        chosen = server_module.portfolio_store.create_project("Deliberate home")
        tickets = [
            _ticket("PROJ-1", "Epic", title="The epic"),
            _ticket("PROJ-2", "Story", parent="PROJ-1", parent_type="Epic"),
        ]
        item = server_module.roadmap_store.create(
            "checkout", "A story", project_id=chosen.id
        )
        server_module.roadmap_store.link_ticket(item.id, "jira", "PROJ-2")
        self._run(tickets)
        server_module._assign_changes_to_epic_projects()
        self.assertEqual(server_module.roadmap_store.get(item.id).project_id, chosen.id)

    # ---- the change import stays on its side of the line ----

    def test_the_change_import_never_consumes_a_project_held_epic(self) -> None:
        """import_types here (mis)include "Epic". The epic pass runs first, so the epic
        is project-held by the time the change import sees it - skipped, not twinned."""
        tickets = [_ticket("PROJ-1", "Epic", title="The epic")]
        self._run(tickets)
        server_module._import_routed_tickets()
        self.assertEqual(
            server_module.roadmap_store.item_for_ticket("jira", "PROJ-1"), None
        )
        self.assertEqual(server_module._import_report["jira"]["imported"], 0)
        self.assertEqual(len(self._projects()), 1)


if __name__ == "__main__":
    unittest.main()
