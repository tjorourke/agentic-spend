"""1.8.5: allowance history, seat value, usage archive."""
import datetime
import json
import os
import re
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

import allowance_history  # noqa: E402
import digest  # noqa: E402
import seat_prices  # noqa: E402
import usage_archive  # noqa: E402
from test_v180_features import html, run_js  # noqa: E402

UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
ACCT = "00000000-0000-4000-8000-0000000000aa"


def _insight(week_pct=11, week_state="current", at="2026-11-20T11:00:00+00:00", sess_pct=4):
    return {"key": "claude:" + ACCT, "tool": "claude", "label": "Work", "pct": float(week_pct),
            "resetAt": "2026-11-27T15:00:00+00:00", "windowSeconds": 604800,
            "session": {"percent": sess_pct, "resetsAt": "2026-11-20T14:00:00+00:00", "state": "current", "asOf": at},
            "week": {"percent": week_pct, "resetsAt": "2026-11-20T15:00:00+00:00" if week_state == "reset" else "2026-11-27T15:00:00+00:00",
                     "state": week_state, "asOf": at},
            "credits": {"kind": "used", "value": 0, "currency": "USD", "exponent": 2}}


MU = [{"accountUuid": ACCT, "label": "Work", "email": "someone@example.com",
       "usageCredits": {"currency": "USD", "exponent": 2, "limitMinor": 4000, "usedMinor": 1250}}]


# ------------------------------------------------------------ allowance history

class AllowanceHistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "allowance-history.jsonl")
        allowance_history.reset_cache()

    def lines(self):
        with open(self.path) as f:
            return [json.loads(l) for l in f if l.strip()]

    def test_record_shape_and_no_email(self):
        allowance_history.record([_insight()], MU, now=NOW, target=self.path)
        recs = self.lines()
        self.assertEqual(len(recs), 1)
        r = recs[0]
        self.assertEqual((r["key"], r["weeklyPct"], r["sessionPct"]), ("claude:" + ACCT, 11.0, 4.0))
        self.assertEqual((r["creditsUsed"], r["creditsLimit"], r["creditsCurrency"]), (12.5, 40.0, "USD"))
        self.assertEqual(r["at"], "2026-11-20T11:00:00+00:00")
        raw = open(self.path).read()
        self.assertNotIn("@", raw)
        self.assertNotIn("email", raw)

    def test_dedupe_same_reading_and_heartbeat(self):
        allowance_history.record([_insight()], MU, now=NOW, target=self.path)
        allowance_history.record([_insight()], MU, now=NOW, target=self.path)      # identical reading
        allowance_history.record([_insight(at="2026-11-20T11:30:00+00:00")], MU, now=NOW, target=self.path)  # same figures, 30 min later
        self.assertEqual(len(self.lines()), 1)
        allowance_history.record([_insight(at="2026-11-20T14:30:00+00:00")], MU, now=NOW, target=self.path)  # 3.5 h later: heartbeat
        allowance_history.record([_insight(week_pct=12, at="2026-11-20T14:40:00+00:00")], MU, now=NOW, target=self.path)  # changed
        self.assertEqual([r["weeklyPct"] for r in self.lines()], [11.0, 11.0, 12.0])

    def test_older_reading_is_ignored(self):
        allowance_history.record([_insight(at="2026-11-20T11:00:00+00:00")], MU, now=NOW, target=self.path)
        allowance_history.record([_insight(week_pct=9, at="2026-11-20T10:00:00+00:00")], MU, now=NOW, target=self.path)
        self.assertEqual(len(self.lines()), 1)

    def test_dedupe_survives_restart(self):
        allowance_history.record([_insight()], MU, now=NOW, target=self.path)
        allowance_history.reset_cache()  # a new server process
        allowance_history.record([_insight()], MU, now=NOW, target=self.path)
        self.assertEqual(len(self.lines()), 1)

    def test_reset_window_is_stamped_now_with_next_reset(self):
        p = _insight(week_pct=0, week_state="reset", at="2026-11-19T09:00:00+00:00")
        p["session"]["state"] = "reset"
        r = allowance_history.readings_from([p], MU, now=NOW)[0]
        self.assertEqual(r["at"], "2026-11-20T12:00:00+00:00")
        self.assertEqual(r["weeklyResetsAt"], "2026-11-27T15:00:00+00:00")
        self.assertIsNone(r["sessionResetsAt"])

    def test_codex_and_grok_without_session(self):
        recs = allowance_history.readings_from([
            {"key": "codex:primary", "tool": "codex", "label": "Codex", "pct": 3.0, "resetAt": "2026-12-10T15:40:46+00:00",
             "windowSeconds": 2592000, "forecast": {"asOf": "2026-11-10T18:20:40+00:00"}},
            {"key": "grok-bot:x", "tool": "grok-bot", "label": "Grok Bot", "pct": 62.8, "resetAt": "2026-11-29T09:06:55+00:00",
             "windowSeconds": 604800, "forecast": {"asOf": "2026-11-20T10:00:00+00:00"},
             "credits": {"kind": "remaining", "value": 10.6, "currency": "USD"}},
        ], None, now=NOW)
        self.assertEqual([(r["key"], r["weeklyPct"], r["sessionPct"], r["creditsUsed"]) for r in recs],
                         [("codex:primary", 3.0, None, None), ("grok-bot:x", 62.8, None, None)])
        self.assertEqual(recs[0]["windowSeconds"], 2592000)

    def test_load_filters_by_time(self):
        allowance_history.record([_insight(at="2026-11-18T11:00:00+00:00")], MU, now=NOW, target=self.path)
        allowance_history.record([_insight(week_pct=20, at="2026-11-25T11:00:00+00:00")], MU, now=NOW, target=self.path)
        got = allowance_history.load("2026-11-16", "2026-11-23", target=self.path)
        self.assertEqual([r["weeklyPct"] for r in got], [11.0])

    def test_server_records_on_every_plan_read(self):
        src = open(os.path.join(ROOT, "server.py")).read()
        self.assertIn("_record_allowance(payload)", src)
        self.assertGreaterEqual(src.count("_record_allowance(payload)"), 2)  # full refresh and cached reads
        self.assertIn("allowance_history.record(plans", src)              # the 10-minute sampler


