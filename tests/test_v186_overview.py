"""1.8.6 Overview redesign: tool tabs, Today strip, 14-day chart, Value tab,
one-line plan rows that open on click."""
import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
from test_v180_features import html, run_js  # noqa: E402


def section(src, view_id):
    start = src.index(f'<section id="{view_id}"')
    return src[start:src.index("</section>", start)]


class LayoutTests(unittest.TestCase):
    def test_value_tab_holds_seat_value_cache_and_token_sources(self):
        src = html()
        self.assertIn('data-tab="value"', src)
        value = section(src, "view-value")
        for card in ("seatValueCard", "cacheCard", "tokenSourcesCard"):
            self.assertIn(f'id="{card}"', value)
        overview = section(src, "view-overview")
        for card in ("seatValueCard", "cacheCard", "tokenSourcesCard"):
            self.assertNotIn(f'id="{card}"', overview)
        self.assertIn("value: 'Value'", src)

    def test_overview_order_tabs_today_then_14_days(self):
        overview = section(html(), "view-overview")
        order = [overview.index(f'id="{i}"') for i in
                 ("toolTabs", "todayStrip", "last14Card", "toolSummaryCard", "realCostOverview", "model-mix", "allModelsCard")]
        self.assertEqual(order, sorted(order))
        self.assertIn('role="tablist"', overview)

    def test_value_tab_still_renders_its_cards(self):
        src = html()
        self.assertIn("if (tab === 'overview' || tab === 'value') {", src)

    def test_grok_bot_alone_still_gets_a_grok_tab(self):
        self.assertIn("if (sourcePresent('grok-bot')) agents.add('grok');", html())

    def test_agent_picker_hidden_on_overview(self):
        self.assertIn("(t === 'overview' || noRange)", html())

    def test_plan_rows_have_an_obvious_toggle(self):
        src = html()
        body = src[src.index("function collapsePlanRows"):src.index("function placeCacheCard")]
        for needle in ("aria-expanded", "aria-controls", "Show details", "Hide details", "plan-chev", "status-dot"):
            self.assertIn(needle, body)
        self.assertRegex(src, r"\.plan-row-toggle \{[^}]*cursor: pointer")
        self.assertIn(".plan-row-toggle:hover", src)
        self.assertIn('.plan-row-toggle[aria-expanded="true"] .plan-chev', src)
        self.assertIn("collapsePlanRows(box);", src)

    def test_new_copy_is_os_neutral(self):
        src = html()
        start = src.index("// ---- 1.8.6 Overview")
        block = src[start:src.index("function update() {", start)]
        self.assertNotRegex(block, r"\bMac\b|macOS|Linux|LaunchAgent|systemd")


