import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))
os.environ.setdefault("AGENTIC_SPEND_DATA_DIR", tempfile.mkdtemp(prefix="agentic-spend-test-"))

import server  # noqa: E402


class BuildHourlyTests(unittest.TestCase):
    def test_buckets_by_local_hour_and_sums_cost(self):
        rows = [
            {
                "agent": "claude", "totalCost": 2.0,
                "metadata": {"lastActivity": "2026-11-24T18:05:00+00:00"},
                "modelBreakdowns": [{"inputTokens": 100, "outputTokens": 20}],
            },
            {
                "agent": "claude", "totalCost": 3.0,
                "metadata": {"lastActivity": "2026-11-24T18:45:00+00:00"},
                "modelBreakdowns": [{"inputTokens": 50, "outputTokens": 10}],
            },
            {
                "agent": "grok", "totalCost": 1.0,
                "metadata": {"lastActivity": "2026-11-24T19:10:00+00:00"},
                "modelBreakdowns": [],
            },
        ]
        hourly = server.build_hourly(rows)
        self.assertEqual(len(hourly), 2)
        h18 = next(b for b in hourly if b["period"].endswith("T18:00"))
        self.assertAlmostEqual(h18["cost"], 5.0)
        self.assertEqual(h18["inputTokens"], 150)
        self.assertEqual(h18["outputTokens"], 30)
        self.assertAlmostEqual(h18["agents"]["claude"], 5.0)

    def test_rows_without_a_timestamp_are_skipped(self):
        rows = [{"agent": "claude", "totalCost": 1.0, "metadata": {}, "modelBreakdowns": []}]
        self.assertEqual(server.build_hourly(rows), [])


class BuildSourcesTests(unittest.TestCase):
    def test_claude_account_with_no_plan_data_gets_kind_none(self):
        account_list = [{"accountUuid": "acct-1", "label": "you@example.com", "note": None}]
        sources = server.build_sources(account_list, [], None, False, None, {}, {}, {})
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0]["kind"], "none")
        self.assertEqual(sources[0]["tool"], "claude")
        self.assertIn("No plan-limit data ever captured", sources[0]["note"])

    def test_claude_account_with_live_plan_data_gets_kind_official_live(self):
        account_list = [{"accountUuid": "acct-1", "label": "you@example.com", "note": None}]
        max_usage = [{"accountUuid": "acct-1", "live": True, "fetchedAt": "2026-11-24T18:00:00"}]
        sources = server.build_sources(account_list, max_usage, None, False, None, {}, {}, {})
        self.assertEqual(sources[0]["kind"], "official-live")
        self.assertEqual(sources[0]["asOf"], "2026-11-24T18:00:00")


if __name__ == "__main__":
    unittest.main()