class AllowanceDigestTests(unittest.TestCase):
    TZ = UTC

    def payload(self):
        return {"daily": [{"period": "2026-11-17", "totalCost": 1.0, "modelBreakdowns": []}],
                "maxUsage": [{"accountUuid": ACCT, "label": "Work"}],
                "usageHistory": {"claude": [{"time": "2026-11-17T09:00:00+00:00", "accountUuid": ACCT,
                                             "fiveHourPercent": 30, "sevenDayPercent": 5}]}}

    def allowance(self):
        return [
            {"at": "2026-11-18T10:00:00+00:00", "key": "claude:" + ACCT, "tool": "claude", "label": "Work",
             "weeklyPct": 20.0, "sessionPct": 50.0, "windowSeconds": 604800, "creditsUsed": 3.0, "creditsLimit": 40.0, "creditsCurrency": "USD"},
            {"at": "2026-11-21T10:00:00+00:00", "key": "claude:" + ACCT, "tool": "claude", "label": "Work",
             "weeklyPct": 41.0, "sessionPct": 12.0, "windowSeconds": 604800},
            {"at": "2026-11-25T10:00:00+00:00", "key": "claude:" + ACCT, "tool": "claude", "label": "Work",
             "weeklyPct": 90.0, "sessionPct": 90.0, "windowSeconds": 604800},  # next week: excluded
        ]

    def test_series_and_peaks(self):
        d = digest.build_digest(self.payload(), today=datetime.date(2026, 11, 24), tz=self.TZ,
                                week=datetime.date(2026, 11, 16), allowance=self.allowance())
        s = d["allowance"]
        self.assertEqual(len(s), 1)
        self.assertEqual(s[0]["key"], "claude:" + ACCT)
        self.assertEqual(s[0]["label"], "Claude: Work")
        self.assertEqual([p["weekly"] for p in s[0]["points"]], [5.0, 20.0, 41.0])  # status line sample merged in
        self.assertEqual((s[0]["peakWeeklyPct"], s[0]["peakSessionPct"]), (41.0, 50.0))
        self.assertEqual(s[0]["credits"]["used"], 3.0)
        self.assertEqual(d["plans"][0]["peakPct"], 41.0)  # the peak table now sees allowance readings too

    def test_allowance_only_account_gets_a_peak_row(self):
        rec = [{"at": "2026-11-18T10:00:00+00:00", "key": "grok-bot:zz", "tool": "grok-bot", "label": "Grok Bot",
                "weeklyPct": 33.0, "windowSeconds": 604800}]
        d = digest.build_digest({"daily": []}, today=datetime.date(2026, 11, 24), tz=self.TZ,
                                week=datetime.date(2026, 11, 16), allowance=rec)
        self.assertEqual([(p["key"], p["peakPct"]) for p in d["plans"]], [("grok-bot:zz", 33.0)])

    def test_no_readings_keeps_empty_lists(self):
        d = digest.build_digest({"daily": []}, today=datetime.date(2026, 11, 24), tz=self.TZ,
                                week=datetime.date(2026, 11, 16), allowance=[])
        self.assertEqual((d["allowance"], d["plans"]), ([], []))

    def test_chart_svg_and_spark(self):
        out = run_js(["allowanceChartSvg", "allowanceSparkSvg", "allowancePoints", "formatPct"], """
          const pts = [{at: '2026-11-18T10:00:00Z', weekly: 20, session: 50}, {at: '2026-11-21T10:00:00Z', weekly: 41, session: null}];
          out.svg = allowanceChartSvg(pts, '2026-11-16T00:00:00Z', '2026-11-23T00:00:00Z', {});
          out.empty = allowanceChartSvg([], '2026-11-16T00:00:00Z', '2026-11-16T00:00:00Z', {});
          out.spark = allowanceSparkSvg(pts, {label: 'Work'});
          out.sparkOne = allowanceSparkSvg(pts.slice(0, 1), {});
          out.pts = allowancePoints([{key: 'a', at: '2026-11-18T10:00:00Z', weeklyPct: 3, sessionPct: 1},
                                     {key: 'b', at: '2026-11-18T10:00:00Z', weeklyPct: 9},
                                     {key: 'a', at: '2026-11-10T10:00:00Z', weeklyPct: 1}], 'a', '2026-11-15T00:00:00Z', null);
        """)
        self.assertIn("<polyline", out["svg"])
        self.assertEqual(out["svg"].count('class="al-dot al-weekly"'), 2)
        self.assertEqual(out["svg"].count('class="al-dot al-session"'), 1)
        self.assertIn("Weekly 41%", out["svg"])
        self.assertEqual(out["svg"].count("<text"), 3 + 7)  # 0/50/100 and one label a day
        self.assertEqual(out["empty"], "")
        self.assertIn("now 41%, peak 41%", out["spark"])
        self.assertEqual(out["sparkOne"], "")
        self.assertEqual(out["pts"], [{"at": "2026-11-18T10:00:00Z", "weekly": 3, "session": 1}])

    def test_report_replaces_empty_message_when_data(self):
        h = html()
        self.assertIn("Plan limit through the week", h)
        self.assertIn("allowanceHtml && !plans.length ? '' : pick(planTable, planGraph)", h)


