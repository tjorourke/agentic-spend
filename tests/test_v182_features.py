"""1.8.2: menu bar app stays alive when macOS hides its icon, first-run
onboarding, global $ | Tokens switch, tokens in the Model mix, prefetch."""
import json
import os
import re
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

import digest  # noqa: E402
import server  # noqa: E402
from test_v180_features import NODE, html, run_js  # noqa: E402


def read(*parts):
    with open(os.path.join(ROOT, *parts)) as f:
        return f.read()


def swift_sources():
    d = os.path.join(ROOT, "macos", "Sources")
    return {n: read("macos", "Sources", n) for n in os.listdir(d) if n.endswith(".swift")}


class MenuBarAppTests(unittest.TestCase):
    """Root cause of the invisible icon: Control Center hid it (app not yet
    approved under Privacy & Security, or switched off under Menu Bar), and a
    MenuBarExtra without isInserted quits the app when that happens."""

    def test_menubarextra_keeps_app_alive_when_hidden(self):
        app = swift_sources()["AgenticSpendApp.swift"]
        self.assertIn("MenuBarExtra(isInserted:", app)
        # Only write when the value changes; SwiftUI writes the same value back
        # on every update, which otherwise spins the main thread.
        self.assertRegex(app, r"set:\s*\{\s*if \$0 != store\.menuBarInserted")

    def test_hidden_icon_shows_help_and_reopen_works(self):
        store = swift_sources()["Store.swift"]
        self.assertIn("func menuBarHidden()", store)
        self.assertIn("InfoWindow.show(.hidden", store)
        self.assertIn("func reopened()", store)
        self.assertIn("DispatchQueue.main.async", store)  # not from inside a view update

    def test_onboarding_text_and_settings_links(self):
        onb = swift_sources()["Onboarding.swift"]
        self.assertIn("Privacy & Security", onb)
        self.assertIn("Open Anyway", onb)
        self.assertIn("Allow in the Menu Bar", onb)
        self.assertIn("x-apple.systempreferences:com.apple.settings.PrivacySecurity.extension", onb)
        self.assertIn("x-apple.systempreferences:com.apple.ControlCenter-Settings.extension", onb)
        self.assertIn("Open dashboard", onb)

    def test_first_launch_opens_dashboard_and_welcome(self):
        store = swift_sources()["Store.swift"]
        self.assertIn("welcomeShown", store)
        self.assertRegex(store, r"openDashboard\(\)[\s\S]{0,200}InfoWindow\.show")
        self.assertIn('"Open dashboard"', "".join(swift_sources().values()))

    def test_build_runs_self_test_before_signing(self):
        build = read("macos", "build.sh")
        self.assertIn("--self-test", build)
        self.assertLess(build.index('AgenticSpend" --self-test'), build.index("codesign --force"))
        onb = swift_sources()["Onboarding.swift"]
        for check in ("isFallback", "isTemplate", "LSUIElement", "com.agenticspend.menubar"):
            self.assertIn(check, onb)

    def test_info_plist_is_menu_bar_only_with_fixed_bundle_id(self):
        plist = read("macos", "Resources", "Info.plist")
        self.assertRegex(plist, r"<key>LSUIElement</key>\s*<true/>")
        self.assertRegex(plist, r"<key>CFBundleIdentifier</key>\s*<string>com.agenticspend.menubar</string>")

    def test_placeholder_label_is_never_empty(self):
        store = swift_sources()["Store.swift"]
        m = re.search(r'placeholderTitle\s*=\s*"([^"]*)"', store)
        self.assertTrue(m and m.group(1).strip())


class GatekeeperTests(unittest.TestCase):
    FILES = ["Formula/agentic-spend.rb", "Casks/agentic-spend-app.rb", "macos/build.sh", "server.py", "login_item.py"]

    def test_nothing_bypasses_gatekeeper(self):
        texts = [read(p) for p in self.FILES] + list(swift_sources().values())
        for t in texts:
            self.assertNotRegex(t, r"xattr\s+-[a-z]*d")
            self.assertNotRegex(t, r"xattr\s+-[a-z]*c")
            self.assertNotRegex(t, r"\bspctl\b")
            self.assertNotIn("com.apple.quarantine", t)


class CaveatTests(unittest.TestCase):
    def test_formula_caveats(self):
        f = read("Formula", "agentic-spend.rb")
        self.assertIn("def caveats", f)
        cav = f[f.index("def caveats"):]
        for s in ("http://127.0.0.1:8787", 'open "/Applications/Agentic Spend.app"',
                  "System Settings > Privacy & Security > Open Anyway", "AGENTIC_SPEND_NO_FIRST_RUN_OPEN"):
            self.assertIn(s, cav)

    def test_cask_caveats(self):
        c = read("Casks", "agentic-spend-app.rb")
        for s in ("http://127.0.0.1:8787", 'open "/Applications/Agentic Spend.app"',
                  "System Settings > Privacy & Security > Open Anyway", "Allow in the Menu Bar"):
            self.assertIn(s, c)

    def test_readme_quick_start_first(self):
        r = read("README.md")
        heads = re.findall(r"^## (.+)$", r, re.M)
        self.assertEqual(heads[0], "Quick start")
        self.assertIn("Notarisation", r)

    def test_launcher_does_not_open_twice(self):
        f = read("Formula", "agentic-spend.rb")
        self.assertIn("export AGENTIC_SPEND_NO_FIRST_RUN_OPEN=1", f)


class FirstRunOpenTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.p = mock.patch("data_dir.DATA_DIR", self.tmp.name)
        self.p.start()

    def tearDown(self):
        self.p.stop()
        self.tmp.cleanup()

    def test_opens_once(self):
        opened = []
        self.assertTrue(server.first_run_open("http://127.0.0.1:8787/", opener=opened.append, env={}))
        self.assertFalse(server.first_run_open("http://127.0.0.1:8787/", opener=opened.append, env={}))
        self.assertEqual(opened, ["http://127.0.0.1:8787/"])
        self.assertTrue(os.path.exists(os.path.join(self.tmp.name, server.FIRST_RUN_FILE)))

    def test_env_opt_out(self):
        opened = []
        self.assertFalse(server.first_run_open("u", opener=opened.append, env={"AGENTIC_SPEND_NO_FIRST_RUN_OPEN": "1"}))
        self.assertEqual(opened, [])

    def test_launchd_no_open_does_not_block_first_run(self):
        # The login item sets AGENTIC_SPEND_NO_OPEN=1; the first run still opens once.
        opened = []
        self.assertTrue(server.first_run_open("u", opener=opened.append, env={"AGENTIC_SPEND_NO_OPEN": "1"}))


class DigestCacheTests(unittest.TestCase):
    def test_digest_uses_recent_cache_without_refreshing(self):
        payload = {"daily": [], "accountSessions": []}
        with mock.patch.dict(server._cache, {"data": payload, "ts": __import__("time").time() - 60}), \
             mock.patch.object(server, "get_usage", side_effect=AssertionError("should not refresh")), \
             mock.patch.object(digest, "build_digest", return_value={"weekStart": "x"}) as bd:
            server._digest_cache.clear()
            self.assertEqual(server.get_digest("2026-11-16"), {"weekStart": "x"})
            self.assertEqual(server.get_digest("2026-11-16"), {"weekStart": "x"})
            self.assertEqual(bd.call_count, 1)  # second answer from the digest cache

    def test_digest_refreshes_when_cache_too_old(self):
        with mock.patch.dict(server._cache, {"data": {"daily": []}, "ts": 0}), \
             mock.patch.object(server, "get_usage", return_value={"error": "x"}) as gu:
            self.assertEqual(server.get_digest("2026-11-16"), {"error": "x"})
            gu.assert_called_once()

    def test_digest_has_tokens(self):
        day = {"period": "2026-11-17", "totalCost": 10,
               "modelBreakdowns": [{"modelName": "m", "cost": 10, "inputTokens": 100, "outputTokens": 50,
                                    "cacheCreationTokens": 7, "cacheReadTokens": 9}],
               "agents": [{"agent": "claude", "totalCost": 10,
                           "modelBreakdowns": [{"modelName": "m", "cost": 10, "inputTokens": 100, "outputTokens": 50}]}]}
        sess = [{"period": "2026-11-17", "projectPath": "/x/proj", "totalCost": 10,
                 "modelBreakdowns": [{"modelName": "m", "inputTokens": 100, "outputTokens": 50}]}]
        d = digest.build_digest({"daily": [day], "sessions": sess, "accountSessions": sess}, week=__import__("datetime").date(2026, 11, 16))
        self.assertEqual([x.get("tokens") for x in d["days"] if x["date"] == "2026-11-17"], [150])
        self.assertEqual(d["agents"][0]["tokens"], 150)
        if d.get("topProjects"):
            self.assertEqual(d["topProjects"][0]["tokens"], 150)


class GlobalMetricToggleTests(unittest.TestCase):
    """The top bar has two segmented controls: $ | Tokens and Graph | Text.
    Before 1.8.2 the only $/Tokens control was on the Daily card (added in
    1c930f9); there was never a global one. This keeps the global one in place."""

    def test_toggle_pair_in_top_bar(self):
        h = html()
        bar = h[h.index('id="metricToggle"') - 400: h.index('id="viewToggle"') + 600]
        self.assertIn('data-metric="cost"', bar)
        self.assertIn('data-metric="tokens"', bar)
        self.assertIn('>Tokens<', bar)
        self.assertIn('data-view="graph"', bar)
        self.assertIn('data-view="table"', bar)
        self.assertLess(h.index('id="metricToggle"'), h.index('id="viewToggle"'))
        self.assertEqual(h.count('id="metricToggle"'), 1)
        self.assertNotIn('chartMetricSeg', h)

    def test_persisted_in_local_storage(self):
        h = html()
        self.assertIn("METRIC_KEY = 'spendMetric'", h.replace('METRIC_KEY=', 'METRIC_KEY = '))
        self.assertRegex(h, r"localStorage\.setItem\(METRIC_KEY")
        self.assertRegex(h, r"localStorage\.getItem\('spendMetric'\)")

    def test_applies_to_every_card(self):
        h = html()
        for fn in ("renderModelMix", "renderDigest"):
            m = re.search(r"^function %s\([\s\S]*?\n}\n" % fn, h, re.M)
            self.assertTrue(m, fn)
            self.assertIn("spendMetric()", m.group(0), fn)
        proj = h[h.index("  if (tab === 'projects') {"):]
        self.assertIn("spendMetric()", proj[:proj.index("\n  }\n")])
        # the Overview's Daily chart follows the same setting
        self.assertRegex(h, r"chartMetric = spendMetric\(\)")


