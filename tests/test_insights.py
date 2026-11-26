import datetime
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))
os.environ.setdefault("AGENTIC_SPEND_DATA_DIR", tempfile.mkdtemp(prefix="agentic-spend-test-"))

import insights  # noqa: E402

UTC = datetime.timezone.utc
WEEK = insights.WEEK_SECONDS


def at(day, hour=0, minute=0):
    return datetime.datetime(2026, 11, day, hour, minute, tzinfo=UTC)


class ParseTimeTests(unittest.TestCase):
    def test_formats(self):
        self.assertEqual(insights.parse_time("2026-11-29T09:06:55Z"), datetime.datetime(2026, 11, 29, 9, 6, 55, tzinfo=UTC))
        self.assertEqual(insights.parse_time(1795791600), datetime.datetime.fromtimestamp(1795791600, tz=UTC))
        self.assertEqual(insights.parse_time(1795791600000), datetime.datetime.fromtimestamp(1795791600, tz=UTC))
        self.assertIsNotNone(insights.parse_time("2026-11-10T18:19:39.1Z"))
        self.assertIsNone(insights.parse_time("not a date"))
        self.assertIsNone(insights.parse_time(None))


class ForecastTests(unittest.TestCase):
    def test_window_basis_when_little_coverage(self):
        # Window started 22 Nov 09:00, resets 29 Nov 09:00. 4 days in at 40%.
        reset = at(29, 9)
        fc = insights.forecast([(at(26, 9), 40.0)], reset, WEEK, now=at(26, 9, 5))
        self.assertEqual(fc["basis"], "window")
        self.assertEqual(fc["confidence"], "low")
        # 10% a day for the 3 remaining days.
        self.assertAlmostEqual(fc["projectedPct"], 70.0, places=1)
        self.assertIsNone(fc["hitsLimitAt"])
        self.assertAlmostEqual(fc["ratePerDay"], 10.0, places=1)

    def test_readings_basis_and_hits_limit(self):
        reset = at(29, 9)
        points = [(at(24, 9), 20.0), (at(25, 9), 40.0), (at(26, 9), 60.0)]
        fc = insights.forecast(points, reset, WEEK, now=at(26, 9))
        self.assertEqual(fc["basis"], "readings")
        self.assertEqual(fc["confidence"], "normal")
        # 20% a day for 3 days from 60%.
        self.assertAlmostEqual(fc["projectedPct"], 120.0, places=1)
        self.assertEqual(fc["hitsLimitAt"], "2026-11-28T09:00:00+00:00")

    def test_ignores_readings_from_previous_window(self):
        reset = at(29, 9)
        points = [(at(20, 9), 95.0), (at(26, 9), 40.0)]
        fc = insights.forecast(points, reset, WEEK, now=at(26, 9))
        self.assertEqual(fc["coverageStart"], "2026-11-26T09:00:00+00:00")
        self.assertEqual(fc["basis"], "window")

    def test_already_at_limit(self):
        fc = insights.forecast([(at(26, 9), 100.0)], at(29, 9), WEEK, now=at(26, 9))
        self.assertEqual(fc["hitsLimitAt"], "2026-11-26T09:00:00+00:00")

    def test_none_cases(self):
        self.assertIsNone(insights.forecast([], at(29, 9), WEEK, now=at(26, 9)))
        self.assertIsNone(insights.forecast([(at(26, 9), 40)], None, WEEK, now=at(26, 9)))
        self.assertIsNone(insights.forecast([(at(26, 9), 40)], at(25, 9), WEEK, now=at(26, 9)))

    def test_flat_readings_do_not_go_negative(self):
        reset = at(29, 9)
        points = [(at(24, 9), 50.0), (at(26, 9), 50.0)]
        fc = insights.forecast(points, reset, WEEK, now=at(26, 9))
        self.assertEqual(fc["projectedPct"], 50.0)


