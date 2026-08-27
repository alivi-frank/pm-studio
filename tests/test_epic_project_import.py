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
    resolution="",
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
        resolution=resolution,
    )


class _StubTrackerStore:
    def __init__(self, tickets):
        self._tickets = list(tickets)

    def tickets_of(self, tracker_id):
        return [t for t in self._tickets if t.tracker_id == tracker_id]


class _EpicHarness(unittest.TestCase):
    """Stores swapped for throwaways, reports cleared - shared by the epic-pass tests
    and the won't-do removal tests, which exercise the same machinery."""

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


class EpicProjectImportTest(_EpicHarness):
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

    def test_a_wont_do_epic_is_declined_not_done(self) -> None:
        """Won't-do sits in Jira's Done category, but must be counted as its own
        outcome, not folded into skipped_done - "declined" and "history" are different
        answers to "where did my epic go"."""
        self._run([
            _ticket("PROJ-1", "Epic", state="Closed", category="Done",
                    resolution="Won't Do"),
            _ticket("PROJ-2", "Epic", state="Won't Do", category="Done"),
        ])
        self.assertEqual(self._projects(), [])
        report = server_module._epic_report["jira"]
        self.assertEqual(report["skipped_wont_do"], 2)
        self.assertEqual(report["skipped_done"], 0)

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

    # ---- reclaiming the pre-link import's epic-shaped changes ----

    def _import_artifact(self, key: str) -> str:
        """A change exactly as the pre-link import created it: stamp description,
        linked to the epic, filed nowhere, owned by nobody."""
        item = server_module.roadmap_store.create(
            "checkout",
            "The epic's own title",
            description=f"Imported from Jira {key} by project route 'PROJ'.",
        )
        server_module.roadmap_store.link_ticket(item.id, "jira", key)
        return item.id

    def test_a_pristine_epic_change_is_reclaimed_and_the_epic_imports(self) -> None:
        change_id = self._import_artifact("PROJ-1")
        self._run([_ticket("PROJ-1", "Epic", title="The epic")])
        self.assertIsNone(server_module.roadmap_store.get(change_id))
        projects = self._projects()
        self.assertEqual(len(projects), 1)
        self.assertEqual(projects[0]["ticket_key"], "PROJ-1")
        report = server_module._epic_report["jira"]
        self.assertEqual(report["reclaimed_from_changes"], 1)
        self.assertEqual(report["held_by_changes"], 0)

    def test_a_reclaimed_epic_links_to_its_evidence_project_not_a_twin(self) -> None:
        """The deployment-in-the-wild shape: the epic was imported as a change AND a
        human made the project by hand, filing the epic's stories under it. One pass
        must delete the artifact and link the hand-made project, creating nothing."""
        self._import_artifact("PROJ-1")
        project = server_module.portfolio_store.create_project("Hand-made")
        item = server_module.roadmap_store.create(
            "checkout", "A story", project_id=project.id
        )
        server_module.roadmap_store.link_ticket(item.id, "jira", "PROJ-2")
        self._run([
            _ticket("PROJ-1", "Epic", title="The epic"),
            _ticket("PROJ-2", "Story", parent="PROJ-1", parent_type="Epic"),
        ])
        projects = self._projects()
        self.assertEqual([p["id"] for p in projects], [project.id])
        self.assertEqual(projects[0]["ticket_key"], "PROJ-1")
        report = server_module._epic_report["jira"]
        self.assertEqual(
            (report["reclaimed_from_changes"], report["projects_linked"], report["projects_created"]),
            (1, 1, 0),
        )

    def test_a_touched_epic_change_is_somebodys_work_and_stays(self) -> None:
        parent = server_module.portfolio_store.create_project("Filed here on purpose")
        item = server_module.roadmap_store.create(
            "checkout",
            "The epic, adopted as a change",
            description="Imported from Jira PROJ-1 by project route 'PROJ'.",
            project_id=parent.id,
        )
        server_module.roadmap_store.link_ticket(item.id, "jira", "PROJ-1")
        self._run([_ticket("PROJ-1", "Epic")])
        self.assertIsNotNone(server_module.roadmap_store.get(item.id))
        report = server_module._epic_report["jira"]
        self.assertEqual(report["reclaimed_from_changes"], 0)
        self.assertEqual(report["held_by_changes"], 1)

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

    def test_title_matching_sees_through_punctuation(self) -> None:
        """The shape found in the wild: the tracker says 'Vendor "Pay To" Changes',
        the hand-made project says 'Vendor Pay To Changes'. Same name."""
        project = server_module.portfolio_store.create_project("Vendor Pay To Changes")
        self._run([_ticket("PROJ-1", "Epic", title='Vendor "Pay To" Changes')])
        projects = self._projects()
        self.assertEqual([p["id"] for p in projects], [project.id])
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

    # ---- merging the twins exact-only title matching created ----

    def _twin(self, key: str, title: str) -> str:
        """An import-created linked project, exactly as the pass used to make them."""
        twin = server_module.portfolio_store.create_project(
            title, description=f"Imported from Jira {key}."
        )
        server_module.portfolio_store.link_epic(twin.id, "jira", key)
        return twin.id

    def test_an_import_twin_is_merged_into_the_hand_made_project(self) -> None:
        initiative = server_module.portfolio_store.create_initiative("The bet")
        survivor = server_module.portfolio_store.create_project(
            "Vendor Pay To Changes", initiative_id=initiative.id
        )
        twin_id = self._twin("PROJ-1", 'Vendor "Pay To" Changes')
        change = server_module.roadmap_store.create(
            "checkout", "Auto-filed story", project_id=twin_id
        )
        self._run([_ticket("PROJ-1", "Epic", title='Vendor "Pay To" Changes')])
        self.assertIsNone(server_module.portfolio_store.get_project(twin_id))
        kept = server_module.portfolio_store.get_project(survivor.id)
        self.assertEqual(kept.ticket_key, "PROJ-1")
        self.assertEqual(kept.initiative_id, initiative.id)  # human context survives
        self.assertEqual(
            server_module.roadmap_store.get(change.id).project_id, survivor.id
        )
        self.assertEqual(server_module._epic_report["jira"]["twins_merged"], 1)

    def test_a_twin_someone_filed_under_an_initiative_is_not_plumbing(self) -> None:
        initiative = server_module.portfolio_store.create_initiative("The bet")
        server_module.portfolio_store.create_project("Vendor Pay To Changes")
        twin_id = self._twin("PROJ-1", 'Vendor "Pay To" Changes')
        server_module.portfolio_store.update_project(twin_id, initiative_id=initiative.id)
        self._run([_ticket("PROJ-1", "Epic", title='Vendor "Pay To" Changes')])
        self.assertIsNotNone(server_module.portfolio_store.get_project(twin_id))
        self.assertEqual(server_module._epic_report["jira"]["twins_merged"], 0)

    def test_a_twin_with_two_lookalikes_merges_into_neither(self) -> None:
        server_module.portfolio_store.create_project("Vendor Pay To Changes")
        server_module.portfolio_store.create_project("Vendor PAY-TO Changes")
        twin_id = self._twin("PROJ-1", 'Vendor "Pay To" Changes')
        self._run([_ticket("PROJ-1", "Epic", title='Vendor "Pay To" Changes')])
        self.assertIsNotNone(server_module.portfolio_store.get_project(twin_id))
        self.assertEqual(server_module._epic_report["jira"]["twins_merged"], 0)

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

    def test_assignment_climbs_through_the_feature_rung(self) -> None:
        """ADO nests User Story under Feature under Epic - one hop up from a story is
        a Feature, and stopping there would leave every ADO story unassigned. The walk
        must reach the epic at the top of the chain."""
        tickets = [
            _ticket("PROJ-1", "Epic", title="The epic"),
            _ticket("PROJ-5", "Feature", parent="PROJ-1", parent_type="Epic"),
            _ticket("PROJ-6", "Story", parent="PROJ-5", parent_type="Feature"),
        ]
        item = server_module.roadmap_store.create("checkout", "A story")
        server_module.roadmap_store.link_ticket(item.id, "jira", "PROJ-6")
        self._run(tickets)
        server_module._assign_changes_to_epic_projects()
        project = server_module.portfolio_store.project_for_ticket("jira", "PROJ-1")
        self.assertIsNotNone(project)
        self.assertEqual(server_module.roadmap_store.get(item.id).project_id, project.id)

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
        # Not even a candidate for correction: the evidence step LINKED "Deliberate home"
        # to PROJ-1 on the way through, so the hierarchy already agrees with the human.
        self.assertEqual(
            server_module.portfolio_store.project_for_ticket("jira", "PROJ-1").id,
            chosen.id,
        )

    # ---- assignment: following a re-parenting upstream ----

    def test_a_hand_made_project_is_not_drained_by_the_hierarchy(self) -> None:
        """The casualty case the fill-only rule was really protecting, and the reason
        the correction is scoped to slots the tracker owns: the epic already has an
        import-created project, so a hand-made project holding one of its stories cannot
        be linked to it - and must not be emptied into it either. Reported instead."""
        epic = _ticket("PROJ-1", "Epic", title="The epic")
        self._run([epic])  # the epic gets its own project first
        imported = server_module.portfolio_store.project_for_ticket("jira", "PROJ-1")
        chosen = server_module.portfolio_store.create_project("Deliberate home")
        item = server_module.roadmap_store.create(
            "checkout", "A story", project_id=chosen.id
        )
        server_module.roadmap_store.link_ticket(item.id, "jira", "PROJ-2")
        self._run([epic, _ticket("PROJ-2", "Story", parent="PROJ-1", parent_type="Epic")])
        server_module._assign_changes_to_epic_projects()
        self.assertIsNone(chosen.tracker_id)  # still nobody's mirror
        self.assertEqual(server_module.roadmap_store.get(item.id).project_id, chosen.id)
        self.assertEqual(server_module._epic_report["jira"]["reparent_kept"], 1)
        self.assertEqual(
            server_module.roadmap_store.count_by_project(imported.id), 0
        )

    def test_a_reparented_story_follows_its_ticket_to_the_new_epic(self) -> None:
        """The point of the whole pass: re-parenting a story in the tracker replicates
        onto the board. Both projects here are the tracker's own, so the slot the change
        sits in is the tracker's to correct."""
        epics = [
            _ticket("PROJ-1", "Epic", title="First epic"),
            _ticket("PROJ-2", "Epic", title="Second epic"),
        ]
        item = server_module.roadmap_store.create("checkout", "A story")
        server_module.roadmap_store.link_ticket(item.id, "jira", "PROJ-3")
        self._run(epics + [_ticket("PROJ-3", "Story", parent="PROJ-1", parent_type="Epic")])
        server_module._assign_changes_to_epic_projects()
        first = server_module.portfolio_store.project_for_ticket("jira", "PROJ-1")
        self.assertEqual(server_module.roadmap_store.get(item.id).project_id, first.id)

        # The one edit, made in the tracker: PROJ-3 now hangs off the second epic.
        self._run(epics + [_ticket("PROJ-3", "Story", parent="PROJ-2", parent_type="Epic")])
        server_module._assign_changes_to_epic_projects()
        second = server_module.portfolio_store.project_for_ticket("jira", "PROJ-2")
        self.assertEqual(server_module.roadmap_store.get(item.id).project_id, second.id)
        self.assertEqual(server_module._epic_report["jira"]["changes_reparented"], 1)

    def test_a_settled_assignment_is_not_rewritten_every_sync(self) -> None:
        """Idempotency: the correction fires on the change, not on every pass, or the
        report would cry wolf and every sync would touch every change."""
        tickets = [
            _ticket("PROJ-1", "Epic", title="The epic"),
            _ticket("PROJ-2", "Story", parent="PROJ-1", parent_type="Epic"),
        ]
        item = server_module.roadmap_store.create("checkout", "A story")
        server_module.roadmap_store.link_ticket(item.id, "jira", "PROJ-2")
        self._run(tickets)
        server_module._assign_changes_to_epic_projects()
        self._run(tickets)
        server_module._assign_changes_to_epic_projects()
        report = server_module._epic_report["jira"]
        self.assertEqual(report["changes_assigned"], 0)
        self.assertEqual(report["changes_reparented"], 0)
        self.assertEqual(report["reparent_kept"], 0)

    def test_a_story_that_lost_its_parent_keeps_its_project(self) -> None:
        """Never CLEARS: a ticket with no visible parent is unknown, not "no epic". The
        story stays where it was filed rather than falling off its project."""
        item = server_module.roadmap_store.create("checkout", "A story")
        server_module.roadmap_store.link_ticket(item.id, "jira", "PROJ-2")
        epic = _ticket("PROJ-1", "Epic", title="The epic")
        self._run([epic, _ticket("PROJ-2", "Story", parent="PROJ-1", parent_type="Epic")])
        server_module._assign_changes_to_epic_projects()
        project = server_module.portfolio_store.project_for_ticket("jira", "PROJ-1")
        self._run([epic, _ticket("PROJ-2", "Story")])
        server_module._assign_changes_to_epic_projects()
        self.assertEqual(server_module.roadmap_store.get(item.id).project_id, project.id)

    def test_a_story_moved_under_an_epic_with_no_project_keeps_its_project(self) -> None:
        """The new epic is outside the synced catalog, so nothing backs it locally yet.
        Unfiling the story until that resolves would be worse than leaving it put."""
        item = server_module.roadmap_store.create("checkout", "A story")
        server_module.roadmap_store.link_ticket(item.id, "jira", "PROJ-2")
        epic = _ticket("PROJ-1", "Epic", title="The epic")
        self._run([epic, _ticket("PROJ-2", "Story", parent="PROJ-1", parent_type="Epic")])
        server_module._assign_changes_to_epic_projects()
        project = server_module.portfolio_store.project_for_ticket("jira", "PROJ-1")
        self._run([epic, _ticket("PROJ-2", "Story", parent="OTHER-9", parent_type="Epic")])
        server_module._assign_changes_to_epic_projects()
        self.assertEqual(server_module.roadmap_store.get(item.id).project_id, project.id)

    def test_a_change_whose_project_was_deleted_is_refilled(self) -> None:
        """A dangling project id is rot, not somebody's filing - the hierarchy fills it
        the same as an empty one."""
        gone = server_module.portfolio_store.create_project("Deleted later")
        item = server_module.roadmap_store.create(
            "checkout", "A story", project_id=gone.id
        )
        server_module.roadmap_store.link_ticket(item.id, "jira", "PROJ-2")
        server_module.portfolio_store.delete_project(gone.id)
        self._run([
            _ticket("PROJ-1", "Epic", title="The epic"),
            _ticket("PROJ-2", "Story", parent="PROJ-1", parent_type="Epic"),
        ])
        server_module._assign_changes_to_epic_projects()
        project = server_module.portfolio_store.project_for_ticket("jira", "PROJ-1")
        self.assertEqual(server_module.roadmap_store.get(item.id).project_id, project.id)
        self.assertEqual(server_module._epic_report["jira"]["changes_assigned"], 1)

    # ---- exclusion: the slice of the tracker that lives elsewhere ----

    def _exclude(self, *components: str) -> None:
        tracker = dataclasses.replace(self.TRACKER, exclude_components=components)
        server_module.CONFIG = dataclasses.replace(
            server_module.CONFIG, trackers=(tracker,)
        )

    def test_an_excluded_epic_is_invisible_to_the_epic_pass(self) -> None:
        """Neither created nor title-linked, however well the names match - the
        matching project stays honestly unlinked, waiting for the tracker that
        actually owns this work."""
        self._exclude("CapAdmin")
        server_module.portfolio_store.create_project("Claims revamp")
        epic = _ticket("PROJ-1", "Epic", title="Claims revamp")
        epic.components = ["CapAdmin"]
        self._run([epic])
        projects = self._projects()
        self.assertEqual(len(projects), 1)
        self.assertIsNone(projects[0]["ticket_key"])
        report = server_module._epic_report["jira"]
        self.assertEqual(report["excluded_epics"], 1)
        self.assertEqual((report["projects_created"], report["projects_linked"]), (0, 0))

    def test_a_story_moved_under_an_excluded_epic_keeps_its_project(self) -> None:
        """Excluded means "this slice lives elsewhere", not "unfile it here": the epic
        is invisible to these passes, so the correction has nothing to say and the
        change stays where it was."""
        first = _ticket("PROJ-1", "Epic", title="The epic")
        elsewhere = _ticket("PROJ-9", "Epic", title="Somebody else's epic")
        elsewhere.components = ["CapAdmin"]
        item = server_module.roadmap_store.create("checkout", "A story")
        server_module.roadmap_store.link_ticket(item.id, "jira", "PROJ-2")
        self._run([first, _ticket("PROJ-2", "Story", parent="PROJ-1", parent_type="Epic")])
        server_module._assign_changes_to_epic_projects()
        project = server_module.portfolio_store.project_for_ticket("jira", "PROJ-1")
        self._exclude("CapAdmin")
        self._run([
            first, elsewhere,
            _ticket("PROJ-2", "Story", parent="PROJ-9", parent_type="Epic"),
        ])
        server_module._assign_changes_to_epic_projects()
        self.assertEqual(server_module.roadmap_store.get(item.id).project_id, project.id)

    def test_exclusion_covers_the_whole_parent_chain(self) -> None:
        """Only the epic carries the component; the story under it and the task under
        the story carry none - all three stay out of the change import, and none of
        them count as unrouted (excluded means "never", not "route this someday")."""
        tracker = dataclasses.replace(
            self.TRACKER,
            exclude_components=("CapAdmin",),
            import_types=("Epic", "Story", "Task"),
        )
        server_module.CONFIG = dataclasses.replace(
            server_module.CONFIG, trackers=(tracker,)
        )
        epic = _ticket("PROJ-1", "Epic", title="The epic")
        epic.components = ["CapAdmin"]
        story = _ticket("PROJ-2", "Story", parent="PROJ-1", parent_type="Epic")
        task = _ticket("PROJ-3", "Task", parent="PROJ-2", parent_type="Story")
        server_module.tracker_store = _StubTrackerStore([epic, story, task])
        server_module._sync_epic_projects()
        server_module._import_routed_tickets()
        self.assertEqual(self._projects(), [])
        for key in ("PROJ-1", "PROJ-2", "PROJ-3"):
            self.assertIsNone(server_module.roadmap_store.item_for_ticket("jira", key))
        report = server_module._import_report["jira"]
        self.assertEqual(report["imported"], 0)
        self.assertEqual(report["excluded"], 3)
        self.assertEqual(report["unrouted_total"], 0)

    def test_evidence_pointing_at_an_excluded_epic_does_not_link(self) -> None:
        self._exclude("CapAdmin")
        project = server_module.portfolio_store.create_project("P")
        item = server_module.roadmap_store.create("checkout", "C", project_id=project.id)
        server_module.roadmap_store.link_ticket(item.id, "jira", "PROJ-2")
        epic = _ticket("PROJ-1", "Epic", title="The epic")
        epic.components = ["CapAdmin"]
        self._run([
            epic,
            _ticket("PROJ-2", "Story", parent="PROJ-1", parent_type="Epic"),
        ])
        self.assertIsNone(self._projects()[0]["ticket_key"])

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

    def test_a_done_epic_does_not_ping_pong_between_the_two_passes(self) -> None:
        """The failure seen live: a DONE epic is refused as a project (history), so with
        "Epic" in import_types the change import would re-import it as a change on every
        sync, for the next sync's reclaim to delete again - 42 phantom changes a cycle.
        Epic-level tickets are never changes, so two full cycles must leave nothing."""
        tickets = [_ticket("PROJ-1", "Epic", title="Old epic", state="Done", category="Done")]
        for _ in range(2):
            self._run(tickets)
            server_module._import_routed_tickets()
        self.assertIsNone(server_module.roadmap_store.item_for_ticket("jira", "PROJ-1"))
        self.assertEqual(self._projects(), [])
        self.assertEqual(server_module._import_report["jira"]["imported"], 0)
        self.assertEqual(server_module._epic_report["jira"]["reclaimed_from_changes"], 0)


