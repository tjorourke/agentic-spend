import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import plan_prices
import pricing_region
import seat_prices
import datetime


class LocaleTests(unittest.TestCase):
    def test_region_from_locale(self):
        self.assertEqual(pricing_region.region_from_locale("en_GB"), "UK")
        self.assertEqual(pricing_region.region_from_locale("en-GB"), "UK")
        self.assertEqual(pricing_region.region_from_locale("en_GB@currency=GBP"), "UK")
        self.assertEqual(pricing_region.region_from_locale("en_UK"), "UK")
        self.assertEqual(pricing_region.region_from_locale("en_US"), "US")
        self.assertEqual(pricing_region.region_from_locale("en-US"), "US")
        self.assertIsNone(pricing_region.region_from_locale("fr_FR"))
        self.assertIsNone(pricing_region.region_from_locale("C"))
        self.assertIsNone(pricing_region.region_from_locale(""))
        self.assertIsNone(pricing_region.region_from_locale(None))

    def test_current_prefers_saved_then_locale_then_us(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "pricing-region.json")
            auto = pricing_region.current(saved=None, locale="en_GB")
            self.assertEqual((auto["region"], auto["currency"], auto["source"], auto["detectedRegion"]),
                             ("UK", "GBP", "auto", "UK"))
            unknown = pricing_region.current(saved=None, locale="fr_FR")
            self.assertEqual((unknown["region"], unknown["currency"], unknown["source"]),
                             ("US", "USD", "auto"))
            self.assertIsNone(unknown["detectedRegion"])
            pricing_region.save("US", path)
            self.assertEqual(pricing_region.load(path), "US")
            chosen = pricing_region.current(saved=pricing_region.load(path), locale="en_GB")
            self.assertEqual((chosen["region"], chosen["source"], chosen["detectedRegion"]),
                             ("US", "user", "UK"))
            pricing_region.save(None, path)
            self.assertIsNone(pricing_region.load(path))
            self.assertFalse(os.path.exists(path))

    def test_validate_body(self):
        self.assertEqual(pricing_region.validate_body({"region": "UK"}), "UK")
        self.assertIsNone(pricing_region.validate_body({"region": None}))
        with self.assertRaises(ValueError):
            pricing_region.validate_body({"region": "EU"})
        with self.assertRaises(ValueError):
            pricing_region.validate_body("UK")


class DefaultCurrencyTests(unittest.TestCase):
    def setUp(self):
        self.table = plan_prices.load()

    def test_gbp_row_used_when_published(self):
        d = plan_prices.default_for("xai/super", "GBP", self.table)
        self.assertEqual((d["perMonth"], d["currency"], d["usedFallback"], d["preferredCurrency"]),
                         (90, "GBP", False, "GBP"))
        self.assertTrue(d["sourceUrl"].startswith("https://grok.com/"))
        self.assertIn("tax", d["tax"].lower())

    def test_usd_fallback_labelled_when_no_uk_price(self):
        d = plan_prices.default_for("claude/ultra", "GBP", self.table)
        self.assertEqual((d["perMonth"], d["currency"], d["usedFallback"], d["preferredCurrency"]),
                         (200, "USD", True, "GBP"))
        d = plan_prices.default_for("chatgpt/plus", "GBP", self.table)
        self.assertEqual((d["perMonth"], d["currency"], d["usedFallback"]), (20, "USD", True))
        # US region should not be flagged as a fallback
        d = plan_prices.default_for("claude/ultra", "USD", self.table)
        self.assertFalse(d["usedFallback"])

    def test_billing_currency_beats_region(self):
        today = datetime.date(2026, 11, 26)
        mu = [{"accountUuid": "team", "label": "team@x", "usageCredits": {"currency": "USD"}}]
        det = [{"key": "team", "provider": "claude", "planId": "claude/team-premium",
                "planLabel": "Claude Team, Premium seat", "detected": True},
               {"key": "grok", "provider": "xai", "label": "Grok", "planId": "xai/super",
                "planLabel": "X Subscription Super", "detected": True}]
        sessions = [{"accountUuid": "team", "period": "2026-11-10", "totalCost": 250.0}]
        daily = [{"period": "2026-11-20", "agents": [{"agent": "grok", "totalCost": 5.24}]}]
        out = {e["key"]: e for e in seat_prices.team_value(
            mu, sessions, {}, today=today, detections=det, daily=daily, region_currency="GBP")}
        # Team is billed in USD, so the USD list price is used even on a UK Mac
        self.assertEqual(out["team"]["seat"]["currency"], "USD")
        self.assertEqual(out["team"]["seat"]["amount"], 125.0)
        self.assertFalse(out["team"]["listPrice"]["default"]["usedFallback"])
        # Grok has no billing currency, so the UK region picks the published £90 row
        self.assertEqual(out["grok"]["seat"],
                         {"amount": 90.0, "currency": "GBP", "period": "month", "source": "default"})
        self.assertIsNone(out["grok"]["multiple"])  # no currency conversion
        self.assertFalse(out["grok"]["listPrice"]["default"]["usedFallback"])


if __name__ == "__main__":
    unittest.main()
