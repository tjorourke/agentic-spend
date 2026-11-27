"""1.8.4: desktop and chat apps, Codex plan fallback, token bars stacked by
kind, and per-app cards that only show when the period has readings."""
import datetime
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

import desktop_sources  # noqa: E402
import digest  # noqa: E402
import insights  # noqa: E402
import provider_usage  # noqa: E402
import source_detection  # noqa: E402
from test_v180_features import NODE, html, run_js  # noqa: E402

ORG_A = "00000000-0000-4000-8000-00000000000a"
ORG_B = "00000000-0000-4000-8000-00000000000b"
ACCT = "00000000-0000-4000-8000-0000000000cc"


def ms(iso):
    return int(datetime.datetime.fromisoformat(iso).timestamp() * 1000)


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


class ClaudeDesktopTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.path = os.path.join(self.home, desktop_sources.CLAUDE_DESKTOP_FILE)

    def test_missing_file_is_not_found(self):
        r = desktop_sources.claude_desktop_plan_usage(home=self.home)
        self.assertFalse(r["found"])
        self.assertEqual(r["orgs"], [])

    def test_empty_file_is_found_with_no_orgs(self):
        write(self.path, "")
        r = desktop_sources.claude_desktop_plan_usage(home=self.home)
        self.assertTrue(r["found"])
        self.assertEqual(r["orgs"], [])
        self.assertEqual(r["error"], "file is empty")
        write(self.path, '{"version": 2, "samples": []}')
        r = desktop_sources.claude_desktop_plan_usage(home=self.home)
        self.assertEqual((r["orgs"], r["error"], r["lastUpdated"]), ([], None, None))

    def test_unreadable_or_unknown_version(self):
        write(self.path, "{not json")
        self.assertIn("could not read", desktop_sources.claude_desktop_plan_usage(home=self.home)["error"])
        write(self.path, '{"version": 3, "samples": [{"t": 1, "org": "x", "u": {"fh": 1, "sd": 2}}]}')
        r = desktop_sources.claude_desktop_plan_usage(home=self.home)
        self.assertEqual(r["orgs"], [])
        self.assertIn("unsupported", r["error"])

    def test_multiple_orgs_labels_and_bad_samples(self):
        samples = [
            {"t": ms("2026-11-20T10:00:00+00:00"), "org": ORG_A, "u": {"fh": 10, "sd": 4}},
            {"t": ms("2026-11-23T21:40:00+00:00"), "org": ORG_A, "u": {"fh": 0, "sd": 8}},
            {"t": ms("2026-11-22T09:00:00+00:00"), "org": ORG_B, "u": {"fh": 150, "sd": None}},
            {"t": "soon", "org": ORG_B, "u": {"fh": 1, "sd": 1}},          # bad time: skipped
            {"t": ms("2026-11-21T09:00:00+00:00"), "org": ORG_B, "u": {}},  # no figures: skipped
            "junk",
        ]
        write(self.path, json.dumps({"version": 2, "samples": samples}))
        write(os.path.join(self.home, ".claude.json"),
              json.dumps({"oauthAccount": {"organizationUuid": ORG_B, "accountUuid": ACCT}}))
        labels = {ORG_A: {"label": "Work"}, ACCT: {"label": "Personal"}}
        r = desktop_sources.claude_desktop_plan_usage(home=self.home, labels=labels)
        self.assertTrue(r["found"])
        self.assertEqual([o["label"] for o in r["orgs"]], ["Work", "Personal"])  # newest first
        a, b = r["orgs"]
        self.assertEqual(len(a["samples"]), 2)
        self.assertEqual(a["latest"], {"at": "2026-11-23T21:40:00+00:00", "fiveHourPct": 0.0, "sevenDayPct": 8.0})
        self.assertEqual(b["latest"]["fiveHourPct"], 100.0)  # clamped
        self.assertIsNone(b["latest"]["sevenDayPct"])
        self.assertEqual(r["lastUpdated"], "2026-11-23T21:40:00+00:00")
        self.assertIn("Percentages only", r["note"])

    def test_unlabelled_org_gets_short_id_not_personal_data(self):
        self.assertEqual(desktop_sources.org_label(ORG_A), "Claude organisation (00000000…)")


