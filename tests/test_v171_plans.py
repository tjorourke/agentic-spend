import base64
import datetime
import json
import os
import re
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import plan_detection
import plan_prices
import seat_prices


class PriceTableTests(unittest.TestCase):
    def setUp(self):
        self.table = plan_prices.load()
        self.rows = self.table["rows"]

    def test_every_row_has_source_currency_billing_and_date(self):
        self.assertGreater(len(self.rows), 30)
        for r in self.rows:
            self.assertTrue(r["sourceUrl"].startswith("https://"), r)
            self.assertIn(r["currency"], ("USD", "GBP"), r)
            self.assertIn(r["billing"], ("monthly", "annual"), r)
            self.assertRegex(r["fetched"], r"^\d{4}-\d{2}-\d{2}$")
            if r["perMonth"] is None:
                # no invented numbers: a blank price says why
                self.assertTrue(r["contactSales"] or r.get("note"), r)
            else:
                self.assertIsInstance(r["perMonth"], (int, float))
                if r["perMonth"] > 0:
                    self.assertTrue(r.get("tax"), r)

    def test_sources_are_official_provider_domains(self):
        allowed = re.compile(r"^https://(claude\.com|support\.claude\.com|chatgpt\.com|help\.openai\.com|docs\.x\.ai|grok\.com|opencode\.ai)/")
        for r in self.rows:
            self.assertRegex(r["sourceUrl"], allowed)

    def test_enterprise_is_contact_sales_not_a_default(self):
        self.assertIsNone(plan_prices.default_for("chatgpt/enterprise", table=self.table))
        self.assertIsNone(plan_prices.default_for("claude/enterprise", table=self.table))
        self.assertTrue(any(r["contactSales"] for r in plan_prices.rows_for("chatgpt/enterprise", self.table)))

    def test_default_is_monthly_in_billing_currency_when_published(self):
        d = plan_prices.default_for("claude/team-premium", table=self.table)
        self.assertEqual((d["perMonth"], d["currency"], d["billing"]), (125, "USD", "monthly"))
        self.assertEqual(plan_prices.default_for("xai/super", "GBP", self.table)["perMonth"], 90)
        self.assertEqual(plan_prices.default_for("xai/super", None, self.table)["perMonth"], 100)
        # Anthropic publishes no GBP price, so a GBP-billed Ultra falls back to USD
        d = plan_prices.default_for("claude/ultra", "GBP", self.table)
        self.assertEqual((d["perMonth"], d["currency"]), (200, "USD"))
        self.assertIsNone(plan_prices.default_for(None, table=self.table))
        self.assertIsNone(plan_prices.default_for("chatgpt/go", table=self.table))


class ClaudePlanTests(unittest.TestCase):
    def plan(self, **kw):
        return plan_detection.claude_plan(kw)[0]

    def test_mapping(self):
        self.assertEqual(self.plan(organizationType="claude_pro"), "claude/pro")
        self.assertEqual(self.plan(organizationType="claude_max", organizationRateLimitTier="default_claude_max_20x"), "claude/ultra")
        self.assertEqual(self.plan(organizationType="claude_max", userRateLimitTier="default_claude_max_5x"), "claude/max")
        self.assertIsNone(self.plan(organizationType="claude_max"))
        self.assertEqual(self.plan(organizationType="claude_team", seatTier="team_tier_1"), "claude/team-premium")
        self.assertEqual(self.plan(organizationType="claude_team", seatTier="team_standard"), "claude/team-standard")
        self.assertIsNone(self.plan(organizationType="claude_team", seatTier=None))
        self.assertEqual(self.plan(organizationType="claude_enterprise"), "claude/enterprise")
        self.assertIsNone(self.plan(organizationType="something_new"))
        pid, label, ev = plan_detection.claude_plan(None)
        self.assertEqual((pid, label), (None, "unknown plan"))

    def test_profile_scan_keeps_newest_and_no_secrets(self):
        with tempfile.TemporaryDirectory() as d:
            def write(name, oauth, mtime):
                path = os.path.join(d, name)
                with open(path, "w") as f:
                    json.dump({"oauthAccount": oauth, "primaryApiKey": "sk-should-not-copy"}, f)
                os.utime(path, (mtime, mtime))
                return path
            old = write("b1", {"accountUuid": "a", "organizationType": "claude_pro", "accessToken": "tok"}, 1000)
            new = write("b2", {"accountUuid": "a", "organizationType": "claude_max",
                               "organizationRateLimitTier": "default_claude_max_20x"}, 2000)
            cur = write("cfg", {"accountUuid": "b", "organizationType": "claude_team", "seatTier": "team_tier_1"}, 3000)
            store = os.path.join(d, "profiles.json")
            prof = plan_detection.scan_claude_profiles(config=cur, backups=[old, new], profiles_path=store)
            self.assertEqual(prof["a"]["organizationType"], "claude_max")
            self.assertEqual(prof["b"]["seatTier"], "team_tier_1")
            saved = open(store).read()
            self.assertNotIn("tok", saved)
            self.assertNotIn("sk-", saved)
            for p in json.loads(saved).values():
                self.assertTrue(set(p) <= set(plan_detection.PROFILE_FIELDS) | {"seenAt"})
            # a later scan with the backups gone still knows account a
            again = plan_detection.scan_claude_profiles(config=cur, backups=[], profiles_path=store)
            self.assertEqual(again["a"]["organizationType"], "claude_max")


