"""Tests for the overview rollup: one read-only payload answering "what are we
working on?".

The builder is pure - groups and workload rows are passed in - so these tests hand it
shapes directly. What matters: counts are conserved from the groups it is given, health
is derived from the same facts the row shows, the shipped feed honors its window, and
overdue work states its lateness in days rather than leaving the reader to diff dates.
"""

import unittest
from datetime import date, timedelta

from pm_studio.overview import build_overview

NOW = 1_760_000_000.0  # an arbitrary fixed instant
DAY = 86400.0


def change(change_id, status="pending", *, product="web", shipped_at=None,
           target_at=None, is_overdue=False, assignee=None, bucket=None):
    return {
        "id": change_id,
        "product": product,
        "title": f"Change {change_id}",
        "status": status,
        "shipped_at": shipped_at,
        "target_at": target_at,
        "is_overdue": is_overdue,
        "assigned": {"name": assignee} if assignee else None,
        "bucket": bucket,
    }


def group(title, changes, *, initiative_id="i1", status="open", in_ideation=False,
          is_maintenance=False):
    initiative = None
    if title is not None:
        initiative = {
            "id": initiative_id, "title": title, "status": status,
            "in_ideation": in_ideation, "is_maintenance": is_maintenance,
        }
    return {"initiative": initiative, "projects": [{"project": None, "changes": changes}]}


def build(groups, workload=(), now=NOW, **kwargs):
    return build_overview(groups, list(workload), now=now, **kwargs)


class RollupTest(unittest.TestCase):
    def test_counts_are_conserved(self) -> None:
        data = build([group("A", [
            change("a"), change("b", "in_progress"), change("c", "done", shipped_at=NOW - DAY),
        ])])
        row = data["initiatives"][0]
        self.assertEqual(row["counts"], {"total": 3, "pending": 1, "in_progress": 1, "done": 1})
        self.assertEqual(row["open"], 2)
        self.assertEqual(row["pct_done"], 33)

    def test_open_now_next_shares_the_boards_working_test(self) -> None:
        # Open changes staged in Now or Next count; Later, unstaged, and done work
        # never does - the Overview tile and the board's "Working" state must agree.
        data = build([group("A", [
            change("a", "in_progress", bucket="now"),
            change("b", "pending", bucket="next"),
            change("c", "pending", bucket="later"),
            change("d", "pending"),
            change("e", "done", bucket="now", shipped_at=NOW - DAY),
        ])])
        row = data["initiatives"][0]
        self.assertEqual(row["open_now_next"], 2)
        self.assertEqual(row["open"], 4)

    def test_open_now_next_counts_maintenance_too(self) -> None:
        data = build([group("Upkeep", [change("a", "in_progress", bucket="now")],
                            is_maintenance=True)])
        self.assertEqual(data["initiatives"][0]["open_now_next"], 1)

    def test_products_and_people_come_from_open_changes(self) -> None:
        data = build([group("A", [
            change("a", "in_progress", product="web", assignee="Ada"),
            change("b", product="app", assignee="Ada"),
            change("c", product="app", assignee="Bo"),
            change("d", "done", product="api", shipped_at=NOW - DAY, assignee="Cy"),
        ])])
        row = data["initiatives"][0]
        self.assertEqual(row["people"], [{"name": "Ada", "open": 2}, {"name": "Bo", "open": 1}])
        # products count every change; people count only open ones (Cy shipped, has none open)
        self.assertEqual(set(row["products"]), {"web", "app", "api"})

    def test_next_target_is_the_nearest_non_overdue_open_date(self) -> None:
        data = build([group("A", [
            change("a", target_at="2030-05-01"),
            change("b", target_at="2030-01-01"),
            change("c", "done", target_at="2029-01-01", shipped_at=NOW - DAY),
            change("d", target_at="2020-01-01", is_overdue=True),  # late, not "next"
        ])])
        self.assertEqual(data["initiatives"][0]["next_target"], "2030-01-01")


class HealthTest(unittest.TestCase):
    def test_overdue_work_means_at_risk(self) -> None:
        data = build([group("A", [
            change("a", "in_progress"),
            change("b", target_at="2020-01-01", is_overdue=True),
        ])])
        row = data["initiatives"][0]
        self.assertEqual(row["health"], "at_risk")
        self.assertEqual(row["overdue"], 1)

    def test_moving_work_is_on_track(self) -> None:
        data = build([group("A", [change("a", "in_progress"), change("b")])])
        self.assertEqual(data["initiatives"][0]["health"], "on_track")

    def test_open_work_with_no_motion_is_quiet(self) -> None:
        data = build([group("A", [change("a"), change("b")])])
        self.assertEqual(data["initiatives"][0]["health"], "quiet")

    def test_recent_ship_keeps_pending_only_work_on_track(self) -> None:
        data = build([group("A", [
            change("a"), change("b", "done", shipped_at=NOW - 2 * DAY),
        ])])
        self.assertEqual(data["initiatives"][0]["health"], "on_track")

    def test_ideation_and_closed_override_risk(self) -> None:
        idea = group("I", [change("a")], in_ideation=True)
        closed = group("C", [change("b", "done", shipped_at=NOW - DAY)],
                       initiative_id="i2", status="closed")
        data = build([idea, closed])
        healths = {row["title"]: row["health"] for row in data["initiatives"]}
        self.assertEqual(healths["I"], "ideation")
        self.assertEqual(healths["C"], "closed")

    def test_everything_shipped_is_done(self) -> None:
        data = build([group("A", [change("a", "done", shipped_at=NOW - DAY)])])
        self.assertEqual(data["initiatives"][0]["health"], "done")