class ChatAppCoverageTests(unittest.TestCase):
    def test_installed_from_app_folder_names_only(self):
        root = tempfile.mkdtemp()
        os.makedirs(os.path.join(root, "Gemini.app"))
        os.makedirs(os.path.join(root, "Copilot.app"))
        rows = {r["id"]: r for r in desktop_sources.chat_app_coverage(home=root, roots=[root])}
        self.assertEqual(set(rows), {"chatgpt", "gemini-app", "perplexity", "grok-app", "ms-copilot", "cursor"})
        self.assertTrue(rows["gemini-app"]["installed"])
        self.assertTrue(rows["ms-copilot"]["installed"])
        self.assertFalse(rows["chatgpt"]["installed"])
        for r in rows.values():
            self.assertEqual(r["class"], "D")
            self.assertTrue(r["link"].startswith("https://"))

    def test_grok_bot_app_is_not_the_grok_app(self):
        root = tempfile.mkdtemp()
        os.makedirs(os.path.join(root, "Grok Bot.app"))
        rows = {r["id"]: r for r in desktop_sources.chat_app_coverage(home=root, roots=[root])}
        self.assertFalse(rows["grok-app"]["installed"])

    def test_module_never_touches_secrets(self):
        with open(os.path.join(ROOT, "desktop_sources.py")) as f:
            code = f.read().split('"""', 2)[2]  # skip the docstring, which names what it avoids
        for word in ("Cookies", "Local Storage", "keychain", "security find", "oauth:token", "auth.json"):
            self.assertNotIn(word, code)


def rollout(path, lines):
    write(path, "\n".join(json.dumps(l) for l in lines) + "\n")


def token_count(ts, pct, window=43200, resets=1_900_000_000, plan="plus"):
    return {"timestamp": ts, "type": "event_msg", "payload": {
        "type": "token_count", "info": {"total_token_usage": {"input_tokens": 1}},
        "rate_limits": {"primary": {"used_percent": pct, "window_minutes": window, "resets_at": resets},
                        "secondary": None, "credits": {"has_credits": False, "balance": "0"}, "plan_type": plan}}}


class CodexFallbackTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_newest_reading_across_files(self):
        rollout(os.path.join(self.dir, "2026/11/09/rollout-a.jsonl"), [
            {"timestamp": "2026-11-09T10:00:00Z", "type": "session_meta", "payload": {}},
            token_count("2026-11-09T10:01:00Z", 1.0),
            token_count("2026-11-09T10:05:00Z", 2.0),
        ])
        rollout(os.path.join(self.dir, "2026/11/10/rollout-b.jsonl"), [
            token_count("2026-11-10T18:20:40Z", 3.0),
            {"timestamp": "2026-11-10T18:21:00Z", "type": "event_msg", "payload": {"type": "token_count", "rate_limits": None}},
        ])
        s = provider_usage.codex_snapshot_from_sessions(self.dir)
        self.assertEqual(s["source"], "session-log")
        self.assertEqual(s["fetchedAt"], "2026-11-10T18:20:40Z")  # when Codex wrote it, not now
        self.assertEqual(s["primary"]["percent"], 3.0)
        self.assertEqual(s["primary"]["windowMins"], 43200)
        self.assertEqual(s["planType"], "plus")
        self.assertIsNone(s["secondary"])

    def test_no_samples_is_none(self):
        rollout(os.path.join(self.dir, "rollout-x.jsonl"), [{"timestamp": "t", "type": "session_meta", "payload": {}}])
        self.assertIsNone(provider_usage.codex_snapshot_from_sessions(self.dir))
        self.assertIsNone(provider_usage.codex_snapshot_from_sessions(os.path.join(self.dir, "missing")))

    def test_live_snapshot_falls_back_when_app_server_unavailable(self):
        rollout(os.path.join(self.dir, "rollout-c.jsonl"), [token_count("2026-11-10T18:20:40Z", 5.0)])
        with mock.patch.object(provider_usage, "fetch_codex_rate_limits", return_value=None), \
                mock.patch.object(provider_usage, "CODEX_SESSIONS_DIR", self.dir):
            s = provider_usage.get_codex_live_snapshot()
        self.assertEqual((s["source"], s["primary"]["percent"]), ("session-log", 5.0))

    def test_app_server_result_is_labelled(self):
        result = {"rateLimits": {"primary": {"usedPercent": 7, "windowDurationMins": 300, "resetsAt": 1_900_000_000}}}
        with mock.patch.object(provider_usage, "fetch_codex_rate_limits", return_value=result):
            s = provider_usage.get_codex_live_snapshot()
        self.assertEqual(s["source"], "app-server")

    def test_stale_session_log_reading_gives_no_insight(self):
        now = datetime.datetime(2026, 11, 27, 8, 0, tzinfo=datetime.timezone.utc)
        base = {"source": "session-log", "planType": "free", "fetchedAt": "2026-11-10T18:20:40Z",
                "primary": {"percent": 3.0, "windowMins": 43200}}
        past = dict(base, primary=dict(base["primary"], resetsAt="2026-11-20T00:00:00+00:00"))
        future = dict(base, primary=dict(base["primary"], resetsAt="2026-12-10T15:40:46+00:00"))
        self.assertEqual(insights._codex_insights(past, [], now), [])
        self.assertEqual(len(insights._codex_insights(future, [], now)), 1)

    def test_codex_detected_from_dot_codex_alone(self):
        home = tempfile.mkdtemp()
        os.makedirs(os.path.join(home, ".codex"))
        det = {d["id"]: d for d in source_detection.detect(home=home, path="", app_roots_list=[])}
        self.assertTrue(det["codex"]["installed"])


