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

    def test_live_nudge_only_when_hook_is_not_ours(self):
        out = run_js([
            "out.none = shouldNudgeLive('none', true);",
            "out.other = shouldNudgeLive('other', true);",
            "out.ours = shouldNudgeLive('ours', true);",
            "out.wraps = shouldNudgeLive('wraps_other', true);",
            "out.fresh = shouldNudgeLive('none', false);",
        ])
        self.assertTrue(out["none"])
        self.assertTrue(out["other"])
        self.assertFalse(out["ours"])
        self.assertFalse(out["wraps"])
        self.assertFalse(out["fresh"])

    def test_empty_axes_draw_baseline_and_zero_per_day(self):
        fake = """
        const made = [];
        const svg = { attrs: {}, children: [],
          setAttribute(k, v) { this.attrs[k] = v; }, appendChild(c) { this.children.push(c); },
          ownerDocument: { createElementNS: (ns, tag) => { const el = { tag, attrs: {}, textContent: '',
            setAttribute(k, v) { this.attrs[k] = v; } }; return el; } } };
        """
        out = run_js([
            fake,
            "drawEmptyAxes(svg, ['11-26'], '$0.00', { axisZeroText: '$0' });",
            "out.one = svg.children.map(c => [c.tag, c.attrs['data-empty'], c.textContent]);",
            "svg.children.length = 0;",
            "const days = Array.from({length: 7}, (_, i) => '11-' + (20 + i));",
            "drawEmptyAxes(svg, days, '0');",
            "out.zeros = svg.children.filter(c => c.attrs['data-empty'] === 'zero-value').map(c => c.textContent);",
            "out.labels = svg.children.filter(c => c.attrs['data-empty'] === 'x-label').map(c => c.textContent);",
            "out.idx24 = emptyAxisLabelIndexes(24, 12);",
        ])
        kinds = [(t, k) for t, k, _ in out["one"]]
        self.assertIn(("line", "baseline"), kinds)
        texts = {k: txt for _, k, txt in out["one"] if k}
        self.assertEqual(texts["zero-value"], "$0.00")
        self.assertEqual(texts["y-zero"], "$0")
        self.assertEqual(texts["x-label"], "11-26")
        self.assertEqual(out["zeros"], ["0"] * 7)
        self.assertEqual(len(out["labels"]), 7)
        self.assertEqual(out["idx24"], list(range(0, 24, 2)) + [23])

    def test_live_age_and_window_labels(self):
        out = run_js([
            "const now = Date.parse('2026-11-26T17:34:00+00:00');",
            "out.fresh = liveAgeLabel('2026-11-26T17:23:26+00:00', now);",
            "out.day = liveAgeLabel('2026-11-25T09:36:21+00:00', now);",
            "out.days = liveAgeLabel('2026-11-24T09:36:21+00:00', now);",
            "out.s = planWindowLabel('session');",
            "out.w = planWindowLabel('week');",
        ])
        self.assertTrue(out["fresh"].startswith("live "))
        self.assertEqual(out["day"], "live, 1 day old")
        self.assertEqual(out["days"], "live, 2 days old")
        self.assertEqual(out["s"], "Session (5-hour)")
        self.assertEqual(out["w"], "Weekly (7-day)")

    def test_no_em_dashes_in_new_ui_strings(self):
        with open(HTML) as f:
            html = f.read()
        block = re.search(r"// <pure-helpers>(.*?)// </pure-helpers>", html, re.S).group(1)
        self.assertNotIn("\u2014", block)


    def test_team_value_parts(self):
        out = run_js([
            "out.none = teamValueParts({apiEquivalentUsd: 1873.85, seat: null});",
            "out.usd = teamValueParts({apiEquivalentUsd: 150, seat: {amount: 30, currency: 'USD'}, multiple: 5});",
            "out.gbp = teamValueParts({apiEquivalentUsd: 12.25, seat: {amount: 90, currency: 'GBP'}, multiple: null});",
        ])
        self.assertEqual(out["none"]["text"], "$1873.85 API-equivalent this month · set seat price")
        self.assertEqual(out["usd"]["text"], "$150.00 API-equivalent this month vs seat $30.00/month (5.0\u00d7 the seat)")
        self.assertEqual(out["gbp"]["text"], "$12.25 API-equivalent this month vs seat \u00a390.00/month")

    def test_pace_text(self):
        out = run_js([
            "const t = iso => iso.slice(11, 16);",
            "out.warn = paceText({warn: true, ratePerHour: 20, currentPct: 40, hitsLimitAt: '2026-11-26T15:30:00Z', resetAt: '2026-11-26T17:00:00Z'}, t);",
            "out.ok = paceText({warn: false, ratePerHour: 2, currentPct: 4, projectedPct: 12.4, resetAt: '2026-11-26T17:00:00Z', thin: true}, t);",
            "out.idle = paceText({idle: true, currentPct: 50, resetAt: '2026-11-26T17:00:00Z'}, t);",
            "out.full = paceText({currentPct: 100, resetAt: '2026-11-26T17:00:00Z'}, t);",
        ])
        self.assertEqual(out["warn"], "Session pace 20%/h: would hit 100% around 15:30, before the 17:00 reset")
        self.assertEqual(out["ok"], "Session pace 2%/h: on track for 12% by the 17:00 reset (early estimate)")
        self.assertIn("no Claude use", out["idle"])
        self.assertEqual(out["full"], "Session (5-hour) limit reached, resets 17:00")

    def test_model_mix_summary(self):
        out = run_js([
            "const w = [{period: '2026-11-25', value: {modelBreakdowns: [{modelName: 'claude-opus-5-5', cost: 10}, {modelName: 'claude-sonnet-5', cost: 30}]}}];",
            "const mix = {opusModels: ['claude-opus-5-5'], sonnetModel: 'claude-sonnet-5', repricedByDay: {'2026-11-25': {'claude-opus-5-5': {calculated: 20, atSonnet: 10}}}};",
            "out.m = modelMixSummary(w, mix);",
            "out.none = modelMixSummary(w, null);",
        ])
        m = out["m"]
        self.assertEqual([r["model"] for r in m["rows"]], ["claude-sonnet-5", "claude-opus-5-5"])
        self.assertEqual(m["opusCost"], 10)
        self.assertEqual(m["saving"], 5)  # half of the $10 shown, from ccusage's 20 -> 10 ratio
        self.assertEqual(m["atSonnet"], 5)
        self.assertEqual(out["none"]["saving"], 0)


if __name__ == "__main__":
    unittest.main()
