"""Tests for PUSH: creating a tracker ticket for work planned here.

Push is the inverse of the import pass and the write half of a tracker connection, so
the contracts worth pinning are the ones that stop it doing damage:

- **Opt-in, and fatally validated.** Without a `[trackers.push]` table a tracker is
  read-only and nothing offers a push. A table that names a project the tracker does not
  sync, or sits on a provider that cannot push, refuses to boot rather than becoming a
  button that always fails.
- **The request we actually send.** Like every other test in this area, `trackers._request`
  is replaced with a recorder, so these pin the create URL, method, body and the ADF
  description shape without anybody needing a Jira.
- **Exactly one ticket per push.** A change that is already linked is refused; a created
  ticket is always linked or else its key is in the error. Losing a created key is the
  one failure worse than a failed push, so it has its own test.
- **The hierarchy survives the trip.** A change whose project is linked to an epic is
  pushed as that epic's child, and a Jira that will not accept a parent still gets its
  ticket - with the skip reported rather than swallowed.
"""

import dataclasses
import tempfile
import time
import unittest
from pathlib import Path

from fastapi import HTTPException

from pm_studio import agent as agent_module
from pm_studio import config as config_module
from pm_studio import roadmap as roadmap_module
from pm_studio import server as server_module
from pm_studio import trackers as trackers_module
from pm_studio.config import PushConfig, TrackerConfig, _parse_trackers
from pm_studio.portfolio import PortfolioStore
from pm_studio.roadmap import RoadmapStore
from pm_studio.trackers import JiraClient, Ticket, TrackerError, TrackerStore

from .test_trackers import _PatchRequest, jira_issue

JIRA_PUSH = TrackerConfig(
    id="jira",
    provider="jira",
    label="Acme Jira",
    base_url="https://acme.atlassian.net",
    projects=("PROJ", "PLAT"),
    username="pm@acme.com",
    token="jira-token-secret",
    push=PushConfig(project="PROJ", change_type="Story", epic_type="Epic"),
)
JIRA_READ_ONLY = dataclasses.replace(JIRA_PUSH, push=None)


# ---- config ---------------------------------------------------------------------


class PushConfigTest(unittest.TestCase):
    """[trackers.push] parsing. Validated fatally, for the same reason routes are: a
    push table that cannot work is a config that plainly says it can."""

    def _parse(self, table: str) -> tuple:
        import tomllib

        return _parse_trackers(
            tomllib.loads(table), Path("/x/config.toml"), products={"checkout": "Checkout"}
        )

    BASE = """
[[trackers]]
id = "jira"
provider = "jira"
base_url = "https://acme.atlassian.net"
projects = ["PROJ", "PLAT"]
token = "t"
"""

    def test_no_push_table_means_read_only(self) -> None:
        tracker = self._parse(self.BASE)[0]
        self.assertIsNone(tracker.push)
        self.assertFalse(tracker.can_push)

    def test_a_push_table_defaults_the_two_types(self) -> None:
        tracker = self._parse(self.BASE + '\n[trackers.push]\nproject = "PLAT"\n')[0]
        self.assertEqual(tracker.push.project, "PLAT")
        # A stock Jira software project's story and epic rungs - an instance that renamed
        # either says so, but nobody should have to state the defaults.
        self.assertEqual(tracker.push.change_type, "Story")
        self.assertEqual(tracker.push.epic_type, "Epic")
        self.assertTrue(tracker.can_push)

    def test_types_are_overridable(self) -> None:
        tracker = self._parse(
            self.BASE
            + '\n[trackers.push]\nproject = "PROJ"\nchange_type = "Task"\nepic_type = "Initiative"\n'
        )[0]
        self.assertEqual((tracker.push.change_type, tracker.push.epic_type), ("Task", "Initiative"))

    def test_pushing_into_an_unsynced_project_is_fatal(self) -> None:
        """A ticket created where the catalog never looks would read as unresolved on the
        very card that created it - so this fails at boot, not at 3am."""
        with self.assertRaises(SystemExit):
            self._parse(self.BASE + '\n[trackers.push]\nproject = "NOPE"\n')

    def test_a_push_table_with_no_project_is_fatal(self) -> None:
        with self.assertRaises(SystemExit):
            self._parse(self.BASE + "\n[trackers.push]\nepic_type = \"Epic\"\n")

    def test_push_on_ado_is_fatal_rather_than_ignored(self) -> None:
        """Pushing is Jira-only for now. Silently ignoring the table would leave an
        operator believing their ADO board was writable."""
        ado = """
[[trackers]]
id = "ado"
provider = "ado"
organization = "acme"
projects = ["Platform"]
token = "t"

[trackers.push]
project = "Platform"
"""
        with self.assertRaises(SystemExit):
            self._parse(ado)

    def test_a_declared_target_with_no_credential_cannot_push(self) -> None:
        """can_push is both halves: a target nobody can authenticate to would put a
        button on the board that only ever fails."""
        tracker = dataclasses.replace(JIRA_PUSH, token="")
        self.assertIsNotNone(tracker.push)
        self.assertFalse(tracker.can_push)


# ---- the create call ------------------------------------------------------------


class JiraCreateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = JiraClient(JIRA_PUSH)

    def test_the_create_request_shape(self) -> None:
        with _PatchRequest(lambda url, method, body: {"key": "PROJ-42"}) as rec:
            key, parented = self.client.create_issue(
                project="PROJ", issue_type="Story", summary="Ship it", description="Because."
            )
        self.assertEqual((key, parented), ("PROJ-42", False))
        call = rec.calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["url"], "https://acme.atlassian.net/rest/api/3/issue")
        fields = call["body"]["fields"]
        self.assertEqual(fields["project"], {"key": "PROJ"})
        self.assertEqual(fields["issuetype"], {"name": "Story"})
        self.assertEqual(fields["summary"], "Ship it")
        self.assertNotIn("parent", fields)
        # Credentials go in the header, never the URL or the body - same rule as reads.
        self.assertIn("Authorization", call["headers"])
        self.assertNotIn("jira-token-secret", call["url"])

    def test_description_is_adf_on_v3(self) -> None:
        with _PatchRequest(lambda url, method, body: {"key": "PROJ-1"}) as rec:
            self.client.create_issue(
                project="PROJ",
                issue_type="Story",
                summary="s",
                description="First para.\n\nSecond para.",
            )
        doc = rec.calls[0]["body"]["fields"]["description"]
        self.assertEqual(doc["type"], "doc")
        self.assertEqual(
            [block["content"][0]["text"] for block in doc["content"]],
            ["First para.", "Second para."],
        )

    def test_single_newlines_become_hard_breaks_not_raw_newlines(self) -> None:
        """ADF forbids a newline inside a text node, and ignoring that fails silently:
        a change described as a list of lines would arrive as one run-on sentence."""
        with _PatchRequest(lambda url, method, body: {"key": "PROJ-1"}) as rec:
            self.client.create_issue(
                project="PROJ",
                issue_type="Story",
                summary="s",
                description="- Support SSO\n- Support SCIM\n\nSecond para.",
            )
        doc = rec.calls[0]["body"]["fields"]["description"]
        first, second = doc["content"]
        self.assertEqual(
            [n.get("text", n["type"]) for n in first["content"]],
            ["- Support SSO", "hardBreak", "- Support SCIM"],
        )
        self.assertEqual([n["text"] for n in second["content"]], ["Second para."])
        # The invariant itself, checked over the whole document rather than node by node.
        for block in doc["content"]:
            for node in block["content"]:
                self.assertNotIn("\n", node.get("text", ""))

    def test_a_description_of_only_whitespace_makes_an_empty_doc(self) -> None:
        """ADF rejects a paragraph holding an empty text node, so blank input must
        produce no paragraphs at all rather than one empty one."""
        self.assertEqual(self.client._adf("  \n\n  \n ")["content"], [])

    def test_an_empty_description_is_omitted_entirely(self) -> None:
        with _PatchRequest(lambda url, method, body: {"key": "PROJ-1"}) as rec:
            self.client.create_issue(project="PROJ", issue_type="Story", summary="s")
        self.assertNotIn("description", rec.calls[0]["body"]["fields"])

    def test_a_v2_only_instance_gets_plain_text(self) -> None:
        """Server/DC has no /api/3. The 404 is the documented signal to fall back, and
        /api/2 wants the description as a string rather than as ADF."""

        def handler(url, method, body):
            if "/api/3/" in url:
                raise TrackerError("HTTP 404: no such endpoint")
            return {"key": "PROJ-7"}

        with _PatchRequest(handler) as rec:
            key, _ = self.client.create_issue(
                project="PROJ", issue_type="Story", summary="s", description="Plain."
            )
        self.assertEqual(key, "PROJ-7")
        self.assertEqual(rec.calls[-1]["url"], "https://acme.atlassian.net/rest/api/2/issue")
        self.assertEqual(rec.calls[-1]["body"]["fields"]["description"], "Plain.")

    def test_a_real_error_is_not_retried_as_a_version_fallback(self) -> None:
        """A 400 naming a missing field must surface as itself; retrying /api/2 would
        just produce the same error twice and hide which one mattered."""

        def handler(url, method, body):
            raise TrackerError('HTTP 400: {"errors":{"customfield_1":"is required"}}')

        with _PatchRequest(handler) as rec:
            with self.assertRaises(TrackerError) as ctx:
                self.client.create_issue(project="PROJ", issue_type="Story", summary="s")
        self.assertIn("customfield_1", str(ctx.exception))
        self.assertEqual(len(rec.calls), 1)

    def test_a_parent_is_sent_when_asked_for(self) -> None:
        with _PatchRequest(lambda url, method, body: {"key": "PROJ-9"}) as rec:
            key, parented = self.client.create_issue(
                project="PROJ", issue_type="Story", summary="s", parent_key="PROJ-1"
            )
        self.assertTrue(parented)
        self.assertEqual(rec.calls[0]["body"]["fields"]["parent"], {"key": "PROJ-1"})

    def test_a_jira_that_rejects_the_parent_still_gets_its_ticket(self) -> None:
        """An older instance wants a per-instance "Epic Link" custom field whose id we
        cannot know, and answers `parent` with a 400. Refusing the push there would mean
        an instance that cannot parent also cannot push at all."""

        def handler(url, method, body):
            if "parent" in (body or {}).get("fields", {}):
                raise TrackerError('HTTP 400: {"errors":{"parent":"Field cannot be set"}}')
            return {"key": "PROJ-9"}

        with _PatchRequest(handler) as rec:
            key, parented = self.client.create_issue(
                project="PROJ", issue_type="Story", summary="s", parent_key="PROJ-1"
            )
        self.assertEqual(key, "PROJ-9")
        # Reported, not silent: the caller turns this into a message on screen.
        self.assertFalse(parented)
        self.assertEqual(len(rec.calls), 2)

    def test_an_auth_failure_on_a_parented_create_is_not_retried(self) -> None:
        """Only a field-level rejection earns the unparented retry. A 401 would fail the
        same way twice and double the noise in the error the PM reads."""

        def handler(url, method, body):
            raise TrackerError("HTTP 401: Unauthorized")

        with _PatchRequest(handler) as rec:
            with self.assertRaises(TrackerError):
                self.client.create_issue(
                    project="PROJ", issue_type="Story", summary="s", parent_key="PROJ-1"
                )
        self.assertEqual(len(rec.calls), 1)

    def test_a_2xx_with_no_key_is_loud(self) -> None:
        """We cannot say whether the issue exists, and a silent success would strand it."""
        with _PatchRequest(lambda url, method, body: {"id": "10001"}):
            with self.assertRaises(TrackerError) as ctx:
                self.client.create_issue(project="PROJ", issue_type="Story", summary="s")
        self.assertIn("no issue key", str(ctx.exception))