class WontDoRemovalTest(_EpicHarness):
    """_remove_wont_do_imports: a ticket declined AFTER import leaves the board the way
    it would never have arrived - but only if nobody touched what the import created.
    Runs the full pass order, since the removal's contract is about what earlier
    cycles created."""

    def _cycle(self, tickets):
        """One sync cycle exactly as _run_tracker_sync orders the passes."""
        server_module.tracker_store = _StubTrackerStore(tickets)
        server_module._sync_epic_projects()
        server_module._import_routed_tickets()
        server_module._assign_changes_to_epic_projects()
        server_module._remove_wont_do_imports()

    def _changes(self):
        return server_module.roadmap_store.list_product("checkout")

    def test_a_change_declined_after_import_is_removed(self) -> None:
        story = _ticket("PROJ-2", "Story")
        self._cycle([story])
        self.assertEqual(len(self._changes()), 1)
        declined = _ticket("PROJ-2", "Story", state="Closed", category="Done",
                           resolution="Won't Do")
        self._cycle([declined])
        self.assertEqual(self._changes(), [])
        report = server_module._import_report["jira"]
        self.assertEqual((report["wont_do_removed"], report["wont_do_kept"]), (1, 0))

    def test_a_touched_change_is_kept_and_counted(self) -> None:
        """A human moved it out of "later": somebody's work, reported instead."""
        self._cycle([_ticket("PROJ-2", "Story")])
        change = self._changes()[0]
        server_module.roadmap_store.update(change["id"], bucket="now")
        self._cycle([_ticket("PROJ-2", "Story", state="Closed", category="Done",
                             resolution="Won't Do")])
        self.assertEqual(len(self._changes()), 1)
        report = server_module._import_report["jira"]
        self.assertEqual((report["wont_do_removed"], report["wont_do_kept"]), (0, 1))

    def test_a_ticket_missing_from_the_catalog_is_unknown_not_declined(self) -> None:
        """Aged out of the sync window is not won't-do - nothing may be removed on
        absence of evidence."""
        self._cycle([_ticket("PROJ-2", "Story")])
        self._cycle([])
        self.assertEqual(len(self._changes()), 1)

    def test_a_declined_epic_and_its_changes_leave_in_one_cycle(self) -> None:
        """Changes are removed before projects are judged empty, so the whole declined
        subtree goes at once instead of the project waiting a cycle for its changes."""
        epic = _ticket("PROJ-1", "Epic", title="Doomed epic")
        story = _ticket("PROJ-2", "Story", parent="PROJ-1", parent_type="Epic")
        self._cycle([epic, story])
        self.assertEqual(len(self._projects()), 1)
        self.assertEqual(len(self._changes()), 1)
        self._cycle([
            _ticket("PROJ-1", "Epic", title="Doomed epic", state="Closed",
                    category="Done", resolution="Won't Do"),
            _ticket("PROJ-2", "Story", parent="PROJ-1", parent_type="Epic",
                    state="Closed", category="Done", resolution="Won't Do"),
        ])
        self.assertEqual(self._changes(), [])
        self.assertEqual(self._projects(), [])
        report = server_module._epic_report["jira"]
        self.assertEqual(report["wont_do_projects_removed"], 1)

    def test_a_project_filed_under_an_initiative_is_kept(self) -> None:
        self._cycle([_ticket("PROJ-1", "Epic", title="Filed epic")])
        project = self._projects()[0]
        initiative = server_module.portfolio_store.create_initiative("Strategy")
        server_module.portfolio_store.update_project(
            project["id"], initiative_id=initiative.id
        )
        self._cycle([_ticket("PROJ-1", "Epic", title="Filed epic", state="Closed",
                             category="Done", resolution="Won't Do")])
        self.assertEqual(len(self._projects()), 1)
        report = server_module._epic_report["jira"]
        self.assertEqual(
            (report["wont_do_projects_removed"], report["wont_do_projects_kept"]), (0, 1)
        )


