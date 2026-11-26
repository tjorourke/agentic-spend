"""1.7.0: ccusage sources and tiers, seat prices and team value, 5-hour
pacing (and its notification), model-mix repricing, and the status line's
stale-session and wrong-account guards."""
import datetime
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))
os.environ.setdefault("AGENTIC_SPEND_DATA_DIR", tempfile.mkdtemp(prefix="agentic-spend-test-"))

import ccusage_sources  # noqa: E402
import insights  # noqa: E402
import model_mix  # noqa: E402
import notify  # noqa: E402
import pacing  # noqa: E402
import seat_prices  # noqa: E402
import server  # noqa: E402
import statusline  # noqa: E402

UTC = datetime.timezone.utc


class CcusageSourcesTests(unittest.TestCase):
    def test_catalog_matches_ccusage_registry_order(self):
        ids = [c[0] for c in ccusage_sources.CATALOG]
        self.assertEqual(ids, ["claude", "codex", "opencode", "amp", "droid", "codebuff", "hermes", "pi",
                               "goose", "openclaw", "kilo", "copilot", "gemini", "antigravity", "kimi",
                               "qwen", "grok", "zcode", "cline", "dsh"])

    def test_tiers_follow_ccusage_doc_titles(self):
        t = ccusage_sources.tier
        self.assertEqual(t("claude"), "first-class")
        self.assertEqual(t("antigravity"), "first-class")
        self.assertEqual(t("codex"), "beta")
        self.assertEqual(t("kilo"), "beta")
        self.assertEqual(t("droid"), "experimental")
        self.assertEqual(t("dsh"), "experimental")
        self.assertEqual(t("grok-cli"), "first-class")
        self.assertIsNone(t("not-a-source"))

    def test_absent_sources_are_left_out_entirely(self):
        daily = [{"agents": [{"agent": "claude"}, {"agent": "amp"}]}]
        out = ccusage_sources.present(daily, [], detection=[{"id": "codex", "installed": True, "hasData": False}])
        self.assertEqual([x["id"] for x in out], ["claude", "codex", "amp"])
        self.assertEqual([x["hasData"] for x in out], [True, False, True])
        self.assertEqual(ccusage_sources.present([], [], []), [])


class SeatPriceTests(unittest.TestCase):
    MU = [
        {"accountUuid": "team", "label": "Team", "usageCredits": {"currency": "USD"}},
        {"accountUuid": "me", "label": "Personal", "usageCredits": {"currency": "GBP"}},
        {"accountUuid": "nocur", "label": "No currency"},
    ]
    SESSIONS = [
        {"accountUuid": "team", "period": "2026-11-02", "totalCost": 100.0},
        {"accountUuid": "team", "period": "2026-11-20", "totalCost": 50.5},
        {"accountUuid": "team", "period": "2026-10-31", "totalCost": 999.0},
        {"accountUuid": "me", "period": "2026-11-26", "totalCost": 12.25},
    ]

    def test_no_default_price(self):
        tv = seat_prices.team_value(self.MU, self.SESSIONS, {}, today=datetime.date(2026, 11, 26))
        self.assertEqual([t["seat"] for t in tv], [None, None, None])
        self.assertEqual(tv[0]["apiEquivalentUsd"], 150.5)
        self.assertEqual(tv[1]["apiEquivalentUsd"], 12.25)
        self.assertEqual(tv[0]["monthStart"], "2026-11-01")

    def test_multiple_only_when_seat_is_in_usd(self):
        prices = {"team": {"amount": 30.0, "currency": "USD", "period": "month"},
                  "me": {"amount": 90.0, "currency": "GBP", "period": "month"}}
        tv = seat_prices.team_value(self.MU, self.SESSIONS, prices, today=datetime.date(2026, 11, 26))
        self.assertEqual(tv[0]["multiple"], 5.0)
        self.assertIsNone(tv[1]["multiple"])  # GBP seat, USD API figure: not converted
        self.assertEqual(tv[1]["billingCurrency"], "GBP")

    def test_price_must_use_billing_currency(self):
        with self.assertRaises(ValueError):
            seat_prices.validate_body({"accountUuid": "me", "amount": 18, "currency": "USD"}, "GBP")
        acct, entry = seat_prices.validate_body({"accountUuid": "me", "amount": 18}, "GBP")
        self.assertEqual(entry, {"amount": 18.0, "currency": "GBP", "period": "month"})
        self.assertEqual(seat_prices.validate_body({"accountUuid": "me", "amount": None}, "GBP"), ("me", None))
        for bad in ({"accountUuid": "me", "amount": 0, "currency": "GBP"},
                    {"accountUuid": "me", "amount": True, "currency": "GBP"},
                    {"accountUuid": "../x", "amount": 1, "currency": "GBP"},
                    {"accountUuid": "me", "amount": 5, "currency": "pounds"}):
            with self.assertRaises(ValueError):
                seat_prices.validate_body(bad, None)

    def test_save_and_load(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "seat-prices.json")
            seat_prices.save({"me": {"amount": 18, "currency": "GBP"}}, path)
            self.assertEqual(seat_prices.load(path), {"me": {"amount": 18.0, "currency": "GBP", "period": "month"}})
            with open(path, "w") as f:
                f.write("not json")
            self.assertEqual(seat_prices.load(path), {})