# ---- the store ------------------------------------------------------------------


class PushTicketStoreTest(unittest.TestCase):
    TRACKERS: tuple[TrackerConfig, ...] = (JIRA_PUSH,)

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self._orig = config_module.CONFIG
        config_module.CONFIG = dataclasses.replace(
            config_module.CONFIG, repo_root=root, workspace_root="ws", trackers=self.TRACKERS
        )
        trackers_module.CONFIG = config_module.CONFIG
        trackers_module.CATALOG_PATH = config_module.CONFIG.workspace_dir / "trackers.json"
        trackers_module.CATALOG_PATH.parent.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        config_module.CONFIG = self._orig
        trackers_module.CONFIG = self._orig
        trackers_module.CATALOG_PATH = self._orig.workspace_dir / "trackers.json"
        self._tmp.cleanup()

    def _handler(self, url, method, body):
        if method == "POST":
            return {"key": "PROJ-42"}
        # The read-back: the tracker's own facts about what it just made.
        return jira_issue("PROJ-42", "Story", summary="Ship it", state="To Do")

    def test_a_push_lands_in_the_catalog_with_the_trackers_own_facts(self) -> None:
        """The catalog entry comes from reading the issue back, not from echoing what we
        asked for - so it carries the workflow's opening status, not a guess at one."""
        store = TrackerStore()
        with _PatchRequest(self._handler):
            ticket, parented = store.push_ticket(
                "jira", project="PROJ", issue_type="Story", summary="Ship it"
            )
        self.assertEqual(ticket.key, "PROJ-42")
        self.assertEqual(ticket.state, "To Do")
        self.assertEqual(ticket.type, "story")
        self.assertFalse(parented)
        self.assertEqual(store.lookup("jira", "PROJ-42").title, "Ship it")
        # And it is persisted, so a restart before the next sync keeps the badge.
        self.assertEqual(TrackerStore().lookup("jira", "PROJ-42").key, "PROJ-42")

    def test_an_unreadable_new_issue_is_still_cataloged_from_what_we_know(self) -> None:
        """A credential that can create but not read must not lose the key. Every field
        of the fallback entry is something we know to be true; the state is left empty
        rather than guessed."""

        def handler(url, method, body):
            if method == "POST":
                return {"key": "PROJ-42"}
            raise TrackerError("HTTP 403: no browse permission")

        store = TrackerStore()
        with _PatchRequest(handler):
            ticket, _ = store.push_ticket(
                "jira", project="PROJ", issue_type="Story", summary="Ship it"
            )
        self.assertEqual(ticket.key, "PROJ-42")
        self.assertEqual(ticket.state, "")
        self.assertEqual(ticket.raw_type, "Story")
        self.assertEqual(ticket.url, "https://acme.atlassian.net/browse/PROJ-42")
        self.assertIsNotNone(store.lookup("jira", "PROJ-42"))

    def test_a_read_only_tracker_refuses_before_any_call(self) -> None:
        config_module.CONFIG = dataclasses.replace(
            config_module.CONFIG, trackers=(JIRA_READ_ONLY,)
        )
        trackers_module.CONFIG = config_module.CONFIG
        store = TrackerStore()
        with _PatchRequest(self._handler) as rec:
            with self.assertRaises(ValueError):
                store.push_ticket("jira", project="PROJ", issue_type="Story", summary="s")
        self.assertEqual(rec.calls, [])

    def test_an_unknown_tracker_refuses_before_any_call(self) -> None:
        store = TrackerStore()
        with _PatchRequest(self._handler) as rec:
            with self.assertRaises(ValueError):
                store.push_ticket("nope", project="PROJ", issue_type="Story", summary="s")
        self.assertEqual(rec.calls, [])

    def test_a_token_never_survives_into_a_push_error(self) -> None:
        """A push error is rendered in a browser verbatim - it carries Jira's own body,
        which is the useful part - so it is exactly the path a credential must not take."""

        def handler(url, method, body):
            raise TrackerError("HTTP 401: bad credential jira-token-secret for pm@acme.com")

        store = TrackerStore()
        with _PatchRequest(handler):
            with self.assertRaises(TrackerError) as ctx:
                store.push_ticket("jira", project="PROJ", issue_type="Story", summary="s")
        message = str(ctx.exception)
        self.assertNotIn("jira-token-secret", message)
        self.assertNotIn("pm@acme.com", message)
        # Still says what went wrong, or scrubbing would have cost the diagnosis.
        self.assertIn("401", message)

    def test_describe_exposes_the_push_target(self) -> None:
        """What the board keys its push affordance on. A read-only tracker answers None,
        so the button is absent rather than present-and-broken."""
        store = TrackerStore()
        push = store.describe()["trackers"][0]["push"]
        self.assertEqual(push["project"], "PROJ")
        self.assertEqual(push["change_type"], "Story")
        self.assertEqual(push["projects"], ["PROJ", "PLAT"])

        config_module.CONFIG = dataclasses.replace(
            config_module.CONFIG, trackers=(JIRA_READ_ONLY,)
        )
        trackers_module.CONFIG = config_module.CONFIG
        self.assertIsNone(TrackerStore().describe()["trackers"][0]["push"])


