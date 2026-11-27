"""1.8.0: single Settings save, OpenCode backends, threshold rules, weekly
report weeks, model mix and projects by account, cost split graphic, shell."""
import datetime
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import account_usage
import data_dir
import digest
import notify
import opencode_backends
import pricing_region
import seat_prices
import server

HTML = os.path.join(ROOT, "dashboard.html")
NODE = shutil.which("node")


def html():
    with open(HTML) as f:
        return f.read()


def js_functions(names, consts=()):
    """Pull named top-level functions/consts out of dashboard.html so node
    can run exactly the code the browser runs."""
    src = html()
    parts = ["const escAttr = s => String(s == null ? '' : s).replace(/&/g,'&amp;').replace(/\"/g,'&quot;').replace(/</g,'&lt;');",
             "const escHtml = s => escAttr(s).replace(/>/g,'&gt;');",
             "const fmt = n => '$' + (n || 0).toLocaleString('en-GB', { minimumFractionDigits: 2, maximumFractionDigits: 2 });",
             "const fmtTok = n => String(n);"]
    for c in consts:
        m = re.search(r"^const %s = [\s\S]*?;\n" % re.escape(c), src, re.M)
        assert m, c
        parts.append(m.group(0))
    for n in names:
        m = re.search(r"^function %s\([\s\S]*?\n}\n" % re.escape(n), src, re.M)
        assert m, n
        parts.append(m.group(0))
    return "\n".join(parts)


def run_js(names, body, consts=()):
    script = js_functions(names, consts) + "\nconst out = {};\n" + body + "\nprocess.stdout.write(JSON.stringify(out));\n"
    r = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=20)
    if r.returncode != 0:
        raise AssertionError(r.stderr)
    return json.loads(r.stdout)


# ---------------------------------------------------------------- OpenCode

