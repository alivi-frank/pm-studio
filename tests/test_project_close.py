"""Tests for the passes that decide a project is over: `_mirror_epic_status`,
`_outstanding_changes` and `_close_candidates`.

The contracts worth pinning, in order of how much damage getting them wrong does:

- CLOSE-ONLY. A project's `closed` is a human act carrying a date. The tracker may
  close one; nothing here ever reopens one, however the epic moves. That asymmetry is
  the whole reason this is not simply `_mirror_ticket_status` for projects, and it is
  the first thing a refactor would "tidy" away.
- REPORT BEFORE WRITE. `close_done_epics` is off by default, and off means the pass
  counts and writes nothing. A deployment that upgrades into this must not wake up to
  forty projects closed overnight.
- DECLINED IS SETTLED, NOT OUTSTANDING. A change on a won't-do ticket is neither
  shipped nor to-do. Counting it as open work pinned its project on the board forever.
- ALL-LOCAL-DONE IS NOT ALL-DONE. An epic only partly imported here reads as finished
  from the local roll-up. The candidate report has to check the epic's real children,
  or the board offers a close on work that is still running.
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
    raw_type="Epic",
    title="T",
    state="To Do",
    category="To Do",
    parent=None,
    parent_type=None,
    resolution="",
    components=None,
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
        components=list(components or []),
        project="PROJ",
        resolution=resolution,
    )


def _done(key, **kw):
    return _ticket(key, state="Done", category="Done", **kw)


class _StubTrackerStore:
    """Enough of TrackerStore for these passes: the per-tracker catalog they walk and
    the single-ticket lookup `_outstanding_changes` resolves each change through."""

    def __init__(self, tickets=()):
        self._tickets = list(tickets)

    def tickets_of(self, tracker_id):
        return [t for t in self._tickets if t.tracker_id == tracker_id]

    def lookup(self, tracker_id, key):
        if not tracker_id or not key:
            return None
        for t in self._tickets:
            if t.tracker_id == tracker_id and t.key.casefold() == key.casefold():
                return t
        return None


class _CloseHarness(unittest.TestCase):
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
            dict(server_module._epic_report),
        )
        server_module.CONFIG = dataclasses.replace(
            server_module.CONFIG, trackers=(self.TRACKER,)
        )
        server_module.roadmap_store = RoadmapStore()
        server_module.portfolio_store = PortfolioStore(root / "portfolio.json")
        server_module.tracker_store = _StubTrackerStore()
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
            epic_report,
        ) = self._orig_server
        server_module._epic_report.clear()
        server_module._epic_report.update(epic_report)
        self._tmp.cleanup()

    # ---- fixtures ----

    def _project(self, title="P", epic=None, status="open", **kw):
        project = server_module.portfolio_store.create_project(
            title=title, status=status, **kw
        )
        if epic:
            server_module.portfolio_store.link_epic(project.id, "jira", epic)
        return project

    def _change(self, project, title="C", status="pending", ticket=None, bucket="later"):
        item = server_module.roadmap_store.create(
            "checkout", title, status=status, project_id=project.id, bucket=bucket
        )
        if ticket:
            server_module.roadmap_store.link_ticket(item.id, "jira", ticket)
        return item

    def _run(self, tickets, close=True, exclude=()):
        server_module.tracker_store = _StubTrackerStore(tickets)
        server_module.CONFIG = dataclasses.replace(
            server_module.CONFIG,
            trackers=(dataclasses.replace(
                self.TRACKER, close_done_epics=close, exclude_components=tuple(exclude)
            ),),
        )
        server_module._mirror_epic_status()

    def _status(self, project):
        return server_module.portfolio_store.get_project(project.id).status

    def _report(self):
        return server_module._epic_report.get("jira", {})


class MirrorEpicStatusTest(_CloseHarness):
    # ---- the write ----

    def test_a_done_epic_closes_its_project(self) -> None:
        project = self._project(epic="PROJ-1")
        self._run([_done("PROJ-1")])
        self.assertEqual(self._status(project), "closed")
        self.assertEqual(self._report()["projects_closed"], 1)

    def test_a_closed_project_carries_the_date_it_closed(self) -> None:
        project = self._project(epic="PROJ-1")
        self._run([_done("PROJ-1")])
        self.assertIsNotNone(
            server_module.portfolio_store.get_project(project.id).closed_at
        )

    def test_an_ideation_project_closes_too(self) -> None:
        # An idea whose epic shipped is not still an idea. Nothing about `ideation`
        # protects it - it is a claim about where the work is, and the tracker just
        # said where.
        project = self._project(epic="PROJ-1", status="ideation")
        self._run([_done("PROJ-1")])
        self.assertEqual(self._status(project), "closed")

    def test_the_pass_is_idempotent(self) -> None:
        project = self._project(epic="PROJ-1")
        self._run([_done("PROJ-1")])
        self._run([_done("PROJ-1")])
        self.assertEqual(self._status(project), "closed")
        self.assertEqual(self._report()["projects_closed"], 0)  # nothing left to do

    # ---- report before write ----

    def test_the_default_is_report_only(self) -> None:
        project = self._project(epic="PROJ-1")
        self._run([_done("PROJ-1")], close=False)
        self.assertEqual(self._status(project), "open")
        self.assertEqual(self._report()["projects_closable"], 1)
        self.assertEqual(self._report()["projects_closed"], 0)

    def test_close_done_epics_defaults_off_on_the_config(self) -> None:
        # The flag's default is the safety property, so it is asserted rather than
        # assumed by the test above.
        self.assertFalse(self.TRACKER.close_done_epics)

    # ---- close-only ----

    def test_an_epic_out_of_done_never_reopens_a_closed_project(self) -> None:
        project = self._project(epic="PROJ-1", status="closed")
        self._run([_ticket("PROJ-1", state="In Progress", category="In Progress")])
        self.assertEqual(self._status(project), "closed")
        self.assertEqual(self._report()["epic_open_on_closed"], 1)

    def test_an_open_project_under_an_open_epic_is_untouched(self) -> None:
        project = self._project(epic="PROJ-1")
        self._run([_ticket("PROJ-1")])
        self.assertEqual(self._status(project), "open")
        self.assertEqual(self._report()["epic_open_on_closed"], 0)

    # ---- what it refuses to read ----

    def test_an_epic_absent_from_the_catalog_closes_nothing(self) -> None:
        # Absent is unknown, not done - it may have aged out of `since`. Mirroring on
        # absence would close the board the first time a sync came back short.
        project = self._project(epic="PROJ-1")
        self._run([])
        self.assertEqual(self._status(project), "open")

    def test_a_wont_do_epic_closes_nothing(self) -> None:
        # Declined is not delivered, and its Done category cannot tell the two apart.
        project = self._project(epic="PROJ-1")
        self._run([_done("PROJ-1", resolution="Won't Do")])
        self.assertEqual(self._status(project), "open")

    def test_an_excluded_epic_closes_nothing(self) -> None:
        project = self._project(epic="PROJ-1")
        self._run([_done("PROJ-1", components=["Elsewhere"])], exclude=("Elsewhere",))
        self.assertEqual(self._status(project), "open")

    def test_a_project_with_no_epic_closes_nothing(self) -> None:
        project = self._project()
        self._run([_done("PROJ-1")])
        self.assertEqual(self._status(project), "open")

    def test_the_catch_all_is_never_closed(self) -> None:
        # update_project refuses it outright; skipping here is what keeps that refusal
        # from being raised as an error on every sync.
        project = server_module.portfolio_store.create_project(
            title="Unplanned work", is_catch_all=True
        )
        self._run([_done("PROJ-1")])
        self.assertEqual(self._status(project), "open")

    # ---- stranding is reported, not hidden ----

    def test_closing_over_open_changes_is_counted(self) -> None:
        project = self._project(epic="PROJ-1")
        self._change(project, "shipped", status="done")
        self._change(project, "still open", ticket="PROJ-2")
        self._run([_done("PROJ-1"), _ticket("PROJ-2", "Story", parent="PROJ-1")])
        self.assertEqual(self._status(project), "closed")
        self.assertEqual(self._report()["closed_with_open_changes"], 1)

    def test_closing_over_declined_changes_is_not_stranding(self) -> None:
        # The whole point of discounting declined work: this project has nothing left.
        project = self._project(epic="PROJ-1")
        self._change(project, "declined", ticket="PROJ-2")
        self._run([
            _done("PROJ-1"),
            _done("PROJ-2", raw_type="Story", parent="PROJ-1", resolution="Won't Do"),
        ])
        self.assertEqual(self._report()["closed_with_open_changes"], 0)


class OutstandingChangesTest(_CloseHarness):
    def test_a_done_change_is_not_outstanding(self) -> None:
        project = self._project()
        self._change(project, status="done")
        self.assertEqual(server_module._outstanding_changes(project.id), [])

    def test_a_pending_change_is_outstanding(self) -> None:
        project = self._project()
        self._change(project)
        self.assertEqual(len(server_module._outstanding_changes(project.id)), 1)

    def test_a_declined_change_is_settled(self) -> None:
        project = self._project()
        self._change(project, ticket="PROJ-9", bucket="next")
        server_module.tracker_store = _StubTrackerStore(
            [_done("PROJ-9", raw_type="Story", resolution="Won't Do")]
        )
        # `pending` locally and it stays that way - the status mirror refuses to call
        # declined work done. Settled all the same, which is what lets its project
        # become history.
        self.assertEqual(server_module._outstanding_changes(project.id), [])

    def test_a_change_whose_ticket_is_absent_stays_outstanding(self) -> None:
        project = self._project()
        self._change(project, ticket="PROJ-9")
        self.assertEqual(len(server_module._outstanding_changes(project.id)), 1)


class CloseCandidateTest(_CloseHarness):
    def _candidates(self, tickets=()):
        server_module.tracker_store = _StubTrackerStore(tickets)
        return server_module._close_candidates()

    def test_all_shipped_under_a_done_epic_reads_as_tracker(self) -> None:
        project = self._project(epic="PROJ-1")
        self._change(project, status="done", ticket="PROJ-2")
        got = self._candidates([
            _done("PROJ-1"), _done("PROJ-2", raw_type="Story", parent="PROJ-1"),
        ])
        self.assertEqual(got[project.id]["reason"], "tracker")

    def test_all_shipped_under_an_open_epic_reads_as_changes(self) -> None:
        # The 15-project case on the live board: mostly ADO epics parked in Active.
        # Offered, never closed automatically.
        project = self._project(epic="PROJ-1")
        self._change(project, status="done", ticket="PROJ-2")
        got = self._candidates([
            _ticket("PROJ-1", state="Active", category="Active"),
            _done("PROJ-2", raw_type="Story", parent="PROJ-1"),
        ])
        self.assertEqual(got[project.id]["reason"], "changes")
        self.assertEqual(got[project.id]["epic_state"], "Active")

    def test_an_epic_with_unimported_open_children_is_blocked(self) -> None:
        # The trap this report exists for: every LOCAL change is done, and the epic has
        # open stories nobody routed onto this board.
        project = self._project(epic="PROJ-1")
        self._change(project, status="done", ticket="PROJ-2")
        got = self._candidates([
            _ticket("PROJ-1", state="Active", category="Active"),
            _done("PROJ-2", raw_type="Story", parent="PROJ-1"),
            _ticket("PROJ-3", "Story", parent="PROJ-1"),
            _ticket("PROJ-4", "Story", parent="PROJ-1"),
        ])
        self.assertEqual(got[project.id]["reason"], "blocked")
        self.assertEqual(got[project.id]["tracker_open"], 2)
        self.assertEqual(sorted(got[project.id]["keys"]), ["PROJ-3", "PROJ-4"])

    def test_a_declined_child_of_the_epic_does_not_block(self) -> None:
        project = self._project(epic="PROJ-1")
        self._change(project, status="done", ticket="PROJ-2")
        got = self._candidates([
            _ticket("PROJ-1", state="Active", category="Active"),
            _done("PROJ-2", raw_type="Story", parent="PROJ-1"),
            _ticket("PROJ-3", "Story", parent="PROJ-1",
                    state="Wont Do", category="Done"),
        ])
        self.assertEqual(got[project.id]["reason"], "changes")

    def test_an_unlinked_project_is_judged_on_its_changes_alone(self) -> None:
        project = self._project()
        self._change(project, status="done")
        self.assertEqual(self._candidates()[project.id]["reason"], "changes")

    def test_a_project_with_no_changes_is_never_offered(self) -> None:
        # No changes is no evidence, not completion. Half this deployment's projects
        # have none, most of them never linked to a tracker at all.
        project = self._project(epic="PROJ-1")
        self.assertNotIn(project.id, self._candidates([_ticket("PROJ-1")]))

    def test_a_project_with_open_work_is_never_offered(self) -> None:
        project = self._project()
        self._change(project, status="done")
        self._change(project, "left to do")
        self.assertNotIn(project.id, self._candidates())

    def test_a_declined_leftover_does_not_stop_the_offer(self) -> None:
        project = self._project()
        self._change(project, status="done")
        self._change(project, "declined", ticket="PROJ-9", bucket="next")
        got = self._candidates([_done("PROJ-9", raw_type="Story", resolution="Won't Do")])
        self.assertEqual(got[project.id]["reason"], "changes")

    def test_a_closed_project_is_never_offered(self) -> None:
        project = self._project(status="closed")
        self._change(project, status="done")
        self.assertNotIn(project.id, self._candidates())

    def test_the_catch_all_is_never_offered(self) -> None:
        project = server_module.portfolio_store.create_project(
            title="Unplanned work", is_catch_all=True
        )
        self._change(project, status="done")
        self.assertNotIn(project.id, self._candidates())

    def test_a_wont_do_epic_is_never_offered(self) -> None:
        # The removal pass owns declined epics; the survivors keep the status a human
        # gave them rather than being offered a close on a decline.
        project = self._project(epic="PROJ-1")
        self._change(project, status="done")
        got = self._candidates([_done("PROJ-1", resolution="Won't Do")])
        self.assertNotIn(project.id, got)


if __name__ == "__main__":
    unittest.main()