class DigestTokenSplitTests(unittest.TestCase):
    # Real figures from ccusage for claude-sonnet-5, 16-22 Nov 2026, split over two days.
    SONNET = {"input": 16594, "output": 5889415, "cacheWrite": 15526577, "cacheRead": 2954106252}

    def payload(self):
        def mb(name, i, o, w, r, cost):
            return {"modelName": name, "inputTokens": i, "outputTokens": o, "cacheCreationTokens": w,
                    "cacheReadTokens": r, "cost": cost}
        d1 = [mb("claude-sonnet-5", 10000, 3000000, 8000000, 1500000000, 400.00), mb("gemini-x", 5, 6, 0, 7, 0.0)]
        d2 = [mb("claude-sonnet-5", 6594, 2889415, 7526577, 1454106252, 309.93)]
        return {"daily": [
            {"period": "2026-11-18", "totalCost": 400.0, "modelBreakdowns": d1,
             "agents": [{"agent": "claude", "totalCost": 400.0, "modelBreakdowns": d1[:1]},
                        {"agent": "opencode", "totalCost": 0.0, "modelBreakdowns": d1[1:]}]},
            {"period": "2026-11-20", "totalCost": 309.93, "modelBreakdowns": d2,
             "agents": [{"agent": "claude", "totalCost": 309.93, "modelBreakdowns": d2}]},
        ], "accountSessions": [
            {"period": "2026-11-18", "project": "/p", "totalCost": 400.0, "modelBreakdowns": d1[:1]},
        ]}

    def test_sonnet_split_matches_ccusage(self):
        d = digest.build_digest(self.payload(), today=datetime.date(2026, 11, 27), week=datetime.date(2026, 11, 16))
        sonnet = next(m for m in d["topModels"] if m["model"] == "claude-sonnet-5")
        self.assertEqual(sonnet["tok"], self.SONNET)
        self.assertEqual(sonnet["cost"], 709.93)
        self.assertEqual(sonnet["tokens"], self.SONNET["input"] + self.SONNET["output"])

    def test_every_row_has_a_split_that_adds_up(self):
        d = digest.build_digest(self.payload(), today=datetime.date(2026, 11, 27), week=datetime.date(2026, 11, 16))
        week = d["tokens"]["split"]
        self.assertEqual(sum(sum(x["tok"].values()) for x in d["days"]), sum(week.values()))
        self.assertEqual(sum(sum(a["tok"].values()) for a in d["agents"]), sum(week.values()))
        self.assertEqual(d["topProjects"][0]["tok"]["cacheRead"], 1500000000)
        # an agent with tokens but no cost (free model) is still listed
        self.assertIn("opencode", [a["agent"] for a in d["agents"]])