def hist(uuid, minute, pct, reset_epoch):
    t = datetime.datetime(2026, 11, 26, 12, 0, tzinfo=UTC) + datetime.timedelta(minutes=minute)
    return {"time": t.isoformat(), "accountUuid": uuid, "fiveHourPercent": pct, "fiveHourResetsAt": reset_epoch}


RESET = datetime.datetime(2026, 11, 26, 17, 0, tzinfo=UTC)  # window 12:00-17:00
RESET_EPOCH = int(RESET.timestamp())


class PacingTests(unittest.TestCase):
    def test_envelope_drops_stale_lower_readings(self):
        pts = [(1, 3.0), (2, 5.0), (3, 3.0), (4, 5.0), (5, 6.0)]
        self.assertEqual(pacing.envelope(pts), [(1, 3.0), (2, 5.0), (5, 6.0)])

    def test_warns_when_pace_runs_out_before_reset(self):
        history = [hist("a", 30, 10, RESET_EPOCH), hist("a", 50, 5, RESET_EPOCH), hist("a", 60, 25, RESET_EPOCH)]
        session = {"percent": 25, "resetsAt": RESET.isoformat(), "asOf": "2026-11-26T13:00:00+00:00"}
        p = pacing.session_pace("a", session, history, now="2026-11-26T13:05:00+00:00")
        self.assertEqual(p["basis"], "readings")
        self.assertEqual(p["ratePerHour"], 30.0)  # 10% -> 25% in 30 minutes; the stale 5% is ignored
        self.assertTrue(p["warn"])
        self.assertEqual(p["hitsLimitAt"], "2026-11-26T15:30:00Z")

    def test_no_warning_when_on_track(self):
        history = [hist("a", 0, 1, RESET_EPOCH), hist("a", 120, 10, RESET_EPOCH)]
        session = {"percent": 12, "resetsAt": RESET.isoformat(), "asOf": "2026-11-26T14:10:00+00:00"}
        p = pacing.session_pace("a", session, history, now="2026-11-26T14:15:00+00:00")
        self.assertFalse(p["warn"])
        self.assertIsNone(p["hitsLimitAt"])

    def test_no_warning_on_thin_data_or_when_idle(self):
        session = {"percent": 4, "resetsAt": RESET.isoformat(), "asOf": "2026-11-26T12:10:00+00:00"}
        thin = pacing.session_pace("a", session, [], now="2026-11-26T12:12:00+00:00")
        self.assertTrue(thin["thin"])
        self.assertFalse(thin["warn"])
        busy = {"percent": 60, "resetsAt": RESET.isoformat(), "asOf": "2026-11-26T13:00:00+00:00"}
        idle = pacing.session_pace("a", busy, [], now="2026-11-26T14:00:00+00:00", active_block={})
        self.assertTrue(idle["idle"])
        self.assertFalse(idle["warn"])

    def test_blocks_basis_only_for_a_single_account(self):
        session = {"percent": 10, "resetsAt": RESET.isoformat(), "asOf": "2026-11-26T13:00:00+00:00"}
        block = {"isActive": True, "startTime": "2026-11-26T12:00:00Z", "costUSD": 5.0,
                 "burnRate": {"costPerHour": 10.0}, "actualEndTime": "2026-11-26T12:59:00Z"}
        one = pacing.session_pace("a", session, [], now="2026-11-26T13:00:00+00:00",
                                  active_block=block, only_claude_account=True)
        self.assertEqual(one["basis"], "blocks")
        self.assertEqual(one["ratePerHour"], 20.0)  # 10% x $10/h / $5
        self.assertEqual(one["projectedPct"], 90.0)  # 10% + 4h x 20%/h, still under 100% at the reset
        self.assertFalse(one["warn"])
        two = pacing.session_pace("a", session, [], now="2026-11-26T13:00:00+00:00",
                                  active_block=block, only_claude_account=False)
        self.assertEqual(two["basis"], "window")

    def test_none_after_reset(self):
        session = {"percent": 50, "resetsAt": RESET.isoformat()}
        self.assertIsNone(pacing.session_pace("a", session, [], now="2026-11-26T17:01:00+00:00"))

    def test_insights_carry_session_pace(self):
        mu = [{"accountUuid": "a", "label": "Team", "fetchedAt": "2026-11-26T13:00:00+00:00",
               "session": {"percent": 25, "resetsAt": RESET.isoformat(), "asOf": "2026-11-26T13:00:00+00:00"},
               "week": {"percent": 30, "resetsAt": "2026-11-29T09:00:00+00:00"}}]
        history = [hist("a", 30, 10, RESET_EPOCH)]
        out = insights.build_plan_insights(max_usage=mu, claude_history=history, now="2026-11-26T13:05:00+00:00")
        self.assertTrue(out[0]["sessionPace"]["warn"])
        summary = server.build_summary({"planInsights": out}, today=datetime.date(2026, 11, 26))
        self.assertEqual(summary["paceWarning"]["label"], "Team")
        self.assertTrue(summary["paceWarning"]["warn"])