# ---- the endpoints --------------------------------------------------------------


class _PushRecorder:
    """Stands in for tracker_store on the push path: records what the endpoint asked for
    and replays one scripted outcome, so the endpoint's own rules are what is under test."""

    is_configured = True

    def __init__(self, ticket: Ticket | None = None, parented: bool = False, error=None) -> None:
        self.ticket = ticket
        self.parented = parented
        self.error = error
        self.calls: list[dict] = []

    def push_ticket(self, tracker_id, **kwargs):
        self.calls.append({"tracker_id": tracker_id, **kwargs})
        if self.error is not None:
            raise self.error
        return self.ticket, self.parented

    def lookup(self, tracker_id, key):
        return self.ticket if self.ticket and self.ticket.key == key else None


def _ticket(key: str, raw_type: str = "Story") -> Ticket:
    return Ticket(
        tracker_id="jira",
        provider="jira",
        key=key,
        type={"Epic": "epic", "Story": "story"}.get(raw_type, "other"),
        raw_type=raw_type,
        title="T",
        state="To Do",
        url=f"https://acme.atlassian.net/browse/{key}",
        synced_at=time.time(),
    )


class PushEndpointTest(unittest.TestCase):
    TRACKERS: tuple[TrackerConfig, ...] = (JIRA_PUSH,)

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
        server_module.CONFIG = dataclasses.replace(server_module.CONFIG, trackers=self.TRACKERS)
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

    def _change(self, **kwargs):
        return server_module.roadmap_store.create(
            product="checkout", title=kwargs.pop("title", "Ship it"), **kwargs
        )

    def _push(self, item, payload=None):
        return server_module.push_roadmap_item(
            item.product, item.id, _FakeRequest(), payload or {}
        )

    # ---- the happy path ----

    def test_a_push_creates_and_links_in_one_call(self) -> None:
        recorder = _PushRecorder(_ticket("PROJ-42"))
        server_module.tracker_store = recorder
        item = self._change(description="Because.")
        body = self._push(item)

        self.assertEqual(body["push"]["key"], "PROJ-42")
        self.assertEqual(body["ticket_key"], "PROJ-42")
        self.assertEqual(body["tracker_id"], "jira")
        # And the joined `ticket` is in the response, so the card is not blank until a
        # reload - the same shape every other roadmap write returns.
        self.assertEqual(body["ticket"]["key"], "PROJ-42")
        # Config supplied the target; an empty payload is the ordinary one-click push.
        self.assertEqual(recorder.calls[0]["project"], "PROJ")
        self.assertEqual(recorder.calls[0]["issue_type"], "Story")
        self.assertEqual(recorder.calls[0]["summary"], "Ship it")

    def test_the_description_carries_the_plan_and_a_provenance_line(self) -> None:
        """A ticket that appears on someone else's board with no context reads as noise,
        and the id is what lets them find the change again."""
        recorder = _PushRecorder(_ticket("PROJ-42"))
        server_module.tracker_store = recorder
        item = self._change(description="Because customers ask.")
        self._push(item)
        sent = recorder.calls[0]["description"]
        self.assertTrue(sent.startswith("Because customers ask."))
        self.assertIn("Pushed from PM Studio", sent)
        self.assertIn(item.id, sent)

    def test_a_change_with_no_description_still_gets_the_provenance_line(self) -> None:
        recorder = _PushRecorder(_ticket("PROJ-42"))
        server_module.tracker_store = recorder
        self._push(self._change())
        self.assertTrue(recorder.calls[0]["description"].startswith("Pushed from PM Studio"))

    # ---- overrides ----

    def test_the_payload_overrides_the_configured_target(self) -> None:
        recorder = _PushRecorder(_ticket("PLAT-1"))
        server_module.tracker_store = recorder
        self._push(self._change(), {"project": "PLAT", "issue_type": "Task"})
        self.assertEqual(recorder.calls[0]["project"], "PLAT")
        self.assertEqual(recorder.calls[0]["issue_type"], "Task")

    def test_an_override_onto_an_unsynced_project_is_refused(self) -> None:
        """Same rule config enforces on the declared default: a ticket the catalog never
        pulls would read as unresolved on the card that made it."""
        recorder = _PushRecorder(_ticket("X-1"))
        server_module.tracker_store = recorder
        with self.assertRaises(HTTPException) as ctx:
            self._push(self._change(), {"project": "NOPE"})
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(recorder.calls, [])

    def test_two_pushable_trackers_must_be_disambiguated(self) -> None:
        """Never guessed: creating a ticket on the wrong board is not something a default
        may do silently."""
        second = dataclasses.replace(JIRA_PUSH, id="jira2", label="Other Jira")
        server_module.CONFIG = dataclasses.replace(
            server_module.CONFIG, trackers=(JIRA_PUSH, second)
        )
        recorder = _PushRecorder(_ticket("PROJ-1"))
        server_module.tracker_store = recorder
        with self.assertRaises(HTTPException) as ctx:
            self._push(self._change())
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("jira2", ctx.exception.detail)
        self.assertEqual(recorder.calls, [])
        # Named, it goes through.
        self._push(self._change(title="Second"), {"tracker_id": "jira2"})
        self.assertEqual(recorder.calls[0]["tracker_id"], "jira2")

    def test_a_read_only_deployment_says_what_to_configure(self) -> None:
        server_module.CONFIG = dataclasses.replace(
            server_module.CONFIG, trackers=(JIRA_READ_ONLY,)
        )
        server_module.tracker_store = _PushRecorder(_ticket("PROJ-1"))
        with self.assertRaises(HTTPException) as ctx:
            self._push(self._change())
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("[trackers.push]", ctx.exception.detail)

    def test_a_declared_target_with_no_credential_names_the_real_problem(self) -> None:
        """A .env problem and a config problem need different messages, or the operator
        edits the wrong file."""
        server_module.CONFIG = dataclasses.replace(
            server_module.CONFIG, trackers=(dataclasses.replace(JIRA_PUSH, token=""),)
        )
        server_module.tracker_store = _PushRecorder(_ticket("PROJ-1"))
        with self.assertRaises(HTTPException) as ctx:
            self._push(self._change())
        self.assertIn("cannot be reached", ctx.exception.detail)
        self.assertIn("API token", ctx.exception.detail)

    # ---- no duplicates ----

    def test_an_already_linked_change_is_refused_by_name(self) -> None:
        """The duplicate this endpoint exists to prevent. The 409 names the ticket it
        already has, so the answer is obvious rather than an investigation."""
        recorder = _PushRecorder(_ticket("PROJ-42"))
        server_module.tracker_store = recorder
        item = self._change()
        server_module.roadmap_store.link_ticket(item.id, "jira", "PROJ-7")
        with self.assertRaises(HTTPException) as ctx:
            self._push(server_module.roadmap_store.get(item.id))
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn("PROJ-7", ctx.exception.detail)
        self.assertEqual(recorder.calls, [])

    def test_a_change_on_another_product_is_refused(self) -> None:
        server_module.tracker_store = _PushRecorder(_ticket("PROJ-1"))
        item = self._change()
        with self.assertRaises(HTTPException) as ctx:
            server_module.push_roadmap_item("other", item.id, _FakeRequest(), {})
        self.assertEqual(ctx.exception.status_code, 400)

    def test_an_unknown_change_is_a_404(self) -> None:
        server_module.tracker_store = _PushRecorder(_ticket("PROJ-1"))
        with self.assertRaises(HTTPException) as ctx:
            server_module.push_roadmap_item("checkout", "nope", _FakeRequest(), {})
        self.assertEqual(ctx.exception.status_code, 404)

    # ---- failures ----

    def test_a_tracker_refusal_is_a_502_carrying_jiras_own_words(self) -> None:
        """Jira's body is what names an issue type the project does not have, or a
        required field nobody configured - so it is the useful part of the message."""
        server_module.tracker_store = _PushRecorder(
            error=TrackerError('HTTP 400: {"errors":{"issuetype":"valid values: Bug"}}')
        )
        with self.assertRaises(HTTPException) as ctx:
            self._push(self._change())
        self.assertEqual(ctx.exception.status_code, 502)
        self.assertIn("valid values: Bug", ctx.exception.detail)
        self.assertIn("Acme Jira", ctx.exception.detail)

    def test_a_created_ticket_whose_link_fails_is_named_in_the_error(self) -> None:
        """The one outcome worse than a failed push is a real ticket nothing points at.
        Here the ticket is already held by another change, so the link must fail - and
        the error has to carry the key that now exists upstream."""
        recorder = _PushRecorder(_ticket("PROJ-42"))
        server_module.tracker_store = recorder
        holder = self._change(title="Holder")
        server_module.roadmap_store.link_ticket(holder.id, "jira", "PROJ-42")
        with self.assertRaises(HTTPException) as ctx:
            self._push(self._change(title="Pusher"))
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn("PROJ-42", ctx.exception.detail)
        self.assertIn("The ticket exists", ctx.exception.detail)

    def test_a_key_a_project_already_holds_is_not_linked_to_a_change(self) -> None:
        """The cross-store half of the 1:1 rule on the push path. A fresh key should
        collide with nothing, but "should" is not a guarantee - a Jira project deleted and
        recreated restarts its numbering - and one ticket must back one thing, not two."""
        recorder = _PushRecorder(_ticket("PROJ-42"))
        server_module.tracker_store = recorder
        holder = server_module.portfolio_store.create_project("Holder")
        server_module.portfolio_store.link_epic(holder.id, "jira", "PROJ-42")
        with self.assertRaises(HTTPException) as ctx:
            self._push(self._change())
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn("PROJ-42", ctx.exception.detail)
        self.assertIn("Holder", ctx.exception.detail)

    def test_a_key_a_change_already_holds_is_not_linked_to_a_project(self) -> None:
        recorder = _PushRecorder(_ticket("PROJ-42", "Epic"))
        server_module.tracker_store = recorder
        change = self._change(title="Holder change")
        server_module.roadmap_store.link_ticket(change.id, "jira", "PROJ-42")
        project = server_module.portfolio_store.create_project("P")
        with self.assertRaises(HTTPException) as ctx:
            server_module.push_project_epic(project.id, _FakeRequest(), {})
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn("PROJ-42", ctx.exception.detail)
        self.assertIn("Holder change", ctx.exception.detail)
        self.assertIsNone(server_module.portfolio_store.get_project(project.id).ticket_key)

    # ---- hierarchy ----

    def test_a_change_lands_under_its_projects_epic(self) -> None:
        """The reason projects are pushable at all: push the epic, then every change
        under that project lands beneath it, so the tracker gets the plan's own shape."""
        recorder = _PushRecorder(_ticket("PROJ-42"), parented=True)
        server_module.tracker_store = recorder
        project = server_module.portfolio_store.create_project("Checkout revamp")
        server_module.portfolio_store.link_epic(project.id, "jira", "PROJ-1")
        item = self._change(project_id=project.id)
        body = self._push(item)
        self.assertEqual(recorder.calls[0]["parent_key"], "PROJ-1")
        self.assertEqual(body["push"]["parent_key"], "PROJ-1")
        self.assertFalse(body["push"]["parent_skipped"])

    def test_a_project_epic_in_another_tracker_is_not_a_parent(self) -> None:
        """Cross-tracker parenting is not a thing Jira can express, so it is not
        attempted - the honest answer is an unparented ticket."""
        recorder = _PushRecorder(_ticket("PROJ-42"))
        server_module.tracker_store = recorder
        project = server_module.portfolio_store.create_project("Elsewhere")
        server_module.portfolio_store.link_epic(project.id, "other", "OTH-1")
        self._push(self._change(project_id=project.id))
        self.assertIsNone(recorder.calls[0]["parent_key"])

    def test_a_change_with_no_project_has_no_parent(self) -> None:
        recorder = _PushRecorder(_ticket("PROJ-42"))
        server_module.tracker_store = recorder
        self._push(self._change())
        self.assertIsNone(recorder.calls[0]["parent_key"])

    def test_a_skipped_parent_is_reported_not_swallowed(self) -> None:
        """A story that quietly landed outside its epic is what a PM finds out about a
        week later from someone else's board."""
        recorder = _PushRecorder(_ticket("PROJ-42"), parented=False)
        server_module.tracker_store = recorder
        project = server_module.portfolio_store.create_project("P")
        server_module.portfolio_store.link_epic(project.id, "jira", "PROJ-1")
        body = self._push(self._change(project_id=project.id))
        self.assertTrue(body["push"]["parent_skipped"])
        self.assertIsNone(body["push"]["parent_key"])

    # ---- the project rung ----

    def test_a_project_pushes_as_an_epic_and_links(self) -> None:
        recorder = _PushRecorder(_ticket("PROJ-1", "Epic"))
        server_module.tracker_store = recorder
        project = server_module.portfolio_store.create_project("Checkout revamp")
        body = server_module.push_project_epic(project.id, _FakeRequest(), {})
        self.assertEqual(recorder.calls[0]["issue_type"], "Epic")
        self.assertIsNone(recorder.calls[0]["parent_key"])
        self.assertEqual(body["ticket_key"], "PROJ-1")
        self.assertEqual(
            server_module.portfolio_store.get_project(project.id).ticket_key, "PROJ-1"
        )
        # And it leaves the pending-upload report, which is the whole point.
        self.assertEqual(server_module.portfolio_store.pending_upload_report(), [])

    def test_a_created_non_epic_is_not_linked_as_a_project_epic(self) -> None:
        """The same rule a manual link enforces: letting a Story stand in for a project
        would make every "tracked as" annotation a small lie. The error names the key so
        the ticket that does exist is not lost."""
        recorder = _PushRecorder(_ticket("PROJ-1", "Story"))
        server_module.tracker_store = recorder
        project = server_module.portfolio_store.create_project("P")
        with self.assertRaises(HTTPException) as ctx:
            server_module.push_project_epic(project.id, _FakeRequest(), {})
        self.assertEqual(ctx.exception.status_code, 502)
        self.assertIn("PROJ-1", ctx.exception.detail)
        self.assertIn("epic_type", ctx.exception.detail)
        self.assertIsNone(server_module.portfolio_store.get_project(project.id).ticket_key)

    def test_a_non_epic_epic_type_is_refused_before_anything_is_created(self) -> None:
        """An instance whose epic rung is called something unrecognised must fail with
        nothing created. The post-create check can only report an orphan ticket it is too
        late to prevent - we never delete in the tracker - so this one runs first."""
        server_module.CONFIG = dataclasses.replace(
            server_module.CONFIG,
            trackers=(dataclasses.replace(
                JIRA_PUSH, push=PushConfig(project="PROJ", epic_type="Deliverable")
            ),),
        )
        recorder = _PushRecorder(_ticket("PROJ-1", "Story"))
        server_module.tracker_store = recorder
        project = server_module.portfolio_store.create_project("P")
        with self.assertRaises(HTTPException) as ctx:
            server_module.push_project_epic(project.id, _FakeRequest(), {})
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("Nothing was created", ctx.exception.detail)
        # The point of the test: no API call happened at all.
        self.assertEqual(recorder.calls, [])

    def test_a_bad_epic_type_override_is_refused_too(self) -> None:
        """A per-push override reaches the same trap by a different route, which is why
        the check lives at the endpoint rather than at config load."""
        recorder = _PushRecorder(_ticket("PROJ-1", "Epic"))
        server_module.tracker_store = recorder
        project = server_module.portfolio_store.create_project("P")
        with self.assertRaises(HTTPException) as ctx:
            server_module.push_project_epic(
                project.id, _FakeRequest(), {"issue_type": "Deliverable"}
            )
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(recorder.calls, [])

    def test_a_renamed_but_epic_level_type_is_accepted(self) -> None:
        """Initiative and Theme are the epic rung under other names, so a deployment that
        renamed it is not locked out - only an unrecognised name is."""
        recorder = _PushRecorder(_ticket("PROJ-1", "Epic"))
        server_module.tracker_store = recorder
        project = server_module.portfolio_store.create_project("P")
        server_module.push_project_epic(project.id, _FakeRequest(), {"issue_type": "Initiative"})
        self.assertEqual(recorder.calls[0]["issue_type"], "Initiative")

    def test_a_change_push_is_not_subject_to_the_epic_rule(self) -> None:
        """The rule is about the project rung only: a change may be any type."""
        recorder = _PushRecorder(_ticket("PROJ-9", "Story"))
        server_module.tracker_store = recorder
        self._push(self._change(), {"issue_type": "Deliverable"})
        self.assertEqual(recorder.calls[0]["issue_type"], "Deliverable")

    def test_an_already_tracked_project_is_refused(self) -> None:
        recorder = _PushRecorder(_ticket("PROJ-2", "Epic"))
        server_module.tracker_store = recorder
        project = server_module.portfolio_store.create_project("P")
        server_module.portfolio_store.link_epic(project.id, "jira", "PROJ-1")
        with self.assertRaises(HTTPException) as ctx:
            server_module.push_project_epic(project.id, _FakeRequest(), {})
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn("PROJ-1", ctx.exception.detail)
        self.assertEqual(recorder.calls, [])

    def test_a_catch_all_is_not_epic_material(self) -> None:
        """Auto-created cost-attribution plumbing, not work anyone would file an epic
        for - the same exemption pending_upload_report makes."""
        recorder = _PushRecorder(_ticket("PROJ-1", "Epic"))
        server_module.tracker_store = recorder
        ids = server_module.portfolio_store.ensure_maintenance_scaffold()
        with self.assertRaises(HTTPException) as ctx:
            server_module.push_project_epic(ids["project_id"], _FakeRequest(), {})
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(recorder.calls, [])

    def test_an_unknown_project_is_a_404(self) -> None:
        server_module.tracker_store = _PushRecorder(_ticket("PROJ-1", "Epic"))
        with self.assertRaises(HTTPException) as ctx:
            server_module.push_project_epic("nope", _FakeRequest(), {})
        self.assertEqual(ctx.exception.status_code, 404)


