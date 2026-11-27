"""1.8.7: the reorganised pages. The Accounts page folded into each tool tab,
one Overview chart with a split switch, Days split by account and tool,
honest weekly spend wording and the Settings clean-up."""
import os
import re
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

from test_v180_features import NODE, html, run_js  # noqa: E402

COLOUR_CONSTS = ("SERIES_FIXED_COLOURS", "CLAUDE_ACCOUNT_COLOURS", "OTHER_SERIES_COLOURS")


def page():
    with open(os.path.join(ROOT, "dashboard.html")) as f:
        return f.read()


@unittest.skipUnless(NODE, "node not installed")
class ScriptSyntaxTests(unittest.TestCase):
    def test_every_inline_script_parses(self):
        scripts = re.findall(r"<script>(.*?)</script>", page(), re.S)
        self.assertTrue(scripts)
        for i, js in enumerate(scripts):
            with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
                f.write(js)
            try:
                r = subprocess.run([NODE, "--check", f.name], capture_output=True, text=True, timeout=30)
            finally:
                os.unlink(f.name)
            self.assertEqual(r.returncode, 0, "script %d: %s" % (i, r.stderr[:400]))


class AccountsFoldedInTests(unittest.TestCase):
    def test_no_accounts_page_and_old_links_redirect(self):
        s = page()
        self.assertNotIn('data-tab="accounts"', s)
        self.assertNotIn('id="view-accounts"', s)
        self.assertIn("if (tab === 'accounts') tab = 'overview';", s)
        self.assertIn('id="limitsCard"', s)

    def test_rename_only_in_settings(self):
        s = page()
        self.assertIn("Rename this account in Settings, Account names.", s)
        self.assertNotIn("on its Accounts card", s)

    def test_grok_bot_charts_say_what_they_measure(self):
        self.assertGreaterEqual(page().count("% of weekly limit, not priced"), 2)


@unittest.skipUnless(NODE, "node not installed")
class ChartModeTests(unittest.TestCase):
    FNS = ["chartModesFor", "pickChartMode", "chartModeLabel"]

    def test_modes_per_tab(self):
        out = run_js(self.FNS, """
          out.all = chartModesFor('all', 3);
          out.oneAcct = chartModesFor('claude', 1);
          out.twoAcct = chartModesFor('claude', 2);
          out.keep = pickChartMode('account', chartModesFor('all', 2));
          out.fallback = pickChartMode('account', chartModesFor('codex', 1));
          out.labels = [chartModeLabel('agent', 'cost'), chartModeLabel('total', 'cost'), chartModeLabel('total', 'tokens')];
        """)
        self.assertEqual(out["all"], ["agent", "account", "total"])
        self.assertEqual(out["oneAcct"], ["total"])  # no single-series split on a one-account tab
        self.assertEqual(out["twoAcct"], ["account", "total"])
        self.assertEqual(out["keep"], "account")
        self.assertEqual(out["fallback"], "total")
        self.assertEqual(out["labels"], ["by agent", "in total", "by token type"])


@unittest.skipUnless(NODE, "node not installed")
class SeriesColourTests(unittest.TestCase):
    def test_same_name_same_colour_everywhere(self):
        out = run_js(["seriesColour"], """
          var data = {sources: [{tool: 'claude', id: 'a', label: 'Work Claude'}, {tool: 'claude', id: 'b', label: 'Personal Claude'}, {tool: 'claude', id: 'unknown-session', label: 'Older'}]};
          out.work = seriesColour('Work Claude');
          out.personal = seriesColour('Personal Claude');
          out.codex = seriesColour('Codex');
          out.other1 = seriesColour('Some Tool');
          out.other2 = seriesColour('Some Tool');
        """, COLOUR_CONSTS)
        self.assertNotEqual(out["work"], out["personal"])
        self.assertEqual(out["codex"], "#2f9e6e")
        self.assertEqual(out["other1"], out["other2"])


@unittest.skipUnless(NODE, "node not installed")
class DaySplitTests(unittest.TestCase):
    FNS = ["daySplitParts", "seriesColour", "zeroTok", "addTok", "tokVec", "tokIO", "accountBucketName", "isGrokCliSession"]
    CONSTS = COLOUR_CONSTS + ("AGENT_BUCKET_NAMES", "TOK_KINDS")

    def run_split(self, body):
        try:
            return run_js(self.FNS, body, self.CONSTS)
        except AssertionError as e:  # pragma: no cover - helps when a helper is renamed
            raise AssertionError("daySplitParts helpers: %s" % e)

    def test_parts_add_up_and_rounding_is_spread(self):
        out = self.run_split("""
          var data = {sources: [{tool: 'claude', id: 'w', label: 'Work'}, {tool: 'claude', id: 'p', label: 'Personal'}]};
          const day = {period: '2026-11-24', agents: [
            {agent: 'claude', totalCost: 10.00, modelBreakdowns: []},
            {agent: 'codex', totalCost: 2.5, modelBreakdowns: []}]};
          const sessions = [
            {period: '2026-11-24', accountUuid: 'w', accountLabel: 'Work', totalCost: 6.00, modelBreakdowns: []},
            {period: '2026-11-24', accountUuid: 'p', accountLabel: 'Personal', totalCost: 3.97, modelBreakdowns: []}];
          out.parts = daySplitParts(day, sessions, 'all');
        """)
        names = [p["name"] for p in out["parts"]]
        self.assertNotIn("Unassigned", names)  # 3c gap is rounding, spread across accounts
        self.assertIn("Codex", names)
        self.assertAlmostEqual(sum(p["cost"] for p in out["parts"]), 12.5, places=6)


class WeeklyReportWordingTests(unittest.TestCase):
    def test_honest_spend_wording(self):
        s = page()
        self.assertIn("from plan readings taken that week", s)
        self.assertIn("How the week's spend was counted", s)
        self.assertIn("No plan readings were recorded that week.", s)


@unittest.skipUnless(NODE, "node not installed")
class SettingsTests(unittest.TestCase):
    def test_claude_status_follows_reading_age(self):
        out = run_js(["sourceBadge", "liveAgeLabel", "snapshotAge"], """
          const now = Date.now();
          const old = new Date(now - 2 * 86400000 - 60000).toISOString();
          const fresh = new Date(now - 60000).toISOString();
          out.old = sourceBadge({tool: 'claude', kind: 'official-live', maxUsage: {live: true, fetchedAt: old}});
          out.fresh = sourceBadge({tool: 'claude', kind: 'official-live', maxUsage: {live: true, fetchedAt: fresh}});
          out.codex = sourceBadge({tool: 'codex', kind: 'official-live'});
        """, ("KIND_BADGE", "STALE_SNAPSHOT_MS"))
        self.assertEqual(out["old"]["text"], "live, 2 days old")
        self.assertEqual(out["old"]["bg"], "var(--neutral-bg)")
        self.assertTrue(out["fresh"]["text"].startswith("live "))
        self.assertEqual(out["codex"]["text"], "live")

    def test_settings_static_parts(self):
        s = page()
        self.assertIn('id="settingsNav"', s)
        self.assertIn("No data yet: ", s)
        self.assertIn("Plan from your manual note: ", s)
        self.assertIn("(billing)", s)
        self.assertNotIn('<input type="hidden" data-seat-currency', s)
        self.assertIn("need${n === 1 ? 's' : ''} you to choose who pays", s)


if __name__ == "__main__":
    unittest.main()