class OpenCodeBackendTests(unittest.TestCase):
    def test_classify_by_auth_type(self):
        c = opencode_backends.classify("google", {"google": "api"}, {}, {})
        self.assertEqual((c["label"], c["billing"], c["realSpend"]), ("Google AI Studio (Gemini API key)", "payg", True))
        self.assertIn("auth.json", c["detectedFrom"])
        sub = opencode_backends.classify("openai", {"openai": "oauth"}, {}, {})
        self.assertEqual((sub["label"], sub["billing"], sub["realSpend"]), ("ChatGPT subscription", "subscription", False))
        cloud = opencode_backends.classify("amazon-bedrock", {}, {}, {})
        self.assertEqual((cloud["billing"], cloud["realSpend"]), ("cloud", True))

    def test_custom_provider_is_unknown_until_set(self):
        cfg = {"provider": {"gw": {"name": "Gateway", "options": {"baseURL": "https://gw.example.com/v1"}},
                            "local": {"options": {"baseURL": "http://127.0.0.1:9/v1"}}}}
        gw = opencode_backends.classify("gw", {}, cfg, {})
        self.assertEqual((gw["billing"], gw["realSpend"]), ("unknown", None))
        self.assertIn("gw.example.com", gw["label"])
        self.assertIn("local proxy", opencode_backends.classify("local", {}, cfg, {})["label"])
        set_ = opencode_backends.classify("gw", {}, cfg, {"gw": "company"})
        self.assertEqual((set_["billing"], set_["realSpend"], set_["setInSettings"]), ("company", False, True))
        # a saved choice wins over detection too, e.g. a free-tier Gemini key
        self.assertEqual(opencode_backends.classify("google", {"google": "api"}, {}, {"google": "free"})["realSpend"], False)

    def test_jsonc_comments_and_trailing_commas(self):
        text = '{\n // c\n "provider": {"a": {"options": {"baseURL": "http://x//y"}},},\n /* b */ }'
        self.assertEqual(json.loads(opencode_backends._strip_jsonc(text))["provider"]["a"]["options"]["baseURL"], "http://x//y")

    def test_validate_overrides(self):
        self.assertEqual(opencode_backends.validate_overrides({"a": "payg", "b": "", "c": "unknown"}), {"a": "payg"})
        with self.assertRaises(ValueError):
            opencode_backends.validate_overrides({"a": "bogus"})
        with self.assertRaises(ValueError):
            opencode_backends.validate_overrides({"../x": "payg"})

    def _db(self, d, rows):
        path = os.path.join(d, "opencode.db")
        con = sqlite3.connect(path)
        con.execute("CREATE TABLE message (id text primary key, session_id text, time_created integer, time_updated integer, data text)")
        for i, (ts, provider, model, tokens) in enumerate(rows):
            con.execute("INSERT INTO message VALUES (?,?,?,?,?)", (str(i), "s", ts, ts, json.dumps(
                {"role": "assistant", "providerID": provider, "modelID": model, "tokens": {"input": tokens, "output": 0, "cache": {"read": 0, "write": 0}}})))
        con.commit()
        con.close()
        return path

    def test_build_and_split_matches_ccusage_total(self):
        noon = int(datetime.datetime(2026, 11, 24, 12).timestamp() * 1000)
        with tempfile.TemporaryDirectory() as d:
            db = self._db(d, [(noon, "google", "gemini-4-neon", 300), (noon, "google", "gemini-4-neon", 100),
                              (noon, "anthropic", "claude-x", 100), (noon, "gw", "claude-x", 300)])
            auth = os.path.join(d, "auth.json")
            with open(auth, "w") as f:
                json.dump({"google": {"type": "api", "key": "secret"}, "anthropic": {"type": "api"}}, f)
            info = opencode_backends.build(db_path=db, auth_path=auth, config_path=os.path.join(d, "none.json"),
                                           overrides_path=os.path.join(d, "ov.json"), litellm={"gemini/gemini-4-neon": {}})
            self.assertNotIn("secret", json.dumps(info))
            self.assertEqual(info["daily"]["2026-11-24"]["claude-x"], {"anthropic": 0.25, "gw": 0.75})
            self.assertTrue(info["models"]["gemini-4-neon"]["listPrice"])
            daily = [{"period": "2026-11-24", "agents": [{"agent": "opencode", "modelBreakdowns": [
                {"modelName": "gemini-4-neon", "cost": 12.17}, {"modelName": "claude-x", "cost": 4.0},
                {"modelName": "never-seen", "cost": 1.0}]}]}]
            split = opencode_backends.split_cost(daily, info, lambda p: True)
            self.assertAlmostEqual(sum(split.values()), 17.17, places=6)
            self.assertAlmostEqual(split["google"], 12.17)
            self.assertAlmostEqual(split[None], 1.0)
            real, covered, unknown = opencode_backends.real_and_covered(daily, info, lambda p: True)
            self.assertAlmostEqual(real, 13.17)       # google + anthropic share
            self.assertAlmostEqual(unknown, 4.0)      # gw (3.0) + never-seen (1.0)
            self.assertAlmostEqual(covered, 0.0)

    def test_server_marks_opencode_as_detected_backends(self):
        info = {"found": True, "providers": {}, "daily": {}, "models": {}, "lastDay": "2026-11-24"}
        sources = server.build_sources([], [], None, False, None, {"opencode": {"plan": "old note"}},
                                       {"agents": {"opencode": 5.0}}, {}, opencode=info)
        oc = [s for s in sources if s["tool"] == "opencode"][0]
        self.assertEqual(oc["kind"], "backends")
        self.assertIs(oc["opencodeBackends"], info)


# ---------------------------------------------------------------- thresholds

class ThresholdTests(unittest.TestCase):
    def test_whole_numbers_1_to_100(self):
        self.assertEqual(notify._clean_thresholds([95, "80", 80.0]), [80, 95])
        self.assertEqual(notify._clean_thresholds([1, 100]), [1, 100])
        for bad in ([0], [101], [80.5], ["x"], [True], [1, 2, 3, 4, 5, 6]):
            with self.assertRaises(ValueError, msg=bad):
                notify._clean_thresholds(bad)


# ---------------------------------------------------------------- single save