class OrderingTest(unittest.TestCase):
    def test_most_active_first_then_quiet_then_ideation_then_closed(self) -> None:
        rows = build([
            group("Closed", [change("z", "done", shipped_at=NOW - DAY)],
                  initiative_id="i4", status="closed"),
            group("Idea", [change("i")], initiative_id="i3", in_ideation=True),
            group("Quiet", [change("q")], initiative_id="i2"),
            group("Busy", [change("b1", "in_progress"), change("b2", "in_progress")],
                  initiative_id="i1"),
        ])["initiatives"]
        self.assertEqual([r["title"] for r in rows], ["Busy", "Quiet", "Idea", "Closed"])

    def test_unaligned_and_maintenance_trail_open_initiatives_whatever_their_size(self) -> None:
        rows = build([
            group(None, [change(f"u{i}", "in_progress") for i in range(20)]),
            group("Maint", [change(f"m{i}", "in_progress") for i in range(15)],
                  initiative_id="i9", is_maintenance=True),
            group("Small", [change("s1", "in_progress")], initiative_id="i1"),
        ])["initiatives"]
        self.assertEqual([r["title"] for r in rows], ["Small", "Maint", None])

    def test_rows_with_neither_open_nor_recent_work_are_dropped(self) -> None:
        """The table's promise is "open or recently shipped": declared-but-empty
        ideation and long-finished initiatives are the portfolio page's business."""
        empty_idea = group("Empty idea", [], initiative_id="i5", in_ideation=True)
        stale_done = group("Old glory", [change("a", "done", shipped_at=NOW - 40 * DAY)],
                           initiative_id="i6")
        fresh_done = group("Just finished", [change("b", "done", shipped_at=NOW - DAY)],
                           initiative_id="i7")
        rows = build([empty_idea, stale_done, fresh_done])["initiatives"]
        self.assertEqual([r["title"] for r in rows], ["Just finished"])

    def test_closed_initiative_with_no_recent_ship_is_dropped(self) -> None:
        old = group("Old", [change("a", "done", shipped_at=NOW - 400 * DAY)],
                    status="closed")
        data = build([old])
        self.assertEqual(data["initiatives"], [])

    def test_empty_unaligned_group_is_dropped_but_kept_when_it_holds_work(self) -> None:
        self.assertEqual(build([group(None, [])])["initiatives"], [])
        rows = build([group(None, [change("a")])])["initiatives"]
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["id"])


class ShippedFeedTest(unittest.TestCase):
    def test_window_and_order_and_days_ago(self) -> None:
        data = build([group("A", [
            change("old", "done", shipped_at=NOW - 40 * DAY),
            change("new", "done", shipped_at=NOW - 1 * DAY, assignee="Ada"),
            change("mid", "done", shipped_at=NOW - 10 * DAY),
        ])])
        feed = data["shipped"]
        self.assertEqual([entry["id"] for entry in feed], ["new", "mid"])
        self.assertEqual(feed[0]["days_ago"], 1)
        self.assertEqual(feed[0]["assignee"], "Ada")
        self.assertEqual(feed[0]["initiative"], "A")
        self.assertEqual(data["shipped_total"], 2)

    def test_done_without_shipped_at_stays_out_of_the_feed(self) -> None:
        data = build([group("A", [change("a", "done")])])
        self.assertEqual(data["shipped"], [])


class OverdueFeedTest(unittest.TestCase):
    def test_days_late_is_spelled_out_and_most_late_leads(self) -> None:
        today = date.fromtimestamp(NOW)
        d3 = (today - timedelta(days=3)).isoformat()
        d9 = (today - timedelta(days=9)).isoformat()
        data = build([group("A", [
            change("a", target_at=d3, is_overdue=True, assignee="Bo"),
            change("b", "in_progress", target_at=d9, is_overdue=True),
        ])])
        feed = data["overdue"]
        self.assertEqual([entry["id"] for entry in feed], ["b", "a"])
        self.assertEqual(feed[0]["days_late"], 9)
        self.assertEqual(feed[1]["days_late"], 3)
        self.assertEqual(data["overdue_total"], 2)