class CompareTests(unittest.TestCase):
    def test_window_peaks_merge_jittered_resets(self):
        samples = [
            (at(10, 1), 3.0, "2026-12-10T15:40:46+00:00"),
            (at(10, 2), 4.0, "2026-12-10T15:40:47+00:00"),
            (at(1, 2), 70.0, "2026-11-10T15:40:47+00:00"),
        ]
        peaks = insights.window_peaks(samples)
        self.assertEqual(len(peaks), 2)
        self.assertEqual(peaks[1]["peakPct"], 4.0)
        self.assertEqual(peaks[1]["count"], 2)

    def test_compare_with_previous_window(self):
        peaks = [{"resetAt": "2026-11-22T09:00:00+00:00", "peakPct": 70},
                 {"resetAt": "2026-11-29T09:00:00+00:00", "peakPct": 40}]
        cmp_ = insights.compare_windows(peaks, at(29, 9), WEEK, current_pct=44)
        self.assertEqual(cmp_["currentPeakPct"], 44.0)
        self.assertEqual(cmp_["previousPeakPct"], 70.0)
        self.assertEqual(cmp_["deltaPct"], -26.0)

    def test_compare_hidden_without_previous_window(self):
        peaks = [{"resetAt": "2026-11-29T09:00:00+00:00", "peakPct": 40}]
        self.assertIsNone(insights.compare_windows(peaks, at(29, 9), WEEK, current_pct=44))
        # A window two weeks back is a gap, not "last week".
        peaks.append({"resetAt": "2026-11-15T09:00:00+00:00", "peakPct": 90})
        self.assertIsNone(insights.compare_windows(peaks, at(29, 9), WEEK, current_pct=44))

    def test_pct_change(self):
        self.assertEqual(insights.pct_change(112, 100), 12.0)
        self.assertEqual(insights.pct_change(50, 100), -50.0)
        self.assertIsNone(insights.pct_change(10, 0))
        self.assertIsNone(insights.pct_change(10, None))


class BuildPlanInsightsTests(unittest.TestCase):
    def test_all_three_sources(self):
        now = at(26, 11)
        grokbot = {"accounts": [{
            "accountId": "acct-1", "plan": "Super", "weeklyPct": 44.0,
            "resetAt": "2026-11-29T09:00:00+00:00", "updatedAt": "2026-11-26T11:00:00+00:00",
            "creditsUsd": 10.6,
            "history": [{"at": "2026-11-26T09:00:00+00:00", "weeklyPct": 40.0}],
            "windows": [{"resetAt": "2026-11-22T09:00:00+00:00", "peakPct": 70.0},
                        {"resetAt": "2026-11-29T09:00:00+00:00", "peakPct": 44.0, "current": True}],
        }]}
        max_usage = [{"accountUuid": "u1", "label": "Work", "fetchedAt": "2026-11-26T10:00:00+00:00",
                      "week": {"percent": 9, "resetsAt": "2026-11-27T15:00:00+00:00"},
                      "usageCredits": {"usedMinor": 0, "currency": "GBP", "exponent": 2}}]
        claude_history = [
            {"time": "2026-11-19T10:00:00+00:00", "accountUuid": "u1", "sevenDayPercent": 30, "sevenDayResetsAt": "2026-11-20T15:00:00+00:00"},
            {"time": "2026-11-25T10:00:00+00:00", "accountUuid": "u1", "sevenDayPercent": 5, "sevenDayResetsAt": 1795791600},
        ]
        codex_live = {"planType": "free", "fetchedAt": "2026-11-26T11:00:00+00:00",
                      "primary": {"percent": 3, "windowMins": 43200, "resetsAt": "2026-12-10T15:40:47+00:00"}}
        out = insights.build_plan_insights(grokbot, max_usage, claude_history, codex_live, [], now=now)
        keys = [p["key"] for p in out]
        self.assertEqual(keys, ["grok-bot:acct-1", "claude:u1", "codex:primary"])
        gb = out[0]
        self.assertEqual(gb["compare"]["previousPeakPct"], 70.0)
        self.assertEqual(gb["credits"]["kind"], "remaining")
        self.assertIsNotNone(gb["forecast"])
        claude = out[1]
        self.assertEqual(claude["compare"]["previousPeakPct"], 30.0)
        self.assertEqual(claude["credits"]["kind"], "used")
        self.assertEqual(out[2]["windowSeconds"], 43200 * 60)

    def test_bad_source_does_not_break_others(self):
        out = insights.build_plan_insights({"accounts": "nonsense"}, None, None, None, None, now=at(26, 11))
        self.assertEqual(out, [])


if __name__ == "__main__":
    unittest.main()