@unittest.skipUnless(NODE, "node not installed")
class TokenBarJsTests(unittest.TestCase):
    CONSTS = ("TOK_KINDS", "TOK_KIND_LABELS", "TOK_KIND_COLOURS")
    FNS = ["zeroTok", "addTok", "tokAll", "fmtExact", "tokStackTitle", "tokStackBarHtml", "tokLegendHtml", "rowTok", "reportBarsHtml"]

    def test_stack_segments_in_order_with_exact_hover(self):
        out = run_js(self.FNS, """
          const t = {input: 16594, output: 5889415, cacheWrite: 15526577, cacheRead: 2954106252};
          out.bar = tokStackBarHtml(t, tokAll(t) * 2, 'claude-sonnet-5');
          out.title = tokStackTitle('claude-sonnet-5', t, 709.93);
          out.legend = tokLegendHtml(t);
          out.empty = tokStackBarHtml({input: 0, output: 0, cacheWrite: 0, cacheRead: 0}, 10, 'x');
        """, self.CONSTS)
        bar = out["bar"]
        order = [bar.index("tok-" + k) for k in ("input", "output", "cacheWrite", "cacheRead")]
        self.assertEqual(order, sorted(order))
        self.assertIn("Cache read: 2,954,106,252 tokens", bar)
        self.assertIn("Input: 16,594 tokens", bar)
        self.assertIn('style="width:50.00%"', bar)  # half of max
        self.assertIn("Output: 5,889,415", out["title"])
        self.assertIn("All tokens: 2,975,538,838", out["title"])
        self.assertIn("$709.93", out["title"])
        for label in ("Input", "Output", "Cache write", "Cache read"):
            self.assertIn(label, out["legend"])
        self.assertNotIn("tok-seg", out["empty"])

    def test_report_bars_stack_only_when_asked(self):
        out = run_js(self.FNS, """
          const rows = [{label: 'a', value: 1, display: 'A', tok: {input: 1, output: 2, cacheWrite: 3, cacheRead: 4}}];
          out.plain = reportBarsHtml(rows, 'none');
          out.stacked = reportBarsHtml(rows, 'none', {stack: true});
        """, self.CONSTS)
        self.assertNotIn("tok-stack", out["plain"])
        self.assertIn("rbar-fill", out["plain"])
        self.assertIn("tok-stack", out["stacked"])
        self.assertIn("tok-legend", out["stacked"])