# ------------------------------------------------------------ seat value

class SeatValueTests(unittest.TestCase):
    TABLE = {"rows": [
        {"planId": "claude/team-premium", "plan": "Claude Team, Premium seat", "billing": "monthly", "currency": "USD", "perMonth": 125},
        {"planId": "xai/premium", "plan": "X Premium", "billing": "monthly", "currency": "GBP", "perMonth": 26},
        {"planId": "xai/premium", "plan": "X Premium", "billing": "monthly", "currency": "USD", "perMonth": 30},
    ]}
    SESSIONS = [{"accountUuid": ACCT, "period": "2026-11-03", "totalCost": 1000.0},
                {"accountUuid": ACCT, "period": "2026-11-17", "totalCost": 900.0},
                {"accountUuid": ACCT, "period": "2026-10-30", "totalCost": 5000.0}]
    DET = [{"key": ACCT, "provider": "claude", "planId": "claude/team-premium", "planLabel": "Team premium", "detected": True}]

    def tv(self, prices=None, **kw):
        return seat_prices.team_value(MU, self.SESSIONS, prices or {}, today=datetime.date(2026, 11, 27),
                                      detections=self.DET, table=self.TABLE, **kw)[0]

    def test_month_ratio_and_credits(self):
        e = self.tv()
        self.assertEqual(e["apiEquivalentUsd"], 1900.0)
        self.assertEqual((e["seat"]["source"], e["seatForPeriod"], e["periodFraction"]), ("default", 125.0, 1.0))
        self.assertEqual(e["multiple"], 15.2)
        self.assertEqual(e["credits"], {"used": 12.5, "limit": 40.0, "currency": "USD", "kind": "current reading"})

    def test_your_price_overrides_list(self):
        e = self.tv({ACCT: {"amount": 100, "currency": "USD", "period": "month"}})
        self.assertEqual((e["seat"]["source"], e["multiple"]), ("user", 19.0))

    def test_week_is_prorated(self):
        e = self.tv(start=datetime.date(2026, 11, 16), end=datetime.date(2026, 11, 22))
        self.assertEqual(e["apiEquivalentUsd"], 900.0)
        self.assertAlmostEqual(e["periodFraction"], 7 / 30, places=4)
        self.assertEqual(e["seatForPeriod"], round(125 * 7 / 30, 2))
        self.assertEqual(e["multiple"], round(900 / (125 * 7 / 30), 1))

    def test_period_fraction(self):
        f = seat_prices.period_fraction
        self.assertEqual(f(datetime.date(2026, 11, 1), datetime.date(2026, 11, 27)), 1.0)
        self.assertAlmostEqual(f(datetime.date(2026, 2, 2), datetime.date(2026, 2, 8)), 7 / 28)

    def test_gbp_seat_ratio_only_against_us_list(self):
        det = [{"key": "grok", "provider": "xai", "planId": "xai/premium", "billingCurrency": "GBP", "label": "Grok"}]
        daily = [{"period": "2026-11-05", "agents": [{"agent": "grok", "totalCost": 60.0}]}]
        e = [x for x in seat_prices.team_value([], [], {}, today=datetime.date(2026, 11, 27), detections=det,
                                               daily=daily, table=self.TABLE) if x["key"] == "grok"][0]
        self.assertEqual((e["seat"]["currency"], e["multiple"]), ("GBP", None))
        self.assertEqual((e["usdList"]["amount"], e["multipleUsdList"]), (30.0, 2.0))

    def test_week_card_only_seat_and_usage(self):
        payload = {"maxUsage": MU, "accountSessions": self.SESSIONS, "planDetection": self.DET + [
                       {"key": "codex", "provider": "openai", "planId": None, "label": "Codex"}],
                   "planPrices": self.TABLE, "daily": [], "seatPrices": {}}
        rows = digest.seat_value_week(payload, datetime.date(2026, 11, 16), datetime.date(2026, 11, 22))
        self.assertEqual([r["key"] for r in rows], [ACCT])
        rows = digest.seat_value_week(payload, datetime.date(2026, 11, 23), datetime.date(2026, 11, 29))
        self.assertEqual(rows, [])  # no usage that week

    def test_js_rows_and_card(self):
        out = run_js(["seatValueRows", "seatValueCardHtml"], """
          const list = [
            {key: 'a', label: 'Work', apiEquivalentUsd: 1900, seat: {amount: 125, currency: 'USD', source: 'default'}, multiple: 15.2, periodFraction: 1, seatForPeriod: 125, credits: {used: 12.5, limit: 40, currency: 'USD'}},
            {key: 'b', label: 'Mine', apiEquivalentUsd: 50, seat: {amount: 90, currency: 'GBP', source: 'user'}, multiple: null, multipleUsdList: null, periodFraction: 1},
            {key: 'c', label: 'Free', apiEquivalentUsd: 9, seat: {amount: 0, currency: 'USD', source: 'default'}},
            {key: 'd', label: 'Idle', apiEquivalentUsd: 0, seat: {amount: 20, currency: 'USD', source: 'default'}},
            {key: 'e', label: 'NoPlan', apiEquivalentUsd: 40, seat: null},
            {key: 'g', label: 'Grok', apiEquivalentUsd: 60, seat: {amount: 26, currency: 'GBP', source: 'default'}, multiple: null, multipleUsdList: 2, usdList: {amount: 30}, periodFraction: 1},
          ];
          out.rows = seatValueRows(list).map(r => [r.key, r.priceLabel, r.ratio]);
          const wk = seatValueRows([Object.assign({}, list[0], {apiEquivalentUsd: 900, periodFraction: 0.2333, seatForPeriod: 29.17, multiple: 30.9})]);
          out.wk = seatValueCardHtml(wk, {title: 'Seat value for the week', prorated: true, days: '7', daysInMonth: '30'});
          out.month = seatValueCardHtml(seatValueRows(list), {title: 'Seat value this month'});
          out.none = seatValueCardHtml([], {});
        """)
        self.assertEqual(out["rows"], [["a", "list price", 15.2], ["g", "list price", 2], ["b", "your price", None]])
        self.assertIn("~31×", out["wk"])
        self.assertIn("× 7/30", out["wk"])
        self.assertIn("Seat cost for the week", out["wk"])
        self.assertIn("~15×", out["month"])
        self.assertIn("your price", out["month"])
        self.assertIn("no ratio", out["month"])
        self.assertIn("US list", out["month"])
        self.assertIn("12.50 of", out["month"])
        self.assertEqual(out["none"], "")

    def test_card_wiring(self):
        h = html()
        self.assertIn('id="seatValueCard"', h)
        self.assertIn("renderSeatValueCard();", h)
        self.assertIn("box.style.display = rows.length ? '' : 'none';", h)
        self.assertIn('data-report-card="seat-value"', h)
        self.assertIn("data-seat-amount", h)  # the Settings field for your price