class PaceNotificationTests(unittest.TestCase):
    def plan(self, warn=True):
        return {"key": "claude:a", "label": "Team", "pct": 30, "resetAt": "2026-11-29T09:00:00+00:00",
                "sessionPace": {"warn": warn, "currentPct": 40, "hitsLimitAt": "2026-11-26T15:30:00+00:00",
                                "resetAt": "2026-11-26T17:00:00+00:00"}}

    def test_once_per_window(self):
        now = datetime.datetime(2026, 11, 26, 13, 0, tzinfo=UTC)
        events, state = notify.evaluate([self.plan()], notify.DEFAULTS, {}, now=now, tz=UTC)
        pace = [e for e in events if e["kind"] == "pace"]
        self.assertEqual(len(pace), 1)
        self.assertIn("before the Thu 17:00 reset", pace[0]["message"])
        again, _ = notify.evaluate([self.plan()], notify.DEFAULTS, state, now=now, tz=UTC)
        self.assertEqual([e for e in again if e["kind"] == "pace"], [])

    def test_off_switch_and_no_warning(self):
        now = datetime.datetime(2026, 11, 26, 13, 0, tzinfo=UTC)
        off = notify.validate_settings({"pace": False}, notify.DEFAULTS)
        events, _ = notify.evaluate([self.plan()], off, {}, now=now, tz=UTC)
        self.assertEqual([e for e in events if e["kind"] == "pace"], [])
        events, _ = notify.evaluate([self.plan(warn=False)], notify.DEFAULTS, {}, now=now, tz=UTC)
        self.assertEqual([e for e in events if e["kind"] == "pace"], [])