class SettingsSaveTests(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        p = lambda n: os.path.join(self.d, n)
        self.patches = [
            mock.patch.object(account_usage, "LABELS_PATH", p("account-labels.json")),
            mock.patch.object(account_usage, "PLANS_PATH", p("plans.json")),
            mock.patch.object(notify, "SETTINGS_PATH", p("notifications.json")),
            mock.patch.object(seat_prices, "PATH", p("seat-prices.json")),
            mock.patch.object(pricing_region, "PATH", p("pricing-region.json")),
            mock.patch.object(data_dir, "DATA_DIR", self.d),
        ]
        for x in self.patches:
            x.start()
        with open(p("account-labels.json"), "w") as f:
            json.dump({"aaa-1": {"label": "Old", "note": "kept"}}, f)

    def tearDown(self):
        for x in self.patches:
            x.stop()
        shutil.rmtree(self.d)

    def test_two_account_names_saved_in_one_request(self):
        # The 1.7.2 bug: two names typed, one Save clicked per row, the
        # second was lost. One request now carries the whole form.
        r = server.apply_settings({"labels": {"aaa-1": "Work", "bbb-2": "Personal"}}, cached={})
        self.assertTrue(r["ok"], r)
        labels = account_usage.load_labels()
        self.assertEqual(labels["aaa-1"], {"label": "Work", "note": "kept"})
        self.assertEqual(labels["bbb-2"]["label"], "Personal")

    def test_invalid_section_writes_nothing(self):
        r = server.apply_settings({"labels": {"aaa-1": "New"}, "notifications": {"thresholds": [150]}}, cached={})
        self.assertFalse(r["ok"])
        self.assertIn("Nothing was saved", r["error"])
        self.assertEqual(account_usage.load_labels()["aaa-1"]["label"], "Old")
        self.assertFalse(os.path.exists(notify.SETTINGS_PATH))

    def test_whole_form(self):
        r = server.apply_settings({
            "labels": {"aaa-1": "  Work   account "},
            "pricingRegion": "UK",
            "seatPrices": {"aaa-1": {"amount": 100, "currency": "GBP"}, "bbb-2": {"amount": None}},
            "opencodeProviders": {"gw": "company", "google": ""},
            "notifications": {"enabled": True, "thresholds": [90, 75], "perPlan": {"claude-x": [50]}},
            "plan": {"agent": "grok", "plan": "Super", "coveredByPlan": True, "note": "", "asOf": "2026-11-26"},
        }, cached={})
        self.assertTrue(r["ok"], r)
        self.assertEqual(account_usage.load_labels()["aaa-1"]["label"], "Work account")
        self.assertEqual(pricing_region.load(), "UK")
        self.assertEqual(seat_prices.load()["aaa-1"]["currency"], "GBP")
        self.assertEqual(opencode_backends.load_overrides(), {"gw": "company"})
        self.assertEqual(notify.load_settings()["thresholds"], [75, 90])
        self.assertTrue(account_usage.load_plans()["grok"]["coveredByPlan"])
        r2 = server.apply_settings({"pricingRegion": "auto"}, cached={})
        self.assertTrue(r2["ok"])
        self.assertIsNone(pricing_region.load())

    def test_unknown_key_refused(self):
        self.assertRaises(ValueError, server.apply_settings, {"evil": 1}, {})

    def test_atomic_write_leaves_no_temp_files(self):
        target = os.path.join(self.d, "x.json")
        data_dir.atomic_write_json(target, {"a": 1})
        with open(target) as f:
            self.assertEqual(json.load(f), {"a": 1})
        self.assertEqual([n for n in os.listdir(self.d) if n.startswith(".x.json")], [])


# ---------------------------------------------------------------- weekly report

class WeeklyReportTests(unittest.TestCase):
    def payload(self):
        return {"daily": [
            {"period": "2026-11-03", "totalCost": 10.0, "modelBreakdowns": [], "agents": []},
            {"period": "2026-11-18", "totalCost": 30.0, "modelBreakdowns": [], "agents": []},
        ], "sources": [], "accountSessions": []}

    def test_parse_week(self):
        latest = datetime.date(2026, 11, 16)
        self.assertEqual(digest.parse_week("2026-11-12", latest), datetime.date(2026, 11, 9))
        self.assertIsNone(digest.parse_week(None, latest))
        self.assertRaises(ValueError, digest.parse_week, "2026-11-23", latest)
        self.assertRaises(ValueError, digest.parse_week, "nope", latest)

    def test_build_for_earlier_week_and_empty_week(self):
        today = datetime.date(2026, 11, 26)
        latest = digest.build_digest(self.payload(), today=today, tz=datetime.timezone.utc)
        self.assertEqual((latest["weekStart"], latest["latestWeekStart"], latest["earliestWeekStart"]),
                         ("2026-11-16", "2026-11-16", "2026-11-02"))
        self.assertEqual(latest["spend"]["list"], 30.0)
        early = digest.build_digest(self.payload(), today=today, tz=datetime.timezone.utc, week=datetime.date(2026, 11, 2))
        self.assertEqual((early["weekStart"], early["weekEnd"], early["spend"]["list"], early["hasData"]),
                         ("2026-11-02", "2026-11-08", 10.0, True))
        empty = digest.build_digest(self.payload(), today=today, tz=datetime.timezone.utc, week=datetime.date(2026, 11, 9))
        self.assertFalse(empty["hasData"])

    def test_opencode_real_spend_in_report(self):
        p = self.payload()
        p["daily"][1]["agents"] = [{"agent": "opencode", "totalCost": 30.0, "modelBreakdowns": [{"modelName": "m", "cost": 30.0}]}]
        p["sources"] = [{"tool": "opencode", "kind": "backends", "opencodeBackends": {
            "daily": {"2026-11-18": {"m": {"google": 0.5, "sub": 0.5}}},
            "providers": {"google": {"realSpend": True}, "sub": {"realSpend": False}}}}]
        self.assertAlmostEqual(digest.week_spend(p, datetime.date(2026, 11, 16), datetime.date(2026, 11, 22), {})["real"], 15.0)


# ---------------------------------------------------------------- dashboard JS

@unittest.skipUnless(NODE, "node not installed")
class DashboardJsTests(unittest.TestCase):
    def test_threshold_validation(self):
        out = run_js(["thresholdError", "parseThresholdInput"], """
          out.ok = thresholdError('80, 95');
          out.pct = thresholdError('80%, 95%');
          out.empty = thresholdError('');
          out.big = thresholdError('101');
          out.zero = thresholdError('0');
          out.dec = thresholdError('80.5');
          out.six = thresholdError('1,2,3,4,5,6');
          out.parsed = parseThresholdInput('80%, 95');
        """)
        self.assertEqual((out["ok"], out["pct"], out["empty"]), ("", "", ""))
        self.assertTrue(out["big"] and out["zero"] and out["dec"] and out["six"])
        self.assertEqual(out["parsed"], [80, 95])

    def test_model_mix_by_account_adds_up(self):
        out = run_js(["tokVec", "zeroTok", "addTok", "tokAll", "accountBucketName", "modelAccountSplit", "finishAccountSplit", "seriesColour"], """
          const days = [{period: '2026-11-24', agents: [
            {agent: 'claude', modelBreakdowns: [{modelName: 'sonnet', cost: 100}]},
            {agent: 'opencode', modelBreakdowns: [{modelName: 'gemini', cost: 12.17}]},
            {agent: 'grok', modelBreakdowns: [{modelName: 'grok-4', cost: 4.04}]},
            {agent: 'claude', modelBreakdowns: [{modelName: 'haiku', cost: 2}]}]}];
          const sessions = [
            {period: '2026-11-24', accountUuid: 'w', label: 'Work', modelBreakdowns: [{modelName: 'sonnet', cost: 30}]},
            {period: '2026-11-24', accountUuid: 'p', label: 'Personal', modelBreakdowns: [{modelName: 'sonnet', cost: 60}]},
            {period: '2026-11-24', accountUuid: 'unknown-session', label: 'Older', modelBreakdowns: [{modelName: 'sonnet', cost: 10}]},
            {period: '2026-11-24', agent: 'grok', accountUuid: 'grok-unattributed', modelBreakdowns: [{modelName: 'grok-4', cost: 4.04}]}];
          out.r = modelAccountSplit(days, sessions, '2026-11-20', '2026-11-26', 'all');
        """, consts=("ACCOUNT_BUCKET_COLOURS", "AGENT_BUCKET_NAMES", "SERIES_FIXED_COLOURS", "CLAUDE_ACCOUNT_COLOURS", "OTHER_SERIES_COLOURS"))
        m = out["r"]["models"]
        self.assertAlmostEqual(m["sonnet"]["Work"], 30)
        self.assertAlmostEqual(m["sonnet"]["Personal"], 60)
        self.assertAlmostEqual(m["sonnet"]["Unassigned"], 10)
        self.assertAlmostEqual(m["haiku"]["Unassigned"], 2)     # no session row for it
        self.assertAlmostEqual(m["gemini"]["OpenCode"], 12.17)
        self.assertAlmostEqual(m["grok-4"]["Grok"], 4.04)
        buckets = {b["name"]: b["total"] for b in out["r"]["buckets"]}
        self.assertAlmostEqual(sum(buckets.values()), 100 + 12.17 + 4.04 + 2)
        self.assertEqual(out["r"]["buckets"][-1]["name"], "Unassigned")

    def test_opencode_split_in_browser_matches_server(self):
        out = run_js(["opencodeSplit"], """
          const s = {opencodeBackends: {daily: {'2026-11-24': {m: {google: 0.5, gw: 0.5}}},
                     providers: {google: {realSpend: true}, gw: {realSpend: null}}}};
          const days = [{period: '2026-11-24', agents: [{agent: 'opencode', modelBreakdowns: [{modelName: 'm', cost: 10}, {modelName: 'x', cost: 1}]}]}];
          out.r = opencodeSplit(s, days, '2026-11-20', '2026-11-26');
        """)
        self.assertAlmostEqual(out["r"]["real"], 5)
        self.assertAlmostEqual(out["r"]["unknown"], 6)
        self.assertAlmostEqual(out["r"]["covered"], 0)

    def test_cost_split_graphic_shows_exact_figures(self):
        out = run_js(["costSplitHtml"], "out.h = costSplitHtml(878.59, 863.63, 14.96);")
        h = out["h"]
        self.assertIn("$878.59", h)
        self.assertIn("$863.63", h)
        self.assertIn("$14.96", h)
        self.assertIn(">98%<", h)
        self.assertIn(">2%<", h)
        self.assertIn("seg-ok", h)
        self.assertIn("seg-bad", h)

    def test_report_graph_uses_same_figures_as_table(self):
        out = run_js(["reportBarsHtml"], """
          out.h = reportBarsHtml([{label: 'claude', value: 763.22, display: fmt(763.22)}, {label: 'opencode', value: 17.63, display: fmt(17.63)}], 'none');
          out.e = reportBarsHtml([], 'No usage that week.');
        """)
        self.assertIn("$763.22", out["h"])
        self.assertIn("$17.63", out["h"])
        self.assertIn("No usage that week.", out["e"])

    def test_week_shift(self):
        out = run_js(["shiftWeek"], "out.a = shiftWeek('2026-11-16', -7); out.b = shiftWeek('2026-03-23', 7);")
        self.assertEqual((out["a"], out["b"]), ("2026-11-09", "2026-03-30"))


class StaticMarkupTests(unittest.TestCase):
    def test_one_save_button_in_settings(self):
        src = html()
        for gone in ('data-seat-save', 'id="pricingRegionSave"', 'id="notifSave"', 'id="planSaveBtn"', 'id="providerSaveBtn"'):
            self.assertNotIn(gone, src)
        self.assertEqual(src.count('id="settingsSaveBtn"'), 1)
        self.assertIn("/api/settings", src)

    def test_renames_and_removals(self):
        src = html()
        self.assertNotIn("Weekly digest", src)
        self.assertNotIn(">Digest<", src)
        self.assertIn("Weekly report", src)
        self.assertNotIn("<h2>Spend by account</h2>", src)
        # 1.8.7: the Accounts page became the Limits section on each tool tab.
        self.assertNotIn('data-tab="accounts"', src)
        self.assertIn("if (tab === 'accounts') tab = 'overview';", src)
        self.assertNotIn("What's actually costing you money", src)
        self.assertNotIn("Those three amounts are USD list price", src)
        self.assertNotIn('id="kpiPlan"', src)
        self.assertNotIn('data-tab="models"', src)
        self.assertIn("if (tab === 'models')", src)       # old links redirect
        self.assertNotIn('placeholder="default"', src)

    def test_shell(self):
        src = html()
        self.assertIn('class="topbar"', src)
        self.assertIn('class="refresh-button"', src)
        self.assertEqual(src.count('class="nav-icon"'), 6)   # 1.8.6 adds Value, 1.8.7 drops Accounts
        self.assertIn("max-width: 1600px", src)
        self.assertNotRegex(src, r"<script[^>]+src=\"https?://")
        self.assertNotRegex(src, r"<link[^>]+href=\"https?://")
        self.assertEqual(src.count('id="viewToggle"'), 1)


if __name__ == "__main__":
    unittest.main()
