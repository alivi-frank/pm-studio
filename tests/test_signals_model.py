"""Tests for the signal model: ticket-key extraction, time parsing, day bucketing."""

import unittest

from pm_studio.signals.model import Clock, extract_ticket_refs, is_ai_assisted, parse_iso, signal_id


class ExtractRefsTest(unittest.TestCase):
    def test_jira_and_ado_shapes(self) -> None:
        jira, ado = extract_ticket_refs("Merged PR 13309: ticket #12099 Refine NDT-5561; task #12594; AB#777", jira_projects={"NDT", "PDMP"})
        self.assertEqual(jira, ["NDT-5561"])
        self.assertEqual(ado, ["12099", "12594", "777"])

    def test_pr_numbers_are_not_tickets(self) -> None:
        jira, ado = extract_ticket_refs("Merged in feature/NDT-5508-x (pull request #67)", jira_projects={"NDT"})
        self.assertEqual((jira, ado), (["NDT-5508"], []))

    def test_unknown_jira_projects_filtered(self) -> None:
        jira, _ = extract_ticket_refs("use UTF-8 and SHA-256 for ABC-123", jira_projects={"NDT"})
        self.assertEqual(jira, [])
        jira, _ = extract_ticket_refs("ABC-123", jira_projects=None)
        self.assertEqual(jira, ["ABC-123"])

    def test_dedupes(self) -> None:
        jira, _ = extract_ticket_refs("NDT-1 NDT-1 NDT-2", jira_projects={"NDT"})
        self.assertEqual(jira, ["NDT-1", "NDT-2"])


class ParseTest(unittest.TestCase):
    def test_offsets(self) -> None:
        self.assertEqual(parse_iso("2026-01-01T00:00:00Z"), 1767225600.0)
        self.assertEqual(parse_iso("2026-01-01T00:00:00+0000"), 1767225600.0)
        self.assertEqual(parse_iso("2025-12-31T19:00:00-05:00"), 1767225600.0)
        self.assertIsNone(parse_iso("nonsense"))
        self.assertIsNone(parse_iso(None))

    def test_ai_trailers(self) -> None:
        self.assertTrue(is_ai_assisted("fix\n\nCo-Authored-By: Claude <noreply@anthropic.com>"))
        self.assertFalse(is_ai_assisted("Co-authored-by: Ada Lovelace <ada@example.com>"))

    def test_stable_ids(self) -> None:
        self.assertEqual(signal_id("git", "repo", "abc"), signal_id("git", "repo", "abc"))
        self.assertNotEqual(signal_id("git", "repo", "abc"), signal_id("git", "repo", "abd"))


class ClockTest(unittest.TestCase):
    def test_local_day_not_utc(self) -> None:
        clock = Clock("America/New_York")
        late = parse_iso("2026-09-15T23:30:00-04:00")
        self.assertEqual(clock.day(late), "2026-09-15")
        self.assertEqual(clock.hour(late), 23)
        self.assertEqual(clock.week(late), "2026-W38")
        self.assertEqual(clock.day_end("2026-09-15") - clock.day_start("2026-09-15"), 86400.0)

    def test_unknown_zone_falls_back(self) -> None:
        clock = Clock("Nowhere/Land")
        self.assertEqual(clock.day(0.0), "1970-01-01")
