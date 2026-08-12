"""Tests for the project⇄epic link: a project is tracked 1:1 as one epic-level ticket.

Three contracts pinned here:

- the store's 1:1 guarantee, in both directions and across key spellings, mirroring the
  change⇄ticket link it is modelled on;
- the meaning of UNLINKED: a project with no epic is the local-only / pending-upload
  state - reported (pending_upload_report), never blocked, and never counted for the
  auto-created catch-alls;
- the server's extra rules on top of the store: only epic-level tickets may back a
  project, and one ticket backs one thing across BOTH stores (a ticket linked to a
  change cannot also be linked to a project, and vice versa).
"""

import dataclasses
import tempfile
import time
import unittest
from pathlib import Path

from fastapi import HTTPException

from pm_studio import roadmap as roadmap_module
from pm_studio import server as server_module
from pm_studio.config import TrackerConfig
from pm_studio.portfolio import EpicAlreadyLinked, PortfolioError, PortfolioStore, Project
from pm_studio.roadmap import RoadmapStore
from pm_studio.trackers import Ticket


class EpicLinkStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "portfolio.json"
        self.store = PortfolioStore(self.path)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_link_and_unlink_round_trip(self) -> None:
        project = self.store.create_project("Checkout revamp")
        linked = self.store.link_epic(project.id, "jira", "PROJ-9")
        self.assertEqual((linked.tracker_id, linked.ticket_key), ("jira", "PROJ-9"))
        unlinked = self.store.unlink_epic(project.id)
        self.assertIsNone(unlinked.tracker_id)
        self.assertIsNone(unlinked.ticket_key)

    def test_the_link_survives_a_restart(self) -> None:
        project = self.store.create_project("Checkout revamp")
        self.store.link_epic(project.id, "jira", "PROJ-9")
        reloaded = PortfolioStore(self.path).get_project(project.id)
        self.assertEqual((reloaded.tracker_id, reloaded.ticket_key), ("jira", "PROJ-9"))

    def test_an_epic_backs_one_project_only(self) -> None:
        first = self.store.create_project("First")
        second = self.store.create_project("Second")
        self.store.link_epic(first.id, "jira", "PROJ-9")
        with self.assertRaises(EpicAlreadyLinked) as ctx:
            self.store.link_epic(second.id, "jira", "PROJ-9")
        # The conflict names the holder - "already linked" alone would leave the user
        # hunting for which project owns it.
        self.assertEqual(ctx.exception.project.id, first.id)
        self.assertIn("First", str(ctx.exception))

    def test_key_spellings_collide_case_insensitively(self) -> None:
        """proj-9 and PROJ-9 are the same Jira issue, so they must be the same link."""
        first = self.store.create_project("First")
        second = self.store.create_project("Second")
        self.store.link_epic(first.id, "jira", "proj-9")
        self.assertEqual(self.store.get_project(first.id).ticket_key, "PROJ-9")
        with self.assertRaises(EpicAlreadyLinked):
            self.store.link_epic(second.id, "jira", "PROJ-9")
        self.assertEqual(
            self.store.project_for_ticket("jira", "proj-9").id, first.id
        )

    def test_relinking_the_same_pair_is_a_no_op(self) -> None:
        project = self.store.create_project("P")
        self.store.link_epic(project.id, "jira", "PROJ-9")
        again = self.store.link_epic(project.id, "jira", "PROJ-9")
        self.assertEqual(again.ticket_key, "PROJ-9")

    def test_unlinking_an_unlinked_project_is_not_an_error(self) -> None:
        project = self.store.create_project("P")
        self.store.unlink_epic(project.id)

    def test_a_catch_all_cannot_be_linked(self) -> None:
        ids = self.store.ensure_maintenance_scaffold()
        with self.assertRaises(PortfolioError):
            self.store.link_epic(ids["project_id"], "jira", "PROJ-9")
        scoped = self.store.ensure_initiative_catch_all(ids["initiative_id"])
        with self.assertRaises(PortfolioError):
            self.store.link_epic(scoped.id, "jira", "PROJ-9")

    def test_linked_refs_cover_projects(self) -> None:
        project = self.store.create_project("P")
        self.store.link_epic(project.id, "jira", "PROJ-9")
        self.assertEqual(self.store.linked_ticket_refs(), [("jira", "PROJ-9")])

    def test_pending_upload_means_open_real_and_unlinked(self) -> None:
        ids = self.store.ensure_maintenance_scaffold()  # catch-all: exempt plumbing
        self.store.ensure_initiative_catch_all(ids["initiative_id"])
        linked = self.store.create_project("Linked")
        self.store.link_epic(linked.id, "jira", "PROJ-9")
        closed = self.store.create_project("Closed")
        self.store.update_project(closed.id, status="closed")
        local = self.store.create_project("Local only")
        pending = self.store.pending_upload_report()
        self.assertEqual([p["id"] for p in pending], [local.id])
        # And the snapshot carries it, so the boards need no second request.
        self.assertEqual(
            [p["id"] for p in self.store.snapshot()["pending_upload"]], [local.id]
        )

    def test_legacy_project_json_loads_without_link_fields(self) -> None:
        project = Project.from_dict(
            {
                "id": "p1",
                "title": "Old",
                "description": "",
                "status": "open",
                "initiative_id": None,
                "created_at": 1.0,
                "updated_at": 1.0,
                "closed_at": None,
                "is_catch_all": False,
            }
        )
        self.assertIsNone(project.tracker_id)
        self.assertIsNone(project.ticket_key)


