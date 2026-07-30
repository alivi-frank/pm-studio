"""Tests for the guarantee that no deploying organisation's data lives in this repo.

This package is public. A deployment's Jira/ADO host, project keys, team names and synced
ticket titles are that organisation's internal structure, and none of it belongs here - it
belongs in the deploying repo's own `pm_studio_local/`, which that organisation controls.

This got violated once in exactly the way you would expect: the tracker feature was
verified by pointing THIS repo's dogfood config at a real company Jira. That put the site
URL and a project key in a committed file, and dropped 3,588 of that company's ticket
titles into `studio_data/` inside this checkout. Both were caught before any push, and
these tests exist so the next person cannot repeat it quietly.

The rule is not "don't write secrets" - it is "this repo describes PM Studio, and nothing
about anyone who deploys it".
"""

import re
import subprocess
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Hosts allowed to appear in tracked files: the documentation placeholders. Anything else
# matching the same shape is a real instance and must not be here.
ALLOWED_HOSTS = {
    "acme.atlassian.net",
    "other.atlassian.net",  # a second instance, for the disambiguation tests
    "dev.azure.com/acme",
}

TRACKER_HOST = re.compile(
    r"\b([a-z0-9][a-z0-9-]*\.atlassian\.net|dev\.azure\.com/[a-z0-9][a-z0-9-]*)\b",
    re.IGNORECASE,
)

# Files whose whole purpose is to document the rule, so they may name the shape of a
# violation without being one.
SELF_EXEMPT = {"tests/test_no_deployment_data.py"}


def tracked_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files"], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    ).stdout
    return [line for line in out.splitlines() if line.strip()]


def read(path: str) -> str:
    try:
        return (REPO_ROOT / path).read_text(errors="replace")
    except (OSError, UnicodeDecodeError):
        return ""


class NoRealTrackerInstanceTest(unittest.TestCase):
    def test_no_tracked_file_names_a_real_tracker_host(self) -> None:
        """A real Jira site or ADO organisation in a committed file publishes which vendor
        instance a customer runs, and usually the customer's own name with it."""
        offenders: list[tuple[str, str]] = []
        for path in tracked_files():
            if path in SELF_EXEMPT:
                continue
            for match in TRACKER_HOST.findall(read(path)):
                if match.lower() not in ALLOWED_HOSTS:
                    offenders.append((path, match))
        self.assertEqual(
            offenders,
            [],
            "real tracker host(s) in tracked files - move this to the deploying repo's "
            f"pm_studio_local/: {offenders}",
        )


class DogfoodConfigDeclaresNoTrackerTest(unittest.TestCase):
    """This repo's own pm_studio_local/ is the config for developing PM Studio. Declaring a
    tracker there does two harmful things at once: it commits the instance's identity, and
    it makes every sync write that instance's ticket titles into studio_data/ here."""

    CONFIG = "pm_studio_local/config.toml"

    def test_the_dogfood_config_declares_no_trackers(self) -> None:
        """Parsed, not grepped: the file explains the rule in a comment, and a substring
        check would trip over the very words documenting it."""
        import tomllib

        raw = tomllib.loads(read(self.CONFIG))
        self.assertIsNone(
            raw.get("trackers"),
            f"{self.CONFIG} declares a tracker. Point a throwaway config at a local fake "
            "tracker instead - the suite covers client behaviour with no network at all.",
        )

    def test_the_config_still_exists_and_describes_this_project(self) -> None:
        """Guards against 'fixing' the test above by deleting the file."""
        text = read(self.CONFIG)
        self.assertIn("[products]", text)
        self.assertIn("[project]", text)


class NoSyncedCatalogInThisCheckoutTest(unittest.TestCase):
    """The catalog cache holds ticket titles pulled from whatever instance was synced. It is
    git-ignored and unstaged from snapshots, so it can never be committed - but it must not
    be sitting in this checkout either, because that is someone else's business data inside
    a public project's directory."""

    def test_no_tracker_catalog_exists_under_this_repo(self) -> None:
        found = [
            str(p.relative_to(REPO_ROOT))
            for p in REPO_ROOT.rglob("trackers.json")
            if ".git" not in p.parts
        ]
        self.assertEqual(
            found,
            [],
            "a synced tracker catalog is present in this checkout; delete it (it is a "
            f"cache and costs one re-sync): {found}",
        )

    def test_the_catalog_is_ignored_for_every_workspace_root(self) -> None:
        """Belt to the snapshot guard's braces, checked for the default workspace root and
        for this repo's own (which is not the default)."""
        from pm_studio.scaffold import GITIGNORE_ENTRIES

        entries = " ".join(GITIGNORE_ENTRIES)
        self.assertIn("trackers.json", entries)

    def test_the_catalog_can_never_be_committed_by_a_snapshot(self) -> None:
        from pm_studio.gitsnapshot import SENSITIVE_WORKSPACE_FILES, sensitive_pathspecs

        self.assertIn("trackers.json", SENSITIVE_WORKSPACE_FILES)
        self.assertIn("any_root/workspace/trackers.json", sensitive_pathspecs("any_root/workspace"))


if __name__ == "__main__":
    unittest.main()