@unittest.skipUnless(NODE, "node not installed")
class ModelMixTokenTests(unittest.TestCase):
    HELPERS = ["tokVec", "zeroTok", "addTok", "tokIO", "tokAll", "tokTitle", "valueCellHtml"]

    def test_value_cell_shows_both_and_tooltip(self):
        out = run_js(self.HELPERS, """
          const t = tokVec({inputTokens: 1000000, outputTokens: 230000, cacheCreationTokens: 5, cacheReadTokens: 7});
          out.c = valueCellHtml(437.25, t, 'cost', false);
          out.t = valueCellHtml(437.25, t, 'tokens', true);
        """)
        self.assertIn("$437.25", out["c"])
        self.assertIn("1230000 tok", out["c"])
        self.assertIn("vc-sub muted", out["c"])
        self.assertIn("Cache write 5", out["c"])
        self.assertIn("Cache read 7", out["c"])
        self.assertIn("All tokens incl. cache 1230012", out["c"])
        self.assertTrue(out["t"].index("tok") < out["t"].index("$437.25"))

    def test_account_token_split_sums_to_model_totals(self):
        out = run_js(self.HELPERS + ["accountBucketName", "modelAccountSplit", "finishAccountSplit", "seriesColour"], """
          const mb = (c, i, o, cw, cr) => ({modelName: 'sonnet', cost: c, inputTokens: i, outputTokens: o, cacheCreationTokens: cw, cacheReadTokens: cr});
          const days = [{period: '2026-11-24', agents: [{agent: 'claude', modelBreakdowns: [mb(100, 1000, 500, 300, 9000)]},
                                                         {agent: 'opencode', modelBreakdowns: [{modelName: 'g', cost: 5, inputTokens: 40, outputTokens: 4}]}]}];
          const sessions = [
            {period: '2026-11-24', accountUuid: 'w', label: 'Work', modelBreakdowns: [mb(30, 100, 400, 0, 1000)]},
            {period: '2026-11-24', accountUuid: 'p', label: 'Personal', modelBreakdowns: [mb(70, 900, 100, 300, 8000)]}];
          out.r = modelAccountSplit(days, sessions, '2026-11-20', '2026-11-26', 'all');
        """, consts=("ACCOUNT_BUCKET_COLOURS", "AGENT_BUCKET_NAMES", "SERIES_FIXED_COLOURS", "CLAUDE_ACCOUNT_COLOURS", "OTHER_SERIES_COLOURS"))
        tok = out["r"]["tokens"]["sonnet"]
        for k, total in (("input", 1000), ("output", 500), ("cacheWrite", 300), ("cacheRead", 9000)):
            self.assertAlmostEqual(sum(b[k] for b in tok.values()), total)
        self.assertAlmostEqual(tok["Work"]["input"], 100)
        self.assertAlmostEqual(tok["Personal"]["output"], 100)
        self.assertEqual(out["r"]["tokens"]["g"]["OpenCode"]["input"], 40)
        buckets = {b["name"]: b["tok"] for b in out["r"]["buckets"]}
        self.assertAlmostEqual(sum(b["input"] + b["output"] for b in buckets.values()), 1500 + 44)

    def test_model_mix_table_uses_value_cells_everywhere(self):
        h = html()
        m = re.search(r"^function renderModelMix\([\s\S]*?\n}\n", h, re.M).group(0)
        self.assertGreaterEqual(m.count("valueCellHtml("), 3)  # cells, Total column, Total row


class PrefetchTests(unittest.TestCase):
    def test_prefetch_after_overview_at_idle(self):
        h = html()
        self.assertIn("requestIdleCallback", h)
        load = re.search(r"^async function load\(force\)[\s\S]*?\n}\n", h, re.M).group(0)
        self.assertLess(load.index("update();"), load.index("schedulePrefetch("))
        pre = re.search(r"^function schedulePrefetch\([\s\S]*?\n}\n", h, re.M).group(0)
        self.assertIn("loadDigest()", pre)
        self.assertIn("loadAppSettings(", pre)

    def test_single_flight(self):
        h = html()
        self.assertIn("if (appSettingsFetching) return appSettingsFetching;", h)
        self.assertRegex(h, r"async function loadDigest\(week\) \{[\s\S]{0,400}digestFetching")


if __name__ == "__main__":
    unittest.main()
