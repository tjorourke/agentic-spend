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

UTC = datetime.timezone.utc
LONDON = ZoneInfo("Europe/London")


def pt(at, pct, reset=None):
    row = {"at": at, "weeklyPct": pct}
    if reset:
        row["resetAt"] = reset
    return row


class DownsampleTests(unittest.TestCase):
    def test_keeps_full_resolution_inside_14_days(self):
        now = datetime.datetime(2026, 3, 20, 12, 0, tzinfo=UTC)
        # 16 points over two recent days, 90 minutes apart, changing values
        # so the 30-minute same-value dedupe would not apply.
        history = []
        start = datetime.datetime(2026, 3, 18, 0, 0, tzinfo=UTC)
        for i in range(16):
            at = start + datetime.timedelta(minutes=90 * i)
            history.append(pt(at.isoformat(), 10.0 + i * 0.1, "2026-03-23T12:00:00+00:00"))
        out = grokbot_usage.downsample_history(history, now=now, current_reset_at="2026-03-23T12:00:00+00:00")
        self.assertEqual(len(out), 16)

    def test_old_dense_hour_keeps_one_reading_plus_window_last(self):
        now = datetime.datetime(2026, 3, 20, 12, 0, tzinfo=UTC)
        reset = "2026-02-23T14:30:00+00:00"
        # Well outside the 14-day full-resolution window.
        history = [
            pt("2026-02-20T10:05:00+00:00", 20.0, reset),
            pt("2026-02-20T10:20:00+00:00", 21.0, reset),
            pt("2026-02-20T10:40:00+00:00", 22.0, reset),
            pt("2026-02-20T11:10:00+00:00", 23.0, reset),
            # last reading of the window, same hour as a later post-reset point
            pt("2026-02-23T14:15:00+00:00", 40.0, reset),
            pt("2026-02-23T14:45:00+00:00", 3.0, "2026-03-02T14:30:00+00:00"),
        ]
        out = grokbot_usage.downsample_history(history, now=now, current_reset_at="2026-03-02T14:30:00+00:00")
        hours = [p["at"][11:13] + p["at"][0:10] + p["at"] for p in out]
        ats = [p["at"] for p in out]
        # Hour 10 keeps the last of that hour (10:40), not all three.
        self.assertNotIn("2026-02-20T10:05:00+00:00", ats)
        self.assertNotIn("2026-02-20T10:20:00+00:00", ats)
        self.assertIn("2026-02-20T10:40:00+00:00", ats)
        self.assertIn("2026-02-20T11:10:00+00:00", ats)
        # Last reading before reset is kept even though 14:45 is last in that hour.
        self.assertIn("2026-02-23T14:15:00+00:00", ats)
        self.assertIn("2026-02-23T14:45:00+00:00", ats)
        self.assertEqual(len(out), 4)

    def test_drops_points_older_than_400_days(self):
        now = datetime.datetime(2026, 3, 20, tzinfo=UTC)
        history = [
            pt("2024-12-01T00:00:00+00:00", 1.0),
            pt("2025-06-01T00:00:00+00:00", 2.0),
        ]
        out = grokbot_usage.downsample_history(history, now=now)
        self.assertEqual([p["weeklyPct"] for p in out], [2.0])


class WindowSummaryTests(unittest.TestCase):
    def test_peak_first_last_and_partial_current_window(self):
        now = datetime.datetime(2026, 1, 8, 12, 0, tzinfo=UTC)
        reset = "2026-01-12T14:30:00+00:00"
        history = [
            pt("2026-01-07T09:21:00+00:00", 10.0, reset),
            pt("2026-01-07T15:00:00+00:00", 12.5, reset),
            pt("2026-01-08T11:00:00+00:00", 12.4, reset),
        ]
        # Peak is 12.5 even though the last reading is slightly lower.
        rows = grokbot_usage.window_summaries_from_history(history, current_reset_at=reset, now=now)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["resetAt"], reset)
        self.assertAlmostEqual(row["peakPct"], 12.5)
        self.assertTrue(row["firstReadingAt"].startswith("2026-01-07T09:21:00"))
        self.assertTrue(row["lastReadingAt"].startswith("2026-01-08T11:00:00"))
        self.assertTrue(row["current"])
        self.assertTrue(row["partialCoverage"])

    def test_completed_window_is_not_current_and_can_be_complete(self):
        now = datetime.datetime(2026, 1, 15, tzinfo=UTC)
        reset = "2026-01-12T14:30:00+00:00"
        # First reading shortly after the implied window start (reset minus 7 days).
        history = [
            pt("2026-01-05T14:40:00+00:00", 1.0, reset),
            pt("2026-01-12T14:00:00+00:00", 44.0, reset),
            pt("2026-01-12T15:00:00+00:00", 2.0, "2026-01-19T14:30:00+00:00"),
        ]
        rows = grokbot_usage.window_summaries_from_history(history, current_reset_at="2026-01-19T14:30:00+00:00", now=now)
        self.assertEqual(len(rows), 2)
        first, second = rows
        self.assertEqual(first["resetAt"], reset)
        self.assertAlmostEqual(first["peakPct"], 44.0)
        self.assertFalse(first["current"])
        self.assertFalse(first["partialCoverage"])
        self.assertTrue(second["current"])
        self.assertAlmostEqual(second["peakPct"], 2.0)

    def test_summary_keeps_a_peak_that_downsample_drops(self):
        now = datetime.datetime(2026, 3, 20, 12, 0, tzinfo=UTC)
        reset = "2026-02-23T14:30:00+00:00"
        history = [
            pt("2026-02-20T10:05:00+00:00", 41.0, reset),  # peak, dropped as not last in the hour
            pt("2026-02-20T10:40:00+00:00", 30.0, reset),
            pt("2026-02-23T14:15:00+00:00", 35.0, reset),
        ]
        summaries = grokbot_usage.window_summaries_from_history(history, current_reset_at=reset, now=now)
        self.assertAlmostEqual(summaries[0]["peakPct"], 41.0)
        pruned = grokbot_usage.downsample_history(history, now=now, current_reset_at=reset)
        self.assertNotIn("2026-02-20T10:05:00+00:00", [p["at"] for p in pruned])
        merged = grokbot_usage.merge_window_summaries(summaries, pruned, current_reset_at=reset, now=now)
        self.assertAlmostEqual(merged[0]["peakPct"], 41.0)
        self.assertTrue(merged[0]["firstReadingAt"].startswith("2026-02-20T10:05:00"))

    def test_existing_summary_peak_is_not_lowered(self):
        now = datetime.datetime(2026, 1, 10, tzinfo=UTC)
        reset = "2026-01-12T14:30:00+00:00"
        existing = [{
            "resetAt": reset,
            "peakPct": 42.5,
            "firstReadingAt": "2026-01-07T09:21:00+00:00",
            "lastReadingAt": "2026-01-08T11:00:00+00:00",
        }]
        history = [pt("2026-01-09T10:00:00+00:00", 40.0, reset)]
        merged = grokbot_usage.merge_window_summaries(existing, history, current_reset_at=reset, now=now)
        self.assertAlmostEqual(merged[0]["peakPct"], 42.5)
        self.assertTrue(merged[0]["firstReadingAt"].startswith("2026-01-07T09:21:00"))
        self.assertTrue(merged[0]["lastReadingAt"].startswith("2026-01-09T10:00:00"))
        self.assertTrue(merged[0]["current"])


if __name__ == "__main__":
    unittest.main()
