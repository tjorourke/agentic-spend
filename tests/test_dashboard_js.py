"""Runs the dashboard's pure helper block (between the <pure-helpers> markers
in dashboard.html) under node, so the compare and forecast wording is tested
as the browser runs it. Skipped when node is not installed; the Homebrew
formula depends on node, so it is normally there."""
import json
import os
import re
import shutil
import subprocess
import unittest

HTML = os.path.join(os.path.dirname(__file__), os.pardir, "dashboard.html")
NODE = shutil.which("node")


def run_js(expr_lines):
    with open(HTML) as f:
        html = f.read()
    m = re.search(r"// <pure-helpers>(.*?)// </pure-helpers>", html, re.S)
    assert m, "pure-helpers block missing"
    script = m.group(1) + "\nconst out = {};\n" + "\n".join(expr_lines) + "\nprocess.stdout.write(JSON.stringify(out));\n"
    r = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=20)
    if r.returncode != 0:
        raise AssertionError(r.stderr)
    return json.loads(r.stdout)


@unittest.skipUnless(NODE, "node not installed")
class DashboardHelperTests(unittest.TestCase):
    def test_previous_period(self):
        out = run_js([
            "out.week = previousPeriod('2026-11-20', '2026-11-26');",
            "out.day = previousPeriod('2026-11-26', '2026-11-26');",
            "out.dst = previousPeriod('2026-03-29', '2026-04-04');",
        ])
        self.assertEqual(out["week"], {"from": "2026-11-13", "to": "2026-11-19", "days": 7})
        self.assertEqual(out["day"], {"from": "2026-11-25", "to": "2026-11-25", "days": 1})
        self.assertEqual(out["dst"]["days"], 7)

    def test_compare_labels(self):
        out = run_js([
            "out.up = compareLabel(112, 100, compareNoun(7, null, null, 7));",
            "out.down = compareLabel(50, 100, compareNoun(1, null, null, 1));",
            "out.same = compareLabel(100.2, 100, 'last week');",
            "out.zero = compareLabel(5, 0, compareNoun(30, null, null, 30));",
            "out.none = compareLabel(0, 0, 'last week');",
            "out.missing = compareLabel(5, null, 'last week');",
            "out.custom = compareNoun(7, '2026-11-01', '2026-11-07', 7);",
        ])
        self.assertEqual(out["up"], "+12% vs last week")
        self.assertEqual(out["down"], "-50% vs yesterday")
        self.assertEqual(out["same"], "same as last week")
        self.assertEqual(out["zero"], "nothing in the previous 30 days")
        self.assertEqual(out["none"], "")
        self.assertEqual(out["missing"], "")
        self.assertEqual(out["custom"], "previous 7 days")

    def test_forecast_summary(self):
        out = run_js([
            "const f = iso => 'T(' + iso.slice(0, 10) + ')';",
            "out.track = forecastSummary({currentPct: 44, projectedPct: 74.6, resetAt: '2026-11-29T09:06:55+00:00', confidence: 'normal', basis: 'readings', coverageStart: '2026-11-24T00:00:00Z', ratePerDay: 10}, f);",
            "out.hit = forecastSummary({currentPct: 60, projectedPct: 120, hitsLimitAt: '2026-11-28T09:00:00+00:00', resetAt: '2026-11-29T09:00:00+00:00', confidence: 'low', basis: 'window', windowStart: '2026-11-22T09:00:00Z', ratePerDay: 20, coverageHours: 2}, f);",
            "out.none = forecastSummary(null, f);",
            "out.cmp = windowCompareSummary({currentPeakPct: 44, previousPeakPct: 70, deltaPct: -26}, 'last week');",
            "out.cmpNone = windowCompareSummary(null, 'last week');",
        ])
        self.assertEqual(out["track"]["text"], "on track for 75% by T(2026-11-29)")
        self.assertFalse(out["track"]["warn"])
        self.assertEqual(out["hit"]["text"], "would hit 100% around T(2026-11-28) (early estimate)")
        self.assertTrue(out["hit"]["warn"])
        self.assertIn("Only 2 hours", out["hit"]["title"])
        self.assertIsNone(out["none"])
        self.assertEqual(out["cmp"]["text"], "-26 pts vs last week")
        self.assertIsNone(out["cmpNone"])

    def test_no_em_dashes_in_new_ui_strings(self):
        with open(HTML) as f:
            html = f.read()
        block = re.search(r"// <pure-helpers>(.*?)// </pure-helpers>", html, re.S).group(1)
        self.assertNotIn("\u2014", block)


if __name__ == "__main__":
    unittest.main()
