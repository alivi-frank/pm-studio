"""Tests for tracker import routing: routes in config, and the import pass itself.

The two contracts that would rot silently: a route validated wrong (imports landing on
a product the config never named), and idempotency (a re-sync double-importing every
ticket). Both are pinned here against the real RoadmapStore - the import pass runs with
a stubbed catalog but writes through the same store the board reads.
"""

import dataclasses
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

from pm_studio import roadmap as roadmap_module
from pm_studio import server as server_module
from pm_studio.config import SystemSpec, TrackerConfig, TrackerRoute, load_config
from pm_studio.roadmap import RoadmapStore
from pm_studio.trackers import Ticket


class RouteConfigTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "pm_studio_local").mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write(self, config: str):
        (self.root / "pm_studio_local" / "config.toml").write_text(textwrap.dedent(config))
        return load_config(self.root)

    BASE = """\
        [products.checkout]
        label = "Checkout"
        systems = ["claims"]

        [products]
        ops = "Ops"

        [systems]
        claims = "Claims Processor"
        rides = "Rides"

        [[trackers]]
        provider = "jira"
        base_url = "https://acme.atlassian.net"
        projects = ["PROJ"]
        token = "t"
    """

    def test_no_routes_means_no_importing(self) -> None:
        cfg = self._write(self.BASE)
        self.assertEqual(cfg.trackers[0].routes, ())
        self.assertFalse(cfg.trackers[0].imports)

    def test_routes_parse_and_validate(self) -> None:
        cfg = self._write(self.BASE + """\
            import_types = ["Epic", "Story"]

            [[trackers.routes]]
            component = "Checkout Web"
            product = "checkout"
            system = "claims"
        """)
        tracker = cfg.trackers[0]
        self.assertTrue(tracker.imports)
        self.assertEqual(
            tracker.routes,
            (TrackerRoute(component="Checkout Web", product="checkout", system="claims"),),
        )

    def test_unknown_product_is_fatal(self) -> None:
        with self.assertRaises(SystemExit):
            self._write(self.BASE + """\
                import_types = ["Epic"]

                [[trackers.routes]]
                component = "X"
                product = "checkuot"
            """)

    def test_system_the_product_does_not_touch_is_fatal(self) -> None:
        """The same rule validate_system enforces on create, applied at boot so a bad
        route fails loudly instead of at 3am mid-sync."""
        with self.assertRaises(SystemExit):
            self._write(self.BASE + """\
                import_types = ["Epic"]

                [[trackers.routes]]
                component = "X"
                product = "checkout"
                system = "rides"
            """)

    def test_duplicate_component_is_fatal(self) -> None:
        with self.assertRaises(SystemExit):
            self._write(self.BASE + """\
                import_types = ["Epic"]

                [[trackers.routes]]
                component = "X"
                product = "checkout"

                [[trackers.routes]]
                component = "x"
                product = "ops"
            """)

    def test_types_without_routes_is_fatal(self) -> None:
        """A config that looks like it imports and silently doesn't."""
        with self.assertRaises(SystemExit):
            self._write(self.BASE + 'import_types = ["Epic"]\n')


def _ticket(key, raw_type="Story", components=(), state_category="To Do", title=None):
    return Ticket(
        tracker_id="jira", provider="jira", key=key, type="story", raw_type=raw_type,
        title=title or f"Ticket {key}", state=state_category, url=f"https://x/{key}",
        synced_at=time.time(), state_category=state_category,
        components=list(components),
    )


class _StubTrackerStore:
    def __init__(self, tickets):
        self._tickets = tickets

    def tickets_of(self, tracker_id):
        return [t for t in self._tickets if t.tracker_id == tracker_id]

    def describe(self) -> dict:
        return {"configured": True, "trackers": []}