# ------------------------------------------------------------ usage archive

def _mb(model, i, o, cw, cr, cost):
    return {"modelName": model, "inputTokens": i, "outputTokens": o, "cacheCreationTokens": cw,
            "cacheReadTokens": cr, "cost": cost}


def _daily(day, mult=1, agent="claude"):
    mb = _mb("claude-sonnet-5", 10 * mult, 20 * mult, 30 * mult, 40 * mult, 1.5 * mult)
    return {"period": day, "agent": "all", "inputTokens": 10 * mult, "outputTokens": 20 * mult,
            "cacheCreationTokens": 30 * mult, "cacheReadTokens": 40 * mult, "totalTokens": 100 * mult,
            "totalCost": 1.5 * mult, "modelBreakdowns": [mb], "modelsUsed": ["claude-sonnet-5"],
            "metadata": {"agents": [agent]}, "agents": [{"agent": agent, "totalCost": 1.5 * mult, "modelBreakdowns": [mb]}]}


def _session(day, mult=1):
    return {"period": day, "project": "/p/one", "accountUuid": ACCT, "label": "someone@example.com", "agent": "claude",
            "totalCost": 1.5 * mult, "totalTokens": 100 * mult, "costKnown": True, "sessions": 2,
            "note": "free text", "modelBreakdowns": [_mb("claude-sonnet-5", 10 * mult, 20 * mult, 30 * mult, 40 * mult, 1.5 * mult)]}


