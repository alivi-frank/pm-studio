"""Tests for the release catalog sync (Jira project versions / ADO iterations).

Releases are imported and cached only - nothing on the board consumes them yet - so
what these tests pin is the data contract: the exact requests sent, how each provider's
payload becomes a Release, and that the catalog persists, survives failures, and never
leaks a token. Same posture as test_trackers.py: trackers._request is the single HTTP
seam and every test replaces it, so nothing here touches the network.
"""

import dataclasses
import json
import tempfile
import time
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from pm_studio import config as config_module
from pm_studio import trackers as trackers_module
from pm_studio.config import TrackerConfig
from pm_studio.trackers import AdoClient, JiraClient, Release, TrackerError, TrackerStore

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


class _Recorder:
    """Stands in for trackers._request, recording calls and replaying scripted responses."""

    def __init__(self, handler) -> None:
        self.calls: list[dict] = []
        self._handler = handler

    def __call__(self, url, *, headers, method="GET", body=None, timeout=20):
        self.calls.append({"url": url, "headers": headers, "method": method, "body": body})
        return self._handler(url, method, body)


class _PatchRequest:
    def __init__(self, handler) -> None:
        self.recorder = _Recorder(handler)

    def __enter__(self) -> _Recorder:
        self._original = trackers_module._request
        trackers_module._request = self.recorder
        return self.recorder

    def __exit__(self, *exc) -> None:
        trackers_module._request = self._original


def jira_version(vid, name, **extra):
    return {"id": vid, "name": name, **extra}


class JiraReleaseFetchTest(unittest.TestCase):
    def test_versions_are_fetched_per_project_and_parsed(self) -> None:
        def handler(url, method, body):
            return {
                "values": [
                    jira_version(
                        10001,
                        "1.0",
                        description="First GA",
                        released=True,
                        archived=False,
                        startDate="2026-01-01",
                        releaseDate="2026-03-01",
                    ),
                    jira_version(10002, "2.0"),
                ],
                "isLast": True,
            }

        with _PatchRequest(handler) as recorder:
            releases = JiraClient(JIRA).fetch_releases()

        url = urlsplit(recorder.calls[0]["url"])
        self.assertEqual(url.path, "/rest/api/3/project/PROJ/version")
        self.assertEqual(len(releases), 2)
        shipped = releases[0]
        self.assertEqual(shipped.key, "10001")
        self.assertEqual(shipped.name, "1.0")
        self.assertEqual(shipped.description, "First GA")
        self.assertTrue(shipped.released)
        self.assertFalse(shipped.archived)
        self.assertEqual(shipped.start_date, "2026-01-01")
        self.assertEqual(shipped.release_date, "2026-03-01")
        self.assertEqual(shipped.project, "PROJ")
        self.assertEqual(shipped.url, "https://acme.atlassian.net/projects/PROJ/versions/10001")
        # A version Jira has not dated or shipped comes through as honestly empty.
        planned = releases[1]
        self.assertFalse(planned.released)
        self.assertEqual(planned.start_date, "")
        self.assertEqual(planned.release_date, "")

    def test_pagination_follows_startAt_until_isLast(self) -> None:
        def handler(url, method, body):
            start = int(parse_qs(urlsplit(url).query)["startAt"][0])
            if start == 0:
                return {"values": [jira_version(1, "a")], "isLast": False}
            return {"values": [jira_version(2, "b")], "isLast": True}

        with _PatchRequest(handler) as recorder:
            releases = JiraClient(JIRA).fetch_releases()
        self.assertEqual([r.key for r in releases], ["1", "2"])
        self.assertEqual(len(recorder.calls), 2)

    def test_server_dc_falls_back_to_the_plain_versions_endpoint(self) -> None:
        """The paginated endpoint is Cloud-only; a 404 means try /versions, v3 then v2 -
        the same fallback contract as the ticket search."""

        def handler(url, method, body):
            path = urlsplit(url).path
            if path.endswith("/version"):
                raise TrackerError("HTTP 404: nope")
            if "/rest/api/3/" in path:
                raise TrackerError("HTTP 404: nope")
            # _request wraps a bare JSON array as {"value": [...]}.
            return {"value": [jira_version(7, "legacy", released=True)]}

        with _PatchRequest(handler) as recorder:
            releases = JiraClient(JIRA).fetch_releases()
        self.assertEqual([r.name for r in releases], ["legacy"])
        paths = [urlsplit(c["url"]).path for c in recorder.calls]
        self.assertEqual(
            paths,
            [
                "/rest/api/3/project/PROJ/version",
                "/rest/api/3/project/PROJ/versions",
                "/rest/api/2/project/PROJ/versions",
            ],
        )

    def test_a_non_404_failure_propagates(self) -> None:
        """A 401 is a broken credential, not a missing endpoint - falling back would
        just fail again and bury the real error."""

        def handler(url, method, body):
            raise TrackerError("HTTP 401: no")

        with _PatchRequest(handler):
            with self.assertRaises(TrackerError):
                JiraClient(JIRA).fetch_releases()