class StaleExclusionReportTest(_EpicHarness):
    """_report_stale_excluded_imports: a component added in the tracker AFTER something
    was imported does not remove it - the mismatch is named on the import report and the
    deletion stays a human call. This is the case seen live on NDT: seven CapAdmin epics
    were already projects, filed under an initiative, when the component was set."""

    def _cycle(self, tickets):
        """One sync cycle exactly as _run_tracker_sync orders the passes."""
        server_module.tracker_store = _StubTrackerStore(tickets)
        server_module._sync_epic_projects()
        server_module._import_routed_tickets()
        server_module._assign_changes_to_epic_projects()
        server_module._remove_wont_do_imports()
        server_module._report_stale_excluded_imports()

    def _exclude(self, *components: str) -> None:
        tracker = dataclasses.replace(self.TRACKER, exclude_components=components)
        server_module.CONFIG = dataclasses.replace(
            server_module.CONFIG, trackers=(tracker,)
        )

    def _stale(self):
        return server_module._import_report["jira"]["stale_excluded"]

    def test_a_project_excluded_after_import_is_reported_not_removed(self) -> None:
        self._cycle([_ticket("PROJ-1", "Epic", title="Claims revamp")])
        self.assertEqual(len(self._projects()), 1)
        epic = _ticket("PROJ-1", "Epic", title="Claims revamp")
        epic.components = ["CapAdmin"]
        self._exclude("CapAdmin")
        self._cycle([epic])
        self.assertEqual(len(self._projects()), 1, "the project must survive")
        stale = self._stale()
        self.assertEqual(stale["total"], 1)
        self.assertEqual(stale["changes"], [])
        self.assertEqual(
            [(p["ticket_key"], p["title"]) for p in stale["projects"]],
            [("PROJ-1", "Claims revamp")],
        )

    def test_a_change_excluded_after_import_is_reported_not_removed(self) -> None:
        self._cycle([_ticket("PROJ-2", "Story", title="A story")])
        item = server_module.roadmap_store.item_for_ticket("jira", "PROJ-2")
        self.assertIsNotNone(item)
        story = _ticket("PROJ-2", "Story", title="A story")
        story.components = ["CapAdmin"]
        self._exclude("CapAdmin")
        self._cycle([story])
        self.assertIsNotNone(server_module.roadmap_store.get(item.id))
        stale = self._stale()
        self.assertEqual(stale["total"], 1)
        self.assertEqual(stale["projects"], [])
        self.assertEqual(stale["changes"][0]["ticket_key"], "PROJ-2")

    def test_a_project_filed_under_an_initiative_is_still_only_reported(self) -> None:
        """The live shape, and the reason this pass reports rather than removes: a human
        filed it under an initiative. Nothing about an added component may undo that."""
        self._cycle([_ticket("PROJ-1", "Epic", title="Filed epic")])
        project = self._projects()[0]
        initiative = server_module.portfolio_store.create_initiative("Claims & RCM")
        server_module.portfolio_store.update_project(
            project["id"], initiative_id=initiative.id
        )
        epic = _ticket("PROJ-1", "Epic", title="Filed epic")
        epic.components = ["CapAdmin"]
        self._exclude("CapAdmin")
        self._cycle([epic])
        kept = self._projects()
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["initiative_id"], initiative.id)
        self.assertEqual(self._stale()["total"], 1)

    def test_exclusion_by_parent_chain_is_reported_too(self) -> None:
        """The story carries no component of its own - it is stale because the epic
        above it is, the same fixpoint walk the import gate uses."""
        self._cycle([
            _ticket("PROJ-1", "Epic", title="The epic"),
            _ticket("PROJ-2", "Story", parent="PROJ-1", parent_type="Epic"),
        ])
        epic = _ticket("PROJ-1", "Epic", title="The epic")
        epic.components = ["CapAdmin"]
        self._exclude("CapAdmin")
        self._cycle([
            epic,
            _ticket("PROJ-2", "Story", parent="PROJ-1", parent_type="Epic"),
        ])
        stale = self._stale()
        self.assertEqual(stale["total"], 2)
        self.assertEqual([c["ticket_key"] for c in stale["changes"]], ["PROJ-2"])
        self.assertEqual([p["ticket_key"] for p in stale["projects"]], ["PROJ-1"])

    def test_nothing_excluded_reports_an_empty_shape(self) -> None:
        """The key is always present, so a consumer never has to guess whether the pass
        ran or simply found nothing."""
        self._cycle([_ticket("PROJ-1", "Epic", title="The epic")])
        self.assertEqual(self._stale(), {"changes": [], "projects": [], "total": 0})

    def test_a_ticket_missing_from_the_catalog_is_not_reported(self) -> None:
        """Aged out of the sync window is unknown, not excluded - same posture as the
        won't-do pass takes on absence."""
        self._cycle([_ticket("PROJ-1", "Epic", title="The epic")])
        self._exclude("CapAdmin")
        self._cycle([])
        self.assertEqual(self._stale()["total"], 0)


if __name__ == "__main__":
    unittest.main()
