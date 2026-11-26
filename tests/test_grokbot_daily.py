import datetime
import os
import sys
import tempfile
import unittest
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))
os.environ.setdefault("AGENTIC_SPEND_DATA_DIR", tempfile.mkdtemp(prefix="agentic-spend-test-"))
os.environ.setdefault("GROK_BOT_PERSISTENCE_DIR", tempfile.mkdtemp(prefix="grokbot-persist-test-"))

import grokbot_usage  # noqa: E402

LONDON = ZoneInfo("Europe/London")


def pt(at, pct, reset=None):
    row = {"at": at, "weeklyPct": pct}
    if reset:
        row["resetAt"] = reset
    return row


def day_map(result):
    return {d["date"]: d for d in result["days"]}


def hour_map(result):
    return {(h["date"], h["hour"]): h["usedPct"] for h in result["hours"]}


class DailyUsageTests(unittest.TestCase):
    def test_increases_on_one_day_from_first_reading(self):
        # Coverage starts mid-day: do not count the opening 10% from zero.
        history = [
            pt("2026-01-03T09:20:00+00:00", 10.0, "2026-01-05T14:30:00+00:00"),
            pt("2026-01-03T11:00:00+00:00", 12.5, "2026-01-05T14:30:00+00:00"),
        ]
        result = grokbot_usage.daily_usage_from_history(history, tz=LONDON)
        day = day_map(result)["2026-01-03"]
        self.assertAlmostEqual(day["usedPct"], 2.5)
        self.assertAlmostEqual(day["sinceFirstPct"], 2.5)
        self.assertFalse(day["coverageComplete"])
        self.assertTrue(day["coverageStart"].startswith("2026-01-03T09:20:00"))

    def test_uses_previous_day_as_baseline_for_full_coverage(self):
        history = [
            pt("2026-01-02T22:00:00+00:00", 10.0, "2026-01-05T14:30:00+00:00"),
            pt("2026-01-03T09:20:00+00:00", 11.0, "2026-01-05T14:30:00+00:00"),
            pt("2026-01-03T11:00:00+00:00", 12.5, "2026-01-05T14:30:00+00:00"),
        ]
        result = grokbot_usage.daily_usage_from_history(history, tz=LONDON)
        mon = day_map(result)["2026-01-02"]
        tue = day_map(result)["2026-01-03"]
        self.assertAlmostEqual(mon["usedPct"], 0.0)
        self.assertAlmostEqual(tue["usedPct"], 2.5)
        self.assertAlmostEqual(tue["sinceFirstPct"], 1.5)
        self.assertTrue(tue["coverageComplete"])

    def test_reset_counts_post_reset_reading_from_zero(self):
        reset = "2026-01-05T14:30:00+00:00"
        history = [
            pt("2026-01-05T09:00:00+00:00", 80.0, reset),
            pt("2026-01-05T10:00:00+00:00", 90.0, reset),
            pt("2026-01-05T15:00:00+00:00", 5.0, "2026-01-12T14:30:00+00:00"),
        ]
        result = grokbot_usage.daily_usage_from_history(history, tz=LONDON)
        day = day_map(result)["2026-01-05"]
        self.assertAlmostEqual(day["usedPct"], 15.0)  # +10 before reset, +5 after

    def test_passing_reset_without_a_stored_reset_on_old_points(self):
        history = [
            pt("2026-01-05T10:00:00+00:00", 90.0),
            pt("2026-01-05T15:00:00+00:00", 4.0),
        ]
        result = grokbot_usage.daily_usage_from_history(
            history, tz=LONDON, current_reset_at="2026-01-05T14:30:00+00:00"
        )
        self.assertAlmostEqual(day_map(result)["2026-01-05"]["usedPct"], 4.0)

    def test_drop_starts_a_new_window_and_counts_from_zero(self):
        history = [
            pt("2026-01-03T10:00:00+00:00", 40.0),
            pt("2026-01-03T16:00:00+00:00", 12.0),
        ]
        result = grokbot_usage.daily_usage_from_history(history, tz=LONDON)
        self.assertAlmostEqual(day_map(result)["2026-01-03"]["usedPct"], 12.0)

    def test_gap_attributes_the_increase_to_the_later_day(self):
        history = [
            pt("2026-01-05T10:00:00+00:00", 10.0, "2026-01-12T14:30:00+00:00"),
            pt("2026-01-08T10:00:00+00:00", 15.0, "2026-01-12T14:30:00+00:00"),
        ]
        result = grokbot_usage.daily_usage_from_history(history, tz=LONDON)
        days = day_map(result)
        self.assertAlmostEqual(days["2026-01-05"]["usedPct"], 0.0)
        self.assertAlmostEqual(days["2026-01-08"]["usedPct"], 5.0)

    def test_empty_history(self):
        result = grokbot_usage.daily_usage_from_history([], tz=LONDON)
        self.assertEqual(result["days"], [])
        self.assertEqual(result["hours"], [])

    def test_deduped_history_does_not_invent_usage(self):
        now = datetime.datetime(2026, 1, 3, 12, 0, tzinfo=datetime.timezone.utc)
        history = grokbot_usage.append_history([], 12.5, "2026-01-03T10:00:00+00:00", now=now, reset_at="2026-01-05T14:30:00+00:00")
        history = grokbot_usage.append_history(history, 12.5, "2026-01-03T10:20:00+00:00", now=now, reset_at="2026-01-05T14:30:00+00:00")
        self.assertEqual(len(history), 1)
        history = grokbot_usage.append_history(history, 13.0, "2026-01-03T10:40:00+00:00", now=now, reset_at="2026-01-05T14:30:00+00:00")
        result = grokbot_usage.daily_usage_from_history(history, tz=LONDON)
        self.assertAlmostEqual(day_map(result)["2026-01-03"]["usedPct"], 0.5)
        self.assertEqual(history[0].get("resetAt"), "2026-01-05T14:30:00+00:00")

    def test_hourly_buckets_use_the_later_reading(self):
        history = [
            pt("2026-01-03T09:20:00+00:00", 10.0, "2026-01-05T14:30:00+00:00"),
            pt("2026-01-03T09:50:00+00:00", 10.4, "2026-01-05T14:30:00+00:00"),
            pt("2026-01-03T11:05:00+00:00", 12.5, "2026-01-05T14:30:00+00:00"),
        ]
        result = grokbot_usage.daily_usage_from_history(history, tz=LONDON)
        hours = hour_map(result)
        # 09:20 and 09:50 are 09:20/09:50 UTC = 09:20/09:50 GMT in January
        self.assertAlmostEqual(hours[("2026-01-03", 9)], 0.4)
        self.assertAlmostEqual(hours[("2026-01-03", 11)], 2.1)

    def test_dst_autumn_fallback_stays_on_the_same_local_day(self):
        # 25 Oct 2026, London clocks go back at 02:00 BST to 01:00 GMT.
        # 00:30 BST = 2026-10-24 23:30 UTC, still local 25 Oct.
        # 01:30 GMT after the fallback = 2026-10-25 01:30 UTC, still 25 Oct.
        history = [
            pt("2026-10-24T23:30:00+00:00", 20.0, "2026-10-26T00:00:00+00:00"),
            pt("2026-10-25T01:30:00+00:00", 21.5, "2026-10-26T00:00:00+00:00"),
        ]
        result = grokbot_usage.daily_usage_from_history(history, tz=LONDON)
        days = day_map(result)
        self.assertIn("2026-10-25", days)
        self.assertNotIn("2026-10-24", days)
        self.assertAlmostEqual(days["2026-10-25"]["usedPct"], 1.5)
        self.assertFalse(days["2026-10-25"]["coverageComplete"])

    def test_dst_spring_forward_splits_local_days_correctly(self):
        # 29 Mar 2026, London jumps from 01:00 GMT to 02:00 BST.
        # 2026-03-28 23:30 GMT is still 28 Mar local; 02:30 BST is 29 Mar 01:30 UTC.
        history = [
            pt("2026-03-28T23:30:00+00:00", 8.0, "2026-04-01T00:00:00+00:00"),
            pt("2026-03-29T01:30:00+00:00", 9.0, "2026-04-01T00:00:00+00:00"),
        ]
        result = grokbot_usage.daily_usage_from_history(history, tz=LONDON)
        days = day_map(result)
        self.assertAlmostEqual(days["2026-03-28"]["usedPct"], 0.0)
        self.assertAlmostEqual(days["2026-03-29"]["usedPct"], 1.0)
        self.assertTrue(days["2026-03-29"]["coverageComplete"])

    def test_collect_stamps_history_with_sample_time_not_cache_read_at(self):
        import json
        from tests.test_grokbot_usage import FAKE_ACCOUNT_A, _cache_payload, _write_blob

        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as data:
            _write_blob(
                directory,
                f"sand.client.slice.account.{FAKE_ACCOUNT_A}.weekly-usage.cache",
                _cache_payload(),
            )
            store = os.path.join(data, "grokbot-usage.json")
            now = datetime.datetime(2026, 1, 3, 12, 45, tzinfo=datetime.timezone.utc)
            report = grokbot_usage.collect(directory=directory, store_path=store, now=now, tz=LONDON)
            self.assertEqual(report["accounts"][0]["history"][0]["at"], "2026-01-03T12:45:00+00:00")
            # Cache readAt is 09:00, weeklyPct is 12.5; this is the first
            # sample, so usedPct is 0 and coverage starts at the sample.
            days = {d["date"]: d for d in report["accounts"][0]["dailyUsage"]}
            self.assertAlmostEqual(days["2026-01-03"]["usedPct"], 0.0)
            later = datetime.datetime(2026, 1, 3, 13, 20, tzinfo=datetime.timezone.utc)
            # Same cache reading 35 minutes later is past the dedupe window.
            report2 = grokbot_usage.collect(directory=directory, store_path=store, now=later, tz=LONDON)
            self.assertEqual(len(report2["accounts"][0]["history"]), 2)
            self.assertEqual([p["weeklyPct"] for p in report2["accounts"][0]["history"]], [12.5, 12.5])


if __name__ == "__main__":
    unittest.main()