class OtherPlanTests(unittest.TestCase):
    def test_codex(self):
        self.assertEqual(plan_detection.codex_plan("free", None)[0], "chatgpt/free")
        self.assertEqual(plan_detection.codex_plan(None, "plus")[0], "chatgpt/plus")
        pid, label, _ = plan_detection.codex_plan("pro", None)
        self.assertIsNone(pid)
        self.assertIn("tier unknown", label)
        self.assertIsNone(plan_detection.codex_plan("team", None)[0])
        self.assertEqual(plan_detection.codex_plan(None, None)[1], "unknown plan")

    def test_codex_claim_reads_only_the_plan(self):
        claims = {"https://api.openai.com/auth": {"chatgpt_plan_type": "plus", "user_id": "u"}, "email": "x"}
        payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"tokens": {"id_token": "h." + payload + ".sig", "access_token": "secret"}}, f)
        try:
            self.assertEqual(plan_detection.codex_plan_claim(f.name), "plus")
        finally:
            os.unlink(f.name)
        self.assertIsNone(plan_detection.codex_plan_claim("/nonexistent/auth.json"))

    def test_grok(self):
        gb = {"found": True, "accounts": [{"plan": "Super"}]}
        self.assertEqual(plan_detection.grok_plan(gb)[0], "xai/super")
        self.assertEqual(plan_detection.grok_plan({"accounts": [{"plan": "SuperGrok Heavy"}]})[0], "xai/legacy-supergrok-heavy")
        self.assertIsNone(plan_detection.grok_plan({"accounts": [{"plan": "Mystery"}]})[0])
        self.assertIsNone(plan_detection.grok_plan({"accounts": [{"plan": "Super"}, {"plan": "Plus"}]})[0])
        self.assertEqual(plan_detection.grok_plan(None)[1], "unknown plan")

    def test_opencode(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"openai": {"type": "api", "key": "sk-x"}, "anthropic": {"type": "api", "key": "sk-y"}}, f)
        try:
            pid, label, ev = plan_detection.opencode_plan(f.name)
            self.assertIsNone(pid)
            self.assertIn("API keys", label)
            self.assertNotIn("sk-", " ".join(ev))
        finally:
            os.unlink(f.name)

    def test_detect_all_unknown_for_other_sources(self):
        out = plan_detection.detect_all([], ccusage_sources=[{"id": "gemini", "label": "Gemini CLI"}], profiles={})
        self.assertEqual(out[0]["planLabel"], "unknown plan")
        self.assertFalse(out[0]["detected"])


class PlanValueTests(unittest.TestCase):
    def setUp(self):
        self.today = datetime.date(2026, 11, 26)
        self.mu = [{"accountUuid": "team", "label": "team@x", "usageCredits": {"currency": "USD"}},
                   {"accountUuid": "pers", "label": "me@x", "usageCredits": {"currency": "GBP"}}]
        self.sessions = [{"accountUuid": "team", "period": "2026-11-10", "totalCost": 250.0},
                         {"accountUuid": "pers", "period": "2026-11-11", "totalCost": 400.0},
                         {"accountUuid": "team", "period": "2026-10-30", "totalCost": 999.0}]
        self.det = [
            {"key": "team", "provider": "claude", "planId": "claude/team-premium", "planLabel": "Claude Team, Premium seat", "detected": True},
            {"key": "pers", "provider": "claude", "planId": "claude/ultra", "planLabel": "Claude Ultra", "detected": True},
            {"key": "grok", "provider": "xai", "label": "Grok", "planId": "xai/super", "planLabel": "X Subscription Super", "detected": True},
            {"key": "opencode", "provider": "opencode", "label": "OpenCode", "planId": None, "planLabel": "unknown plan", "detected": False},
        ]
        self.daily = [{"period": "2026-11-20", "agents": [{"agent": "grok", "totalCost": 30.0}, {"agent": "opencode", "totalCost": 5.0}]},
                      {"period": "2026-10-31", "agents": [{"agent": "grok", "totalCost": 70.0}]}]

    def by_key(self, prices):
        out = seat_prices.team_value(self.mu, self.sessions, prices, today=self.today,
                                     detections=self.det, daily=self.daily)
        return {e["key"]: e for e in out}

    def test_defaults_follow_the_table(self):
        v = self.by_key({})
        self.assertEqual(v["team"]["seat"], {"amount": 125.0, "currency": "USD", "period": "month", "source": "default"})
        self.assertEqual(v["team"]["multiple"], 2.0)  # 250 / 125
        self.assertEqual(v["pers"]["seat"]["amount"], 200.0)
        self.assertEqual(v["pers"]["seat"]["currency"], "USD")
        self.assertEqual(v["pers"]["billingCurrency"], "GBP")
        self.assertEqual(v["grok"]["apiEquivalentUsd"], 30.0)  # October excluded
        self.assertEqual(v["grok"]["seat"]["amount"], 100.0)
        self.assertEqual(v["grok"]["multiple"], 0.3)
        self.assertIsNone(v["opencode"]["seat"])

    def test_your_price_overrides_the_default(self):
        v = self.by_key({"team": {"amount": 100.0, "currency": "USD", "period": "month"},
                         "grok": {"amount": 90.0, "currency": "GBP", "period": "month"}})
        self.assertEqual(v["team"]["seat"]["source"], "user")
        self.assertEqual(v["team"]["multiple"], 2.5)
        self.assertEqual(v["grok"]["seat"]["source"], "user")
        self.assertIsNone(v["grok"]["multiple"])  # no currency conversion

    def test_non_claude_keys_accepted_by_validation(self):
        self.assertEqual(seat_prices.validate_body({"accountUuid": "grok", "amount": 90, "currency": "GBP"})[0], "grok")


if __name__ == "__main__":
    unittest.main()