def ado_node(nid, name, start=None, finish=None, children=()):
    node = {"id": nid, "name": name, "children": list(children)}
    attributes = {}
    if start:
        attributes["startDate"] = start
    if finish:
        attributes["finishDate"] = finish
    if attributes:
        node["attributes"] = attributes
    return node


class AdoReleaseFetchTest(unittest.TestCase):
    def _tree(self):
        # The root is the project's iteration root, not a release.
        return {
            **ado_node(
                1,
                "Platform",
                children=[
                    ado_node(
                        2,
                        "Release 1",
                        start="2020-01-01T00:00:00Z",
                        finish="2020-02-01T00:00:00Z",
                        children=[
                            ado_node(
                                3, "Sprint 1",
                                start="2020-01-01T00:00:00Z",
                                finish="2020-01-14T00:00:00Z",
                            )
                        ],
                    ),
                    ado_node(4, "Backlog"),
                ],
            ),
            "url": "https://dev.azure.com/acme/_apis/wit/classificationNodes/Iterations/1",
        }

    def test_the_iteration_tree_is_flattened_and_the_root_skipped(self) -> None:
        with _PatchRequest(lambda u, m, b: self._tree()) as recorder:
            releases = AdoClient(ADO).fetch_releases()

        url = urlsplit(recorder.calls[0]["url"])
        self.assertEqual(url.path, "/acme/Platform/_apis/wit/classificationnodes/iterations")
        query = parse_qs(url.query)
        self.assertEqual(query["$depth"], ["10"])
        self.assertEqual([r.name for r in releases], ["Release 1", "Sprint 1", "Backlog"])
        self.assertNotIn("Platform", [r.name for r in releases])

    def test_dates_are_truncated_and_released_is_derived_from_the_finish(self) -> None:
        with _PatchRequest(lambda u, m, b: self._tree()):
            releases = {r.name: r for r in AdoClient(ADO).fetch_releases()}

        past = releases["Release 1"]
        self.assertEqual(past.start_date, "2020-01-01")
        self.assertEqual(past.release_date, "2020-02-01")
        self.assertTrue(past.released)
        # No dates at all: not finished, honestly undated.
        undated = releases["Backlog"]
        self.assertFalse(undated.released)
        self.assertEqual(undated.release_date, "")

    def test_a_future_iteration_is_not_released(self) -> None:
        tree = ado_node(
            1, "Platform",
            children=[ado_node(2, "Next", start="2999-01-01T00:00:00Z", finish="2999-02-01T00:00:00Z")],
        )
        with _PatchRequest(lambda u, m, b: tree):
            (release,) = AdoClient(ADO).fetch_releases()
        self.assertFalse(release.released)


class _StoreCase(unittest.TestCase):
    """Base for tests needing a real TrackerStore over a temp workspace."""

    TRACKERS: tuple[TrackerConfig, ...] = (JIRA, ADO)

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self._orig_config = config_module.CONFIG
        config_module.CONFIG = dataclasses.replace(
            config_module.CONFIG,
            repo_root=root,
            workspace_root="ws",
            products={"app": "App"},
            trackers=self.TRACKERS,
        )
        trackers_module.CONFIG = config_module.CONFIG
        trackers_module.CATALOG_PATH = config_module.CONFIG.workspace_dir / "trackers.json"
        trackers_module.CATALOG_PATH.parent.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        config_module.CONFIG = self._orig_config
        trackers_module.CONFIG = self._orig_config
        trackers_module.CATALOG_PATH = self._orig_config.workspace_dir / "trackers.json"
        self._tmp.cleanup()