class UsageArchiveTests(unittest.TestCase):
    TODAY = datetime.date(2026, 11, 3)

    def setUp(self):
        self.base = tempfile.mkdtemp()

    def test_write_complete_days_only_and_split_by_month(self):
        daily = [_daily("2026-10-31"), _daily("2026-11-01"), _daily("2026-11-02"), _daily("2026-11-03")]
        sessions = [_session(d["period"]) for d in daily]
        res = usage_archive.write(daily, sessions, today=self.TODAY, base=self.base)
        self.assertEqual(res["months"], ["2026-10", "2026-11"])
        nov = json.load(open(os.path.join(self.base, "2026-11.json")))
        self.assertEqual(sorted(nov["days"]), ["2026-11-01", "2026-11-02"])  # today is not complete
        s = nov["days"]["2026-11-01"]["sessions"][0]
        self.assertNotIn("label", s)
        self.assertNotIn("note", s)
        raw = open(os.path.join(self.base, "2026-11.json")).read()
        self.assertNotIn("@", raw)
        self.assertNotIn("free text", raw)

    def test_due_once_a_day(self):
        self.assertTrue(usage_archive.due(self.TODAY, base=self.base))
        usage_archive.write([_daily("2026-11-01")], [], today=self.TODAY, base=self.base)
        self.assertFalse(usage_archive.due(self.TODAY, base=self.base))
        self.assertTrue(usage_archive.due(self.TODAY + datetime.timedelta(days=1), base=self.base))

    def test_never_shrinks_a_day(self):
        usage_archive.write([_daily("2026-11-01", 5)], [], today=self.TODAY, base=self.base)
        usage_archive.write([_daily("2026-11-01", 2)], [], today=self.TODAY, base=self.base)  # transcripts partly gone
        self.assertEqual(usage_archive.load(self.base)["2026-11-01"]["daily"]["totalTokens"], 500)
        usage_archive.write([_daily("2026-11-01", 7)], [], today=self.TODAY, base=self.base)
        self.assertEqual(usage_archive.load(self.base)["2026-11-01"]["daily"]["totalTokens"], 700)

    def test_merge_live_wins_and_fills_gaps_without_double_count(self):
        usage_archive.write([_daily("2026-10-30", 3), _daily("2026-10-31", 3), _daily("2026-11-01", 3)],
                            [_session("2026-10-30", 3), _session("2026-10-31", 3), _session("2026-11-01", 3)],
                            today=self.TODAY, base=self.base)
        # Later: the October transcripts are gone; 1 Nov is still live with different figures.
        live_daily = [_daily("2026-11-01", 1), _daily("2026-11-02", 1)]
        live_sessions = [_session("2026-11-01", 1), _session("2026-11-02", 1)]
        d, s, filled = usage_archive.merge(live_daily, live_sessions, usage_archive.load(self.base),
                                           {ACCT: {"label": "Work"}})
        self.assertEqual(filled, ["2026-10-30", "2026-10-31"])
        self.assertEqual([r["period"] for r in d], ["2026-10-30", "2026-10-31", "2026-11-01", "2026-11-02"])
        by_day = {r["period"]: r for r in d}
        self.assertEqual(by_day["2026-11-01"]["totalTokens"], 100)         # live wins
        self.assertNotIn("archived", by_day["2026-11-01"])
        self.assertTrue(by_day["2026-10-30"]["archived"])
        self.assertEqual(sum(r["totalTokens"] for r in d), 300 + 300 + 100 + 100)  # each day once
        self.assertEqual(len(s), 4)
        self.assertEqual({r["label"] for r in s if r.get("archived")}, {"Work"})
        self.assertIs(d[2], live_daily[0])  # live rows are passed through untouched

    def test_merge_without_archive_returns_live_unchanged(self):
        live = [_daily("2026-11-01")]
        d, s, filled = usage_archive.merge(live, [], {}, {})
        self.assertEqual((d, s, filled), (live, [], []))

    def test_live_ccusage_totals_unchanged_by_merge(self):
        # Totals for live days must still match ccusage exactly.
        live = [_daily("2026-11-01", 4), _daily("2026-11-02", 9)]
        usage_archive.write([_daily("2026-11-01", 99)], [], today=self.TODAY, base=self.base)
        d, _, _ = usage_archive.merge(live, [], usage_archive.load(self.base), {})
        self.assertEqual(d, live)

    def test_server_wiring(self):
        src = open(os.path.join(ROOT, "server.py")).read()
        self.assertIn("_archive_and_merge(daily_rows, account_sessions, labels)", src)
        self.assertIn('"archive": archive_info', src)
        self.assertIn("_archive_if_due()", src)

    def test_archive_note(self):
        out = run_js(["archiveDaysIn", "archiveNoteText"], """
          const a = {filledDays: ['2026-10-01', '2026-10-02', '2026-10-20']};
          out.days = archiveDaysIn(a, '2026-10-01', '2026-10-07');
          out.text = archiveNoteText(out.days);
          out.none = archiveNoteText(archiveDaysIn(a, '2026-11-01', '2026-11-07'));
        """)
        self.assertEqual(out["days"], ["2026-10-01", "2026-10-02"])
        self.assertIn("2 days in this range (2026-10-01 to 2026-10-02) come from the usage archive", out["text"])
        self.assertEqual(out["none"], "")


class DocsTests(unittest.TestCase):
    def test_readme_has_185_without_em_dashes(self):
        text = open(os.path.join(ROOT, "README.md")).read()
        m = re.search(r"## Version 1\.8\.5\n(.*?)\n## ", text, re.S)
        self.assertIsNotNone(m)
        self.assertNotIn("\u2014", m.group(1))
        for word in ("allowance-history.jsonl", "archive", "Seat value"):
            self.assertIn(word, m.group(1))

    def test_ci_imports_new_modules(self):
        ci = open(os.path.join(ROOT, ".github", "workflows", "ci.yml")).read()
        self.assertIn("allowance_history", ci)
        self.assertIn("usage_archive", ci)

    def test_formula_tests_new_modules(self):
        f = open(os.path.join(ROOT, "Formula", "agentic-spend.rb")).read()
        self.assertIn("allowance_history", f)
        self.assertIn("usage_archive", f)


if __name__ == "__main__":
    unittest.main()
