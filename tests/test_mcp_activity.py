"""The MCP activity log (mcp_activity.py): every MCP call agents make, bounded, never
committed, and readable per server - so a database server failing with
`read ECONNRESET` shows on the Systems page instead of being invisible for minutes."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pm_studio import gitsnapshot, mcp_activity, scaffold
from pm_studio import server as server_module
from pm_studio.mcp_activity import McpActivityLog, make_entry, result_summary


def _entry(server: str, ok: bool, ts: float, outcome: str = "ok") -> dict:
    return make_entry(
        session="s1", source="task", task_id="t1", server=server, tool="query",
        args="select 1", ok=ok, outcome=outcome, duration_ms=12, ts=ts,
    )


class ActivityLogTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "ws" / "mcp-activity.jsonl"

    def test_append_and_report_counters(self) -> None:
        log = McpActivityLog(self.path)
        log.record(_entry("dev-db", True, 1.0, "3 rows"))
        log.record(_entry("browser", True, 2.0))
        for ts in (3.0, 4.0, 5.0):
            log.record(_entry("dev-db", False, ts, "read ECONNRESET"))
        report = log.report()
        self.assertEqual([e["ts"] for e in report["entries"]], [5.0, 4.0, 3.0, 2.0, 1.0])
        db = report["servers"]["dev-db"]
        self.assertEqual(
            (db["calls"], db["errors"], db["consecutive_errors"], db["last_ok_at"], db["last_ok"]),
            (4, 3, 3, 1.0, False),
        )
        self.assertEqual(db["last_error"], {"ts": 5.0, "tool": "query", "outcome": "read ECONNRESET"})
        self.assertEqual(report["servers"]["browser"]["consecutive_errors"], 0)
        only = log.report(server="browser", limit=10)
        self.assertEqual([e["server"] for e in only["entries"]], ["browser"])
        self.assertEqual(len(log.report(limit=2)["entries"]), 2)
        # A call that works again resets the streak.
        log.record(_entry("dev-db", True, 6.0))
        self.assertEqual(log.report()["servers"]["dev-db"]["consecutive_errors"], 0)

    def test_bounded_with_cheap_trims(self) -> None:
        log = McpActivityLog(self.path, max_entries=10, slack=5)
        for i in range(16):
            log.record(_entry("dev-db", True, float(i)))
        lines = self.path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 10)  # trimmed at 16 > 10 + 5
        self.assertEqual(json.loads(lines[-1])["ts"], 15.0)
        for i in range(16, 20):
            log.record(_entry("dev-db", True, float(i)))
        self.assertEqual(len(self.path.read_text(encoding="utf-8").splitlines()), 14)  # appends only
        self.assertFalse(self.path.with_name(self.path.name + ".tmp").exists())
        # A fresh instance (server restart) counts what is there before appending.
        again = McpActivityLog(self.path, max_entries=10, slack=5)
        again.record(_entry("dev-db", True, 20.0))
        again.record(_entry("dev-db", True, 21.0))
        self.assertEqual(len(self.path.read_text(encoding="utf-8").splitlines()), 10)

    def test_broken_lines_are_skipped(self) -> None:
        self.path.parent.mkdir(parents=True)
        self.path.write_text('{"server": "a", "ok": true, "ts": 1}\nnot json\n', encoding="utf-8")
        self.assertEqual(len(McpActivityLog(self.path).entries()), 1)

    def test_never_committed(self) -> None:
        self.assertIn("mcp-activity.jsonl", gitsnapshot.SENSITIVE_WORKSPACE_FILES)
        self.assertIn("{workspace_rel}/mcp-activity.jsonl", scaffold.GITIGNORE_ENTRIES)
        self.assertEqual(mcp_activity.ACTIVITY_LOG.path.name, "mcp-activity.jsonl")


class ResultSummaryTest(unittest.TestCase):
    def test_shapes_only(self) -> None:
        self.assertEqual(result_summary([{"type": "text", "text": '[{"name": "Jane"}]'}]), "1 row")
        self.assertEqual(result_summary('{"rows": [1, 2, 3], "rowCount": 3}'), "3 rows")
        self.assertEqual(result_summary([{"type": "text", "text": "[]"}]), "0 rows")
        self.assertEqual(result_summary([{"type": "text", "text": "Navigated to the page. Title: Patient Jane"}]), "ok")
        self.assertEqual(result_summary([{"type": "image", "data": "x"}, {"type": "image", "data": "y"}]), "2 images")
        self.assertEqual(result_summary(None), "ok")


class ActivityRouteTest(unittest.TestCase):
    def test_route_reads_the_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = McpActivityLog(Path(tmp) / "mcp-activity.jsonl")
            log.record(_entry("dev-db", False, 1.0, "read ECONNRESET"))
            log.record(_entry("browser", True, 2.0))
            with mock.patch.object(mcp_activity, "ACTIVITY_LOG", log):
                everything = server_module.agents_mcp_activity(mock.Mock(), server="", limit=50)
                db = server_module.agents_mcp_activity(mock.Mock(), server="dev-db", limit=50)
        self.assertEqual(len(everything["entries"]), 2)
        self.assertEqual([e["server"] for e in db["entries"]], ["dev-db"])
        self.assertEqual(db["servers"]["dev-db"]["last_error"]["outcome"], "read ECONNRESET")


if __name__ == "__main__":
    unittest.main()