class StoreReleaseSyncTest(_StoreCase):
    def _handler(self, url, method, body):
        path = urlsplit(url).path
        if "/search/jql" in path:
            return {"issues": [], "isLast": True}
        if path.endswith("/version"):
            return {
                "values": [jira_version(10, "1.0", released=True, releaseDate="2026-03-01")],
                "isLast": True,
            }
        if "classificationnodes" in path:
            return ado_node(
                1, "Platform",
                children=[ado_node(2, "Sprint 9", finish="2020-01-14T00:00:00Z")],
            )
        if "wiql" in path:
            return {"workItems": []}
        return {"value": []}

    def test_sync_populates_releases_for_both_trackers_and_persists(self) -> None:
        store = TrackerStore()
        with _PatchRequest(self._handler):
            described = store.sync()

        self.assertEqual([r.name for r in store.releases_of("jira")], ["1.0"])
        self.assertEqual([r.name for r in store.releases_of("ado")], ["Sprint 9"])
        by_id = {t["id"]: t for t in described["trackers"]}
        self.assertEqual(by_id["jira"]["release_count"], 1)
        self.assertEqual(by_id["jira"]["status"]["release_count"], 1)
        self.assertIsNone(by_id["jira"]["status"]["release_error"])
        # A fresh store reads the cache back rather than needing another sync.
        reloaded = TrackerStore()
        self.assertEqual([r.name for r in reloaded.releases_of("jira")], ["1.0"])

    def test_a_release_failure_does_not_fail_the_ticket_sync(self) -> None:
        def handler(url, method, body):
            if "/version" in urlsplit(url).path or "classificationnodes" in url:
                raise TrackerError("HTTP 500: versions exploded")
            return self._handler(url, method, body)

        store = TrackerStore()
        with _PatchRequest(handler):
            described = store.sync()
        status = {t["id"]: t["status"] for t in described["trackers"]}["jira"]
        self.assertTrue(status["ok"])
        self.assertIsNone(status["error"])
        self.assertIn("versions exploded", status["release_error"])

    def test_a_failed_release_fetch_keeps_the_previous_slice(self) -> None:
        """Same posture as the ticket catalog: last sync's releases beat an empty list
        that claims there are none."""
        store = TrackerStore()
        with _PatchRequest(self._handler):
            store.sync()

        def broken(url, method, body):
            if "/version" in urlsplit(url).path or "classificationnodes" in url:
                raise TrackerError("boom")
            return self._handler(url, method, body)

        with _PatchRequest(broken):
            described = store.sync()
        self.assertEqual([r.name for r in store.releases_of("jira")], ["1.0"])
        by_id = {t["id"]: t for t in described["trackers"]}
        # The kept slice is what the count reports - not zero.
        self.assertEqual(by_id["jira"]["status"]["release_count"], 1)

    def test_a_successful_sync_drops_releases_deleted_upstream(self) -> None:
        store = TrackerStore()
        with _PatchRequest(self._handler):
            store.sync()

        def shrunk(url, method, body):
            if urlsplit(url).path.endswith("/version"):
                return {"values": [], "isLast": True}
            return self._handler(url, method, body)

        with _PatchRequest(shrunk):
            store.sync()
        self.assertEqual(store.releases_of("jira"), [])
        # ...without disturbing the other tracker's slice.
        self.assertEqual([r.name for r in store.releases_of("ado")], ["Sprint 9"])

    def test_a_cache_written_before_releases_existed_still_loads(self) -> None:
        trackers_module.CATALOG_PATH.write_text(
            json.dumps({"tickets": [], "statuses": [], "last_sync_at": None})
        )
        store = TrackerStore()  # must not raise
        self.assertEqual(store.list_releases(), [])

    def test_list_releases_filters_and_orders_deterministically(self) -> None:
        store = TrackerStore()
        with _PatchRequest(self._handler):
            store.sync()
        everything = store.list_releases()
        self.assertEqual([r["tracker_id"] for r in everything], ["ado", "jira"])
        self.assertEqual([r["name"] for r in store.list_releases("jira")], ["1.0"])

    def test_neither_the_cache_nor_the_payload_contains_a_token(self) -> None:
        store = TrackerStore()
        with _PatchRequest(self._handler):
            store.sync()
        for blob in (trackers_module.CATALOG_PATH.read_text(), json.dumps(store.describe())):
            self.assertNotIn("jira-token-secret", blob)
            self.assertNotIn("ado-pat-secret", blob)

    def test_release_errors_are_scrubbed_of_credentials(self) -> None:
        def handler(url, method, body):
            if "/version" in urlsplit(url).path or "classificationnodes" in url:
                raise TrackerError("HTTP 500: jira-token-secret leaked into an error")
            return self._handler(url, method, body)

        store = TrackerStore()
        with _PatchRequest(handler):
            described = store.sync()
        status = {t["id"]: t["status"] for t in described["trackers"]}["jira"]
        self.assertNotIn("jira-token-secret", status["release_error"])
        self.assertIn("***", status["release_error"])


class ReleaseRoundTripTest(unittest.TestCase):
    def test_to_dict_from_dict_round_trips(self) -> None:
        release = Release(
            tracker_id="jira",
            provider="jira",
            key="10001",
            name="1.0",
            description="d",
            released=True,
            archived=False,
            start_date="2026-01-01",
            release_date="2026-03-01",
            url="https://acme.atlassian.net/projects/PROJ/versions/10001",
            project="PROJ",
            synced_at=time.time(),
        )
        self.assertEqual(Release.from_dict(release.to_dict()), release)

    def test_missing_fields_default_rather_than_break(self) -> None:
        """A cache written by an older (or newer) build must load, not crash the boot."""
        release = Release.from_dict({"tracker_id": "jira", "provider": "jira", "key": "1"})
        self.assertEqual(release.name, "")
        self.assertFalse(release.released)
        self.assertEqual(release.synced_at, 0.0)


if __name__ == "__main__":
    unittest.main()