def _epic(key: str, raw_type: str = "Epic") -> Ticket:
    return Ticket(
        tracker_id="jira",
        provider="jira",
        key=key,
        type={"Epic": "epic", "Story": "story", "Bug": "bug"}.get(raw_type, "other"),
        raw_type=raw_type,
        title="T",
        state="To Do",
        url=f"https://x/browse/{key}",
        synced_at=time.time(),
    )


class _StubTrackerStore:
    """Just enough of TrackerStore for _resolve_ticket: a configured tracker whose
    catalog is a dict, so no test touches the network or the real cache file."""

    is_configured = True

    def __init__(self, tickets: dict[str, Ticket]) -> None:
        self._tickets = tickets

    def resolve(self, reference: str):
        return ("jira", reference.strip().upper())

    def ensure_ticket(self, tracker_id: str, key: str):
        return self._tickets.get(key)


class EpicLinkEndpointRulesTest(unittest.TestCase):
    """_apply_epic_link / _apply_ticket_link against real stores and a stubbed catalog:
    the epic-only rule and the cross-store 1:1 live in server.py, so they are pinned
    there rather than on either store."""

    TRACKER = TrackerConfig(
        id="jira", provider="jira", label="Jira",
        base_url="https://x", projects=("PROJ",), token="t",
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
        )
        server_module.CONFIG = dataclasses.replace(
            server_module.CONFIG, trackers=(self.TRACKER,)
        )
        server_module.tracker_store = _StubTrackerStore(
            {"PROJ-1": _epic("PROJ-1"), "PROJ-2": _epic("PROJ-2", "Story")}
        )
        server_module.roadmap_store = RoadmapStore()
        server_module.portfolio_store = PortfolioStore(root / "portfolio.json")

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
        ) = self._orig_server
        self._tmp.cleanup()

    def test_an_epic_links_and_an_empty_value_unlinks(self) -> None:
        project = server_module.portfolio_store.create_project("P")
        linked = server_module._apply_epic_link(None, project.id, {"ticket": "PROJ-1"})
        self.assertEqual((linked.tracker_id, linked.ticket_key), ("jira", "PROJ-1"))
        unlinked = server_module._apply_epic_link(None, project.id, {"ticket": ""})
        self.assertIsNone(unlinked.ticket_key)

    def test_a_non_epic_ticket_is_refused_by_name(self) -> None:
        project = server_module.portfolio_store.create_project("P")
        with self.assertRaises(HTTPException) as ctx:
            server_module._apply_epic_link(None, project.id, {"ticket": "PROJ-2"})
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("Story", ctx.exception.detail)
        self.assertIsNone(server_module.portfolio_store.get_project(project.id).tracker_id)

    def test_a_ticket_backing_a_change_cannot_back_a_project(self) -> None:
        item = server_module.roadmap_store.create("checkout", "A change")
        server_module.roadmap_store.link_ticket(item.id, "jira", "PROJ-1")
        project = server_module.portfolio_store.create_project("P")
        with self.assertRaises(HTTPException) as ctx:
            server_module._apply_epic_link(None, project.id, {"ticket": "PROJ-1"})
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn("A change", ctx.exception.detail)

    def test_a_ticket_backing_a_project_cannot_back_a_change(self) -> None:
        project = server_module.portfolio_store.create_project("P")
        server_module.portfolio_store.link_epic(project.id, "jira", "PROJ-1")
        item = server_module.roadmap_store.create("checkout", "A change")
        with self.assertRaises(HTTPException) as ctx:
            server_module._apply_ticket_link(None, item.id, {"ticket": "PROJ-1"})
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn("P", ctx.exception.detail)
        self.assertIsNone(server_module.roadmap_store.get(item.id).tracker_id)

    def test_a_conflicting_epic_link_is_a_409_naming_the_holder(self) -> None:
        first = server_module.portfolio_store.create_project("First")
        second = server_module.portfolio_store.create_project("Second")
        server_module._apply_epic_link(None, first.id, {"ticket": "PROJ-1"})
        with self.assertRaises(HTTPException) as ctx:
            server_module._apply_epic_link(None, second.id, {"ticket": "PROJ-1"})
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn("First", ctx.exception.detail)


if __name__ == "__main__":
    unittest.main()