class PushContextAnnotationTest(unittest.TestCase):
    """The initiative context block's "no epic yet" annotation, which the PM reads every
    turn. It has to say whether filing one is POSSIBLE, not just that it hasn't happened:
    the same two wrong answers the prompt guards against, one rung further in."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self._orig_roadmap = (roadmap_module.ROADMAP_DIR, roadmap_module.PRODUCTS)
        roadmap_module.ROADMAP_DIR = root / "roadmap"
        roadmap_module.PRODUCTS = {"checkout": "Checkout"}
        self._orig = (
            server_module.CONFIG,
            server_module.portfolio_store,
            server_module.roadmap_store,
            server_module.tracker_store,
        )
        server_module.portfolio_store = PortfolioStore(root / "portfolio.json")
        server_module.roadmap_store = RoadmapStore()
        server_module.tracker_store = _PushRecorder()

    def tearDown(self) -> None:
        (roadmap_module.ROADMAP_DIR, roadmap_module.PRODUCTS) = self._orig_roadmap
        (
            server_module.CONFIG,
            server_module.portfolio_store,
            server_module.roadmap_store,
            server_module.tracker_store,
        ) = self._orig
        self._tmp.cleanup()

    def _context(self, trackers: tuple[TrackerConfig, ...]) -> str:
        server_module.CONFIG = dataclasses.replace(server_module.CONFIG, trackers=trackers)
        initiative = server_module.portfolio_store.create_initiative("Initiative X")
        project = server_module.portfolio_store.create_project(
            "Local only", initiative_id=initiative.id
        )
        server_module.roadmap_store.create(
            product="checkout", title="A change", project_id=project.id
        )
        block, _ = server_module._initiative_context(initiative.id)
        return block

    def test_a_pushable_deployment_points_at_the_control(self) -> None:
        text = self._context((JIRA_PUSH,))
        self.assertIn("no epic in the tracker yet", text)
        self.assertIn("Push epic", text)
        self.assertNotIn("upload is not available", text)

    def test_a_read_only_deployment_says_upload_is_unavailable(self) -> None:
        text = self._context((JIRA_READ_ONLY,))
        self.assertIn("upload is not available", text)
        self.assertNotIn("Push epic", text)


class PushPromptTest(unittest.TestCase):
    """What the PM agent is TOLD about push.

    The prompt is the only place the PM learns what this system can do, and both wrong
    answers cost the stakeholder a round trip: told push exists when it doesn't, it
    promises an upload nobody can perform; told it doesn't when it does, it refuses one
    that is a single click away. So both shapes are pinned.
    """

    def setUp(self) -> None:
        self._orig = (
            agent_module.CONFIG,
            roadmap_module.SYSTEMS,
            roadmap_module.PRODUCT_SYSTEMS,
            roadmap_module.PRODUCTS,
        )
        roadmap_module.SYSTEMS = agent_module.SYSTEMS = {}
        roadmap_module.PRODUCT_SYSTEMS = {}
        roadmap_module.PRODUCTS = {"checkout": "Checkout"}

    def tearDown(self) -> None:
        (
            agent_module.CONFIG,
            roadmap_module.SYSTEMS,
            roadmap_module.PRODUCT_SYSTEMS,
            roadmap_module.PRODUCTS,
        ) = self._orig
        agent_module.SYSTEMS = self._orig[1]

    def _prompt(self, trackers: tuple[TrackerConfig, ...]) -> str:
        import threading

        agent_module.CONFIG = dataclasses.replace(agent_module.CONFIG, trackers=trackers)
        session = dataclasses.make_dataclass(
            "StubSession",
            [
                "id",
                "product",
                "initiative_id",
                "adopted_products",
                "model",
                "mode",
                "worktree_path",
            ],
        )(
            id="s1",
            product="checkout",
            initiative_id=None,
            adopted_products=[],
            model="claude-opus-5",
            mode="build",
            worktree_path=tempfile.gettempdir(),
        )
        return agent_module.PMAgent(session, threading.Lock()).system_prompt

    def test_a_read_only_deployment_is_still_told_upload_is_unavailable(self) -> None:
        prompt = self._prompt((JIRA_READ_ONLY,))
        self.assertIn("uploading is NOT available", prompt)
        self.assertNotIn("Push epic", prompt)

    def test_a_pushable_deployment_is_told_the_control_exists(self) -> None:
        prompt = self._prompt((JIRA_PUSH,))
        self.assertNotIn("uploading is NOT available", prompt)
        self.assertIn("Push epic", prompt)
        # Named concretely, so the PM can tell the stakeholder where it lands.
        self.assertIn("Acme Jira (PROJ)", prompt)

    def test_the_pm_is_told_not_to_push_on_its_own(self) -> None:
        """The roadmap POST allowlist reaches the push endpoint, so the only thing
        stopping an agent filing tickets on someone else's board is being told not to."""
        prompt = self._prompt((JIRA_PUSH,))
        self.assertIn("STAKEHOLDER's act", prompt)
        self.assertIn("Do not push a change or a project yourself", prompt)

    def test_the_never_updates_promise_holds_either_way(self) -> None:
        """Push creates once. A PM must never claim to have changed a ticket's state,
        whether or not this deployment can create one."""
        for trackers in ((JIRA_PUSH,), (JIRA_READ_ONLY,)):
            self.assertIn("never claim to have changed a ticket's type or status",
                          self._prompt(trackers))


class _FakeRequest:
    """_require reads cookies and headers off the request; in personal mode (the default
    for these tests) it returns None without consulting either."""

    cookies: dict = {}
    headers: dict = {}


if __name__ == "__main__":
    unittest.main()