class HelperTests(unittest.TestCase):
    def test_tool_tabs_split_claude_accounts_and_skip_old_sessions(self):
        out = run_js(["agentName", "toolTabsFrom"], """
          out.tabs = toolTabsFrom(new Set(['codex', 'claude', 'grok']), [
            {id: 'w', label: 'Work Claude'}, {id: 'p', label: 'Personal Claude'},
            {id: 'unknown-session', label: 'Older sessions'}]);
          out.single = toolTabsFrom(new Set(['claude']), []);
        """, consts=["AGENT_NAMES"])
        self.assertEqual([t["label"] for t in out["tabs"]], ["Overview", "Work Claude", "Personal Claude", "Codex", "Grok"])
        self.assertEqual(out["tabs"][1]["value"], "account:w")
        self.assertEqual([t["value"] for t in out["single"]], ["all", "claude"])

    def test_only_detected_tools_get_tabs(self):
        out = run_js(["agentName", "toolTabsFrom"], """
          out.tabs = toolTabsFrom(new Set(['opencode']), []);
        """, consts=["AGENT_NAMES"])
        self.assertEqual([t["label"] for t in out["tabs"]], ["Overview", "OpenCode"])

    def test_plan_insights_follow_the_tab(self):
        out = run_js(["toolsForFilter", "planInsightsForFilter", "nearestPlan"], """
          const ins = [
            {key: 'claude:w', tool: 'claude', pct: 12}, {key: 'claude:p', tool: 'claude', pct: 40},
            {key: 'codex:primary', tool: 'codex', pct: 3},
            {key: 'grok-bot:x', tool: 'grok-bot', pct: 70.4, forecast: {hitsLimitAt: '2026-11-28T23:21:00Z'}}];
          out.all = nearestPlan(planInsightsForFilter(ins, 'all')).key;
          out.work = planInsightsForFilter(ins, 'account:w').map(p => p.key);
          out.grok = planInsightsForFilter(ins, 'grok').map(p => p.key);
          out.codex = nearestPlan(planInsightsForFilter(ins, 'codex')).key;
          out.none = nearestPlan(planInsightsForFilter(ins, 'opencode'));
        """)
        self.assertEqual(out["all"], "grok-bot:x")
        self.assertEqual(out["work"], ["claude:w"])
        self.assertEqual(out["grok"], ["grok-bot:x"])
        self.assertEqual(out["codex"], "codex:primary")
        self.assertIsNone(out["none"])

    def test_status_colour(self):
        out = run_js(["planStatus"], """
          out.v = [planStatus(null), planStatus(5), planStatus(5, true), planStatus(70), planStatus(95)];
        """)
        self.assertEqual(out["v"], ["", "st-ok", "st-warn", "st-warn", "st-bad"])

    def test_source_rows_follow_the_tab(self):
        out = run_js(["toolsForFilter", "sourceMatchesFilter"], """
          function grokBotAccounts() { return [{accountId: 'gb1'}]; }
          const S = [{tool: 'claude', id: 'w'}, {tool: 'claude', id: 'p'}, {tool: 'codex', id: 'codex'},
                     {tool: 'grok-cli', id: 'grok-cli'}, {tool: 'grok-bot', grokBot: true}, {tool: 'opencode'}];
          const pick = v => S.filter(s => sourceMatchesFilter(s, v)).map(s => s.tool + (s.id ? ':' + s.id : ''));
          out.all = pick('all').length;
          out.work = pick('account:w');
          out.grok = pick('grok');
          out.codex = pick('codex');
        """)
        self.assertEqual(out["all"], 6)
        self.assertEqual(out["work"], ["claude:w"])
        self.assertEqual(out["grok"], ["grok-cli:grok-cli", "grok-bot"])
        self.assertEqual(out["codex"], ["codex:codex"])

    def test_last_14_days_stacks_by_agent(self):
        out = run_js(["metricValue", "rowRealTokens", "rangeSeries"], """
          var chartMetric = 'cost';
          const addDays = (d, n) => { const t = new Date(d + 'T12:00:00Z'); t.setUTCDate(t.getUTCDate() + n); return t.toISOString().slice(0, 10); };
          function sourcePresent() { return true; }
          function detectionIdForTool(a) { return a; }
          const days = [
            {period: '2026-11-27', agents: [{agent: 'claude', totalCost: 3}, {agent: 'codex', totalCost: 1}]},
            {period: '2026-11-20', agents: [{agent: 'claude', totalCost: 2}]},
            {period: '2026-11-01', agents: [{agent: 'grok', totalCost: 9}]}];
          const r = rangeSeries(days, '2026-11-14', '2026-11-27', 'all');
          out.len = r.periods.length; out.first = r.periods[0]; out.series = r.series;
          out.last = r.rows[13].parts; out.mid = r.rows.find(x => x.period === '2026-11-20').parts;
        """)
        self.assertEqual(out["len"], 14)
        self.assertEqual(out["first"], "2026-11-14")
        self.assertEqual(out["series"], ["claude", "codex"])   # grok is outside the 14 days
        self.assertEqual(out["last"], [3, 1])
        self.assertEqual(out["mid"], [2, 0])


    def test_chart_title_follows_the_range(self):
        out = run_js(["rangeChartTitle"], """
          out.v = [rangeChartTitle(7, null, null, 'a', 'b'), rangeChartTitle(30, null, null, 'a', 'b'),
                   rangeChartTitle(9999, null, null, 'a', 'b'), rangeChartTitle(1, null, null, 'a', 'b'),
                   rangeChartTitle(7, '2026-11-01', '2026-11-05', '2026-11-01', '2026-11-05')];
        """)
        self.assertEqual(out["v"], ["Last 7 days", "Last 30 days", "All time", "Today", "2026-11-01 to 2026-11-05"])

    def test_chart_uses_the_selected_range(self):
        src = html()
        self.assertIn("drawLast14(days, cutoff, windowEnd, mode);", src)
        self.assertIn("rangeSeries(days, cutoff, windowEnd, agent)", src)

    def test_stale_forecast_is_not_a_warning(self):
        out = run_js(["forecastSummary"], """
          const now = Date.parse('2026-11-27T17:00:00Z');
          const f = t => t;
          out.stale = forecastSummary({currentPct: 3, hitsLimitAt: '2026-11-14T08:31:09Z', resetAt: '2026-12-10T15:40:46Z', ratePerDay: 27, basis: 'window', windowStart: 'x'}, f, now);
          out.live = forecastSummary({currentPct: 60, hitsLimitAt: '2026-11-28T08:00:00Z', resetAt: '2026-11-29T00:00:00Z', ratePerDay: 27, basis: 'window', windowStart: 'x'}, f, now).warn;
          out.atLimit = forecastSummary({currentPct: 100, hitsLimitAt: '2026-11-14T08:31:09Z', resetAt: '2026-12-10T15:40:46Z', ratePerDay: 27, basis: 'window', windowStart: 'x'}, f, now).text;
        """)
        self.assertIsNone(out["stale"])
        self.assertTrue(out["live"])
        self.assertEqual(out["atLimit"], "at the limit now")


if __name__ == "__main__":
    unittest.main()
