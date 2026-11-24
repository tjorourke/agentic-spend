import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))
os.environ.setdefault("AGENTIC_SPEND_DATA_DIR", tempfile.mkdtemp(prefix="agentic-spend-test-"))

import provider_usage  # noqa: E402


class CentsTests(unittest.TestCase):
    def test_converts_cents_to_dollars(self):
        self.assertEqual(provider_usage._cents({"val": 1250}), 12.5)

    def test_missing_or_malformed_returns_none(self):
        self.assertIsNone(provider_usage._cents(None))
        self.assertIsNone(provider_usage._cents({}))
        self.assertIsNone(provider_usage._cents({"val": "not-a-number"}))


class ParseGrokBillingTests(unittest.TestCase):
    def test_parses_weekly_period_and_usd_fields(self):
        body = {
            "subscription_tier": "SuperGrok",
            "config": {
                "currentPeriod": {
                    "type": "USAGE_PERIOD_TYPE_WEEKLY",
                    "start": "2026-11-17T00:00:00Z",
                    "end": "2026-11-24T00:00:00Z",
                },
                "onDemandUsed": {"val": 0},
                "onDemandCap": {"val": 2000},
                "prepaidBalance": {"val": 50000},
            },
        }
        parsed = provider_usage._parse_grok_billing(body)
        self.assertEqual(parsed["plan"], "SuperGrok")
        self.assertEqual(parsed["period"], "week")
        self.assertEqual(parsed["periodStart"], "2026-11-17T00:00:00Z")
        self.assertEqual(parsed["resetsAt"], "2026-11-24T00:00:00Z")
        self.assertEqual(parsed["onDemandUsedUsd"], 0.0)
        self.assertEqual(parsed["onDemandCapUsd"], 20.0)
        self.assertEqual(parsed["prepaidBalanceUsd"], 500.0)

    def test_unknown_period_type_passed_through_unmapped(self):
        body = {"config": {"currentPeriod": {"type": "SOMETHING_NEW"}}}
        parsed = provider_usage._parse_grok_billing(body)
        self.assertEqual(parsed["period"], "SOMETHING_NEW")


if __name__ == "__main__":
    unittest.main()