@unittest.skipUnless(NODE, "node not installed")
class PeriodGateJsTests(unittest.TestCase):
    FNS = ["localDay", "inPeriod", "claudeDesktopInPeriod", "codexReadingInPeriod", "coverageRows"]

    def test_claude_desktop_only_with_readings_in_period(self):
        out = run_js(self.FNS, """
          const cd = {found: true, note: 'Percentages only', orgs: [
            {org: 'a', label: 'Work', samples: [
              {at: '2026-11-20T12:00:00', fiveHourPct: 10, sevenDayPct: 4},
              {at: '2026-11-23T12:00:00', fiveHourPct: 0, sevenDayPct: 8}]},
            {org: 'b', label: 'Personal', samples: [{at: '2026-10-30T12:00:00', fiveHourPct: 5, sevenDayPct: 1}]}]};
          out.week = claudeDesktopInPeriod(cd, '2026-11-16', '2026-11-22');
          out.both = claudeDesktopInPeriod(cd, '2026-10-01', '2026-11-30');
          out.none = claudeDesktopInPeriod(cd, '2026-11-25', '2026-11-27');
          out.missing = claudeDesktopInPeriod({found: false, orgs: []}, null, null);
        """)
        self.assertEqual([o["label"] for o in out["week"]["orgs"]], ["Work"])
        self.assertEqual(out["week"]["orgs"][0]["count"], 1)
        self.assertEqual(len(out["both"]["orgs"]), 2)
        self.assertEqual(out["both"]["orgs"][0]["peakFiveHour"], 10)
        self.assertIsNone(out["none"])
        self.assertIsNone(out["missing"])

    def test_codex_card_needs_usage_or_a_logged_reading(self):
        out = run_js(self.FNS, """
          const live = {source: 'app-server', fetchedAt: '2026-11-27T08:00:00', planType: 'free',
                        primary: {percent: 3, windowMins: 43200, resetsAt: '2026-12-10T15:40:46Z'}};
          const hist = [{time: '2026-11-10T18:20:40', percent: 3, windowMins: 43200}];
          out.pollOnly = codexReadingInPeriod(live, hist, '2026-11-21', '2026-11-27', false);
          out.withUse = codexReadingInPeriod(live, hist, '2026-11-21', '2026-11-27', true);
          out.history = codexReadingInPeriod(live, hist, '2026-11-09', '2026-11-10', false);
          out.useNoReading = codexReadingInPeriod(live, hist, '2026-10-01', '2026-10-07', true);
        """)
        self.assertIsNone(out["pollOnly"])
        self.assertEqual(out["withUse"]["source"], "app-server")
        self.assertEqual(out["history"]["source"], "history")
        self.assertEqual(out["history"]["at"], "2026-11-10T18:20:40")
        self.assertIsNone(out["useNoReading"])

    def test_coverage_groups_rows(self):
        out = run_js(self.FNS, """
          out.r = coverageRows({
            ccusage: [{id: 'claude', label: 'Claude Code', tier: 'first-class'}, {id: 'codex', label: 'Codex', tier: 'beta'}],
            periodAgents: ['claude'],
            claudeDesktop: {found: true}, claudeDesktopPeriod: null,
            codexInstalled: true, codexPeriod: null, codexSource: 'session-log',
            extra: [{id: 'grok-bot', label: 'Grok Bot', kind: 'B', what: 'x', active: true}],
            chatApps: [{id: 'chatgpt', label: 'ChatGPT', installed: false, link: 'https://a'},
                       {id: 'cursor', label: 'Cursor', installed: true, link: 'https://b'}],
          });
        """)
        r = out["r"]
        self.assertEqual([x["id"] for x in r["active"]], ["claude", "grok-bot"])
        self.assertEqual([x["id"] for x in r["idle"]], ["codex", "claude-desktop", "codex-plan"])
        self.assertEqual([x["id"] for x in r["noLocal"]], ["cursor", "chatgpt"])  # installed first
        self.assertTrue(all(x["kind"] == "D" for x in r["noLocal"]))


class StaticWiringTests(unittest.TestCase):
    def test_cards_hidden_when_nothing_in_period(self):
        h = html()
        body = re.search(r"^function renderAppLimitCards\([\s\S]*?\n}\n", h, re.M).group(0)
        self.assertIn("box.style.display = cards.length ? '' : 'none'", body)
        self.assertIn('id="appLimitCards"', h)
        self.assertIn('id="coverageCard"', h)
        self.assertIn("renderAppLimitCards(cutoff, windowEnd)", h)
        self.assertIn("renderCoverage(cutoff, windowEnd)", h)

    def test_digest_and_model_mix_use_stacked_bars_in_tokens_view(self):
        h = html()
        dig = re.search(r"^function renderDigest\([\s\S]*?\n}\n", h, re.M).group(0)
        self.assertEqual(dig.count("stackOn ? stackOpts : undefined"), 4)  # projects, models, agents, days
        mix = re.search(r"^function renderModelMix\([\s\S]*?\n}\n", h, re.M).group(0)
        self.assertIn("metric === 'tokens' && view === 'graph'", mix)
        self.assertIn("tokStackBarHtml(r.tok", mix)

    def test_session_log_codex_reading_never_called_live(self):
        h = html()
        self.assertIn("cx.source === 'session-log' ? `from Codex's session log", h)
        self.assertIn("Not live: the codex app-server was not available.", h)

    def test_readme_documents_trust_and_opt_in_telemetry(self):
        with open(os.path.join(ROOT, "README.md")) as f:
            readme = f.read()
        self.assertIn("brew trust tjorourke/agentic-spend", readme)
        self.assertIn('"github.copilot.chat.otel.exporterType": "file"', readme)
        self.assertIn('"target": "local"', readme)
        self.assertIn("## Version 1.8.4", readme)


if __name__ == "__main__":
    unittest.main()