class ImportPassTest(unittest.TestCase):
    """_import_routed_tickets against the real RoadmapStore."""

    TRACKER = TrackerConfig(
        id="jira", provider="jira", label="Jira", base_url="https://x",
        projects=("PROJ",), token="t",
        import_types=("Epic", "Story"),
        routes=(
            TrackerRoute(component="Checkout Web", product="checkout", system="claims"),
            TrackerRoute(component="Ops Misc", product="ops"),
        ),
    )

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_roadmap = (
            roadmap_module.ROADMAP_DIR, roadmap_module.PRODUCTS,
            roadmap_module.SYSTEMS, roadmap_module.PRODUCT_SYSTEMS,
        )
        roadmap_module.ROADMAP_DIR = Path(self._tmp.name)
        roadmap_module.PRODUCTS = {"checkout": "Checkout", "ops": "Ops"}
        roadmap_module.SYSTEMS = {"claims": SystemSpec(label="Claims Processor")}
        roadmap_module.PRODUCT_SYSTEMS = {"checkout": ("claims",)}
        self.store = RoadmapStore()
        self._orig_server = (
            server_module.CONFIG, server_module.tracker_store,
            server_module.roadmap_store, dict(server_module._import_report),
        )
        server_module.CONFIG = dataclasses.replace(
            server_module.CONFIG, trackers=(self.TRACKER,)
        )
        server_module.roadmap_store = self.store
        server_module._import_report.clear()

    def tearDown(self) -> None:
        (
            roadmap_module.ROADMAP_DIR, roadmap_module.PRODUCTS,
            roadmap_module.SYSTEMS, roadmap_module.PRODUCT_SYSTEMS,
        ) = self._orig_roadmap
        (
            server_module.CONFIG, server_module.tracker_store,
            server_module.roadmap_store, report,
        ) = self._orig_server
        server_module._import_report.clear()
        server_module._import_report.update(report)
        self._tmp.cleanup()

    def _run(self, tickets):
        server_module.tracker_store = _StubTrackerStore(tickets)
        server_module._import_routed_tickets()

    def test_routed_ticket_becomes_a_linked_attributed_change(self) -> None:
        self._run([_ticket("PROJ-1", components=["Checkout Web"])])
        items = self.store.list_product("checkout")
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item["system"], "claims")
        self.assertEqual((item["tracker_id"], item["ticket_key"]), ("jira", "PROJ-1"))
        self.assertEqual(item["status"], "pending")
        self.assertEqual(server_module._import_report["jira"]["imported"], 1)

    def test_second_pass_imports_nothing(self) -> None:
        """Idempotency, the contract that makes running after every sync safe."""
        tickets = [_ticket("PROJ-1", components=["Checkout Web"])]
        self._run(tickets)
        self._run(tickets)
        self.assertEqual(len(self.store.list_product("checkout")), 1)
        self.assertEqual(server_module._import_report["jira"]["imported"], 0)

    def test_route_without_system_imports_unattributed(self) -> None:
        """An imported change you can see beats a refused one you can't."""
        self._run([_ticket("PROJ-2", components=["Ops Misc"])])
        item = self.store.list_product("ops")[0]
        self.assertIsNone(item["system"])

    def test_status_maps_from_the_state_category(self) -> None:
        self._run([
            _ticket("PROJ-3", components=["Checkout Web"], state_category="In Progress"),
            _ticket("PROJ-4", components=["Checkout Web"], state_category="Done"),
        ])
        by_key = {i["ticket_key"]: i for i in self.store.list_product("checkout")}
        self.assertEqual(by_key["PROJ-3"]["status"], "in_progress")
        self.assertEqual(by_key["PROJ-4"]["status"], "done")

    def test_unrouted_is_reported_never_guessed(self) -> None:
        self._run([
            _ticket("PROJ-5", components=["Mystery Component"]),
            _ticket("PROJ-6", components=[]),
        ])
        report = server_module._import_report["jira"]
        self.assertEqual(report["unrouted_total"], 2)
        self.assertIn("Mystery Component", report["unrouted"])
        self.assertIn("(no component)", report["unrouted"])
        self.assertEqual(self.store.list_all(), {"checkout": [], "ops": []})

    def test_described_trackers_carries_the_report_and_terminates(self) -> None:
        """Regression: a scripted refactor once rewrote this function's own body into a
        self-call, and nothing exercised it - every board load then 500ed on live
        deployments while the whole suite stayed green. The one shape every TRACKERS
        consumer gets must be tested as a real call."""
        self._run([_ticket("PROJ-9", components=["Checkout Web"])])
        described = server_module._described_trackers()
        self.assertIn("imports", described)
        self.assertEqual(described["imports"]["jira"]["imported"], 1)

    def test_type_filter_and_manual_links_are_respected(self) -> None:
        # A Bug is not in import_types; a manually linked ticket is not re-imported.
        manual = self.store.create("checkout", "Done by hand", system="claims")
        self.store.link_ticket(manual.id, "jira", "PROJ-7")
        self._run([
            _ticket("PROJ-7", components=["Checkout Web"]),
            _ticket("PROJ-8", raw_type="Bug", components=["Checkout Web"]),
        ])
        self.assertEqual(len(self.store.list_product("checkout")), 1)


if __name__ == "__main__":
    unittest.main()