class PlanFeedsTest(unittest.TestCase):
    """The "working now vs. up next" halves: in-progress work is one list, next-bucket
    pending work the other, dated commitments leading in both."""

    def test_split_and_order(self) -> None:
        changes = [
            change("w2", "in_progress"),
            change("w1", "in_progress", target_at="2030-01-01"),
            change("n2"), change("n1", target_at="2030-02-01"),
            change("skip_later"), change("d", "done", shipped_at=NOW - DAY),
        ]
        changes[2]["bucket"] = "next"
        changes[3]["bucket"] = "next"
        changes[4]["bucket"] = "later"
        data = build([group("A", changes)])
        self.assertEqual([e["id"] for e in data["working"]], ["w1", "w2"])
        self.assertEqual([e["id"] for e in data["next_up"]], ["n1", "n2"])
        self.assertEqual(data["working_total"], 2)
        self.assertEqual(data["next_total"], 2)

    def test_undated_work_falls_back_to_recency_not_alphabet(self) -> None:
        stale = change("stale", "in_progress")
        stale["updated_at"] = NOW - 10 * DAY
        stale["title"] = "AAA ancient"
        fresh = change("fresh", "in_progress")
        fresh["updated_at"] = NOW - DAY
        fresh["title"] = "ZZZ recent"
        dated = change("dated", "in_progress", target_at="2030-01-01")
        data = build([group("A", [stale, fresh, dated])])
        self.assertEqual([e["id"] for e in data["working"]], ["dated", "fresh", "stale"])

    def test_in_progress_next_bucket_counts_as_working_not_next(self) -> None:
        item = change("x", "in_progress")
        item["bucket"] = "next"
        data = build([group("A", [item])])
        self.assertEqual([e["id"] for e in data["working"]], ["x"])
        self.assertEqual(data["next_up"], [])


class IdleDaysTest(unittest.TestCase):
    def test_open_entries_carry_their_idle_age(self) -> None:
        fresh = change("fresh", "in_progress")
        fresh["updated_at"] = NOW - DAY
        stale = change("stale", "in_progress")
        stale["updated_at"] = NOW - 12 * DAY
        data = build([group("A", [fresh, stale])])
        by_id = {e["id"]: e for e in data["working"]}
        self.assertEqual(by_id["fresh"]["idle_days"], 1)
        self.assertEqual(by_id["stale"]["idle_days"], 12)


class StaleFeedTest(unittest.TestCase):
    def test_stale_list_is_the_working_feeds_mirror(self) -> None:
        fresh = change("fresh", "in_progress")
        fresh["updated_at"] = NOW - DAY
        stale1 = change("stale1", "in_progress")
        stale1["updated_at"] = NOW - 9 * DAY
        stale2 = change("stale2", "in_progress")
        stale2["updated_at"] = NOW - 20 * DAY
        pending_old = change("old_pending")
        pending_old["updated_at"] = NOW - 30 * DAY  # pending: aging quietly is normal
        data = build([group("A", [fresh, stale1, stale2, pending_old])])
        self.assertEqual([e["id"] for e in data["stale"]], ["stale2", "stale1"])
        self.assertEqual(data["stale_total"], 2)


class ExternalCountTest(unittest.TestCase):
    def test_open_external_work_is_counted(self) -> None:
        owned = change("ext")
        owned["owner"] = "Acme Analytics"
        shipped_ext = change("done_ext", "done", shipped_at=NOW - DAY)
        shipped_ext["owner"] = "Acme Analytics"
        data = build([group("A", [owned, shipped_ext, change("plain")])])
        self.assertEqual(data["external_open"], 1)  # shipped external is history


class LoadTest(unittest.TestCase):
    def test_workload_rows_are_trimmed_not_recomputed(self) -> None:
        rows = [{
            "person_id": "p1", "name": "Ada", "status": "active", "open": 3,
            "in_progress": 1, "overdue": 0, "shipped": 2, "now": 2, "next": 1,
            "later": 0, "products": [], "systems": [], "areas": [],
        }]
        data = build([], workload=rows)
        self.assertEqual(data["load"], [{
            "person_id": "p1", "name": "Ada", "open": 3, "in_progress": 1,
            "overdue": 0, "now": 2, "shipped": 2,
        }])
        self.assertEqual(data["load_quiet_people"], 0)

    def test_shipped_only_people_fold_into_a_count(self) -> None:
        rows = [
            {"person_id": "p1", "name": "Ada", "open": 2, "in_progress": 1,
             "overdue": 0, "shipped": 0, "now": 1, "next": 1, "later": 0,
             "products": [], "systems": [], "areas": []},
            {"person_id": "p2", "name": "Eve", "open": 0, "in_progress": 0,
             "overdue": 0, "shipped": 5, "now": 0, "next": 0, "later": 0,
             "products": [], "systems": [], "areas": []},
        ]
        data = build([], workload=rows)
        self.assertEqual([r["name"] for r in data["load"]], ["Ada"])
        self.assertEqual(data["load_quiet_people"], 1)


if __name__ == "__main__":
    unittest.main()