class ModelMixTests(unittest.TestCase):
    DAILY = [{"period": "2026-11-25", "modelBreakdowns": [
        {"modelName": "claude-opus-5-5", "cost": 10.0}, {"modelName": "claude-sonnet-5", "cost": 4.0}]}]
    LITELLM = {"claude-sonnet-5": {"input_cost_per_token": 2e-06, "output_cost_per_token": 1e-05,
                                   "cache_read_input_token_cost": 2e-07,
                                   "cache_creation_input_token_cost_above_1hr": 4e-06},
               "claude-sonnet-4-5": {"input_cost_per_token": 3e-06, "output_cost_per_token": 1.5e-05}}

    def test_override_and_config(self):
        rates = model_mix.override_for(self.LITELLM, "claude-sonnet-5")
        self.assertEqual(rates["inputCostPerToken"], 2e-06)
        self.assertEqual(rates["cacheCreationInputTokenCostAbove1hr"], 4e-06)
        cfg = model_mix.build_config(["claude-opus-5-5"], rates, {"defaults": {"offline": True}})
        self.assertTrue(cfg["defaults"]["offline"])
        self.assertEqual(cfg["defaults"]["pricingOverrides"]["claude-opus-5-5"], rates)

    def test_pick_sonnet_prefers_the_one_you_use(self):
        self.assertEqual(model_mix.pick_sonnet(self.DAILY, self.LITELLM), "claude-sonnet-5")
        self.assertEqual(model_mix.pick_sonnet([], self.LITELLM), "claude-sonnet-5")

    def test_compute_runs_ccusage_twice_in_calculate_mode(self):
        calls = []

        def runner(cmd, **kw):
            calls.append(cmd)
            cfg = json.load(open(cmd[cmd.index("--config") + 1]))
            repriced = bool(cfg["defaults"]["pricingOverrides"])

            class R:
                stdout = json.dumps({"daily": [{"period": "2026-11-25", "modelBreakdowns": [
                    {"modelName": "claude-opus-5-5", "cost": 5.0 if repriced else 10.0}]}]})
            return R()

        mix = model_mix.compute(self.DAILY, runner=runner, litellm=self.LITELLM, user_config={})
        self.assertIsNone(mix["error"])
        self.assertEqual(len(calls), 2)
        self.assertTrue(all("calculate" in c for c in calls))
        self.assertEqual(mix["repricedByDay"]["2026-11-25"]["claude-opus-5-5"], {"calculated": 10.0, "atSonnet": 5.0})
        s = model_mix.summarise(self.DAILY, mix)
        self.assertEqual(s["saving"], 5.0)
        self.assertTrue(mix["estimate"])

    def test_no_price_list_means_no_estimate(self):
        mix = model_mix.compute(self.DAILY, runner=lambda *a, **k: None, litellm={}, user_config={})
        self.assertIn("LiteLLM", mix["error"])
        self.assertEqual(mix["repricedByDay"], {})

    def test_reads_ccusage_http_cache_format(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "raw.githubusercontent.com-BerriAI-litellm-x.cache")
            with open(path, "wb") as f:
                f.write(b'W/"etag"\n' + json.dumps(self.LITELLM).encode())
            self.assertIn("claude-sonnet-5", model_mix.load_litellm(os.path.join(d, "*.cache")))


class StatuslineGuardTests(unittest.TestCase):
    def test_same_window_lower_figure_keeps_the_higher_one(self):
        now = datetime.datetime(2026, 11, 26, 13, 0, tzinfo=UTC)
        prev = {"fiveHourPercent": 6, "fiveHourResetsAt": RESET_EPOCH, "sevenDayPercent": 9, "sevenDayResetsAt": RESET_EPOCH + 86400}
        new = {"fiveHourPercent": 3, "fiveHourResetsAt": RESET_EPOCH, "sevenDayPercent": 9, "sevenDayResetsAt": RESET_EPOCH + 86400}
        self.assertEqual(statusline.merge_live_record(prev, new, now=now)["fiveHourPercent"], 6)
        newer = dict(new, fiveHourResetsAt=RESET_EPOCH + 5 * 3600, fiveHourPercent=1)
        self.assertEqual(statusline.merge_live_record(prev, newer, now=now)["fiveHourPercent"], 1)

    def test_other_accounts_weekly_window_is_not_ours(self):
        now = datetime.datetime(2026, 11, 25, 11, 0, tzinfo=UTC)
        team_reset = int(datetime.datetime(2026, 11, 27, 15, 0, tzinfo=UTC).timestamp())
        personal_reset = int(datetime.datetime(2026, 11, 25, 22, 0, tzinfo=UTC).timestamp())
        with tempfile.TemporaryDirectory() as d:
            json.dump({"sevenDayResetsAt": team_reset}, open(os.path.join(d, "team.json"), "w"))
            json.dump({"sevenDayResetsAt": personal_reset}, open(os.path.join(d, "me.json"), "w"))
            other = statusline.reading_belongs_to_other_account("team", {"resets_at": personal_reset}, d, now)
            ours = statusline.reading_belongs_to_other_account("team", {"resets_at": team_reset}, d, now)
            unknown = statusline.reading_belongs_to_other_account("team", {"resets_at": team_reset + 7 * 86400}, d, now)
        self.assertTrue(other)
        self.assertFalse(ours)
        self.assertFalse(unknown)


if __name__ == "__main__":
    unittest.main()
