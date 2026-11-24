import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))
os.environ.setdefault("AGENTIC_SPEND_DATA_DIR", tempfile.mkdtemp(prefix="agentic-spend-test-"))

import server  # noqa: E402


class BuildHourlyFromBlocksTests(unittest.TestCase):
    """ccusage blocks groups by each request's own timestamp, not a session's
    lastActivity — this is what makes a past hour's bar final instead of
    drifting forward as a long session keeps running."""

    def test_converts_blocks_to_hour_buckets(self):
        blocks_json = {"blocks": [
            {"startTime": "2026-11-24T18:00:00.000Z", "costUSD": 11.82, "isGap": False,
             "tokenCounts": {"inputTokens": 1000, "outputTokens": 200}},
            {"startTime": "2026-11-24T19:00:00.000Z", "costUSD": 23.91, "isGap": False,
             "tokenCounts": {"inputTokens": 2000, "outputTokens": 400}},
            {"startTime": "2026-11-24T20:00:00.000Z", "isGap": True},
        ]}
        buckets = server.build_hourly_from_blocks(blocks_json)
        self.assertEqual(len(buckets), 2)  # the gap block is dropped
        h18 = next(b for b in buckets if b["period"].endswith("T18:00"))
        self.assertAlmostEqual(h18["cost"], 11.82)
        self.assertEqual(h18["inputTokens"], 1000)
        self.assertAlmostEqual(h18["agents"]["claude"], 11.82)


class BuildHourlyTests(unittest.TestCase):
    """build_hourly merges Claude's already-final hour buckets with the
    other agents, who still only carry a session-level lastActivity."""

    def test_claude_rows_are_ignored_since_the_blocks_already_cover_them(self):
        rows = [{
            "agent": "claude", "totalCost": 99.0,
            "metadata": {"lastActivity": "2026-11-24T18:05:00+00:00"},
            "modelBreakdowns": [],
        }]
        claude_blocks = [{"period": "2026-11-24T18:00", "cost": 11.82, "agents": {"claude": 11.82},
                           "inputTokens": 1000, "outputTokens": 200}]
        hourly = server.build_hourly(rows, claude_blocks)
        self.assertEqual(len(hourly), 1)
        self.assertAlmostEqual(hourly[0]["cost"], 11.82)  # not 99.0 + 11.82

    def test_claude_falls_back_to_lastActivity_when_blocks_call_failed(self):
        rows = [{
            "agent": "claude", "totalCost": 99.0,
            "metadata": {"lastActivity": "2026-11-24T18:05:00+00:00"},
            "modelBreakdowns": [],
        }]
        hourly = server.build_hourly(rows, None)  # None: the ccusage blocks call itself failed
        self.assertEqual(len(hourly), 1)
        self.assertAlmostEqual(hourly[0]["cost"], 99.0)  # not dropped

    def test_claude_genuinely_idle_today_stays_empty_when_blocks_call_worked(self):
        rows = [{
            "agent": "claude", "totalCost": 99.0,
            "metadata": {"lastActivity": "2026-11-24T18:05:00+00:00"},
            "modelBreakdowns": [],
        }]
        hourly = server.build_hourly(rows, [])  # []: ccusage ran fine, found nothing
        self.assertEqual(hourly, [])

    def test_non_claude_rows_still_bucket_by_session_lastActivity(self):
        rows = [{
            "agent": "grok", "totalCost": 1.0,
            "metadata": {"lastActivity": "2026-11-24T19:10:00+00:00"},
            "modelBreakdowns": [{"inputTokens": 50, "outputTokens": 10}],
        }]
        hourly = server.build_hourly(rows, [])
        self.assertEqual(len(hourly), 1)
        h19 = hourly[0]
        self.assertTrue(h19["period"].endswith("T19:00"))
        self.assertAlmostEqual(h19["cost"], 1.0)
        self.assertAlmostEqual(h19["agents"]["grok"], 1.0)

    def test_rows_without_a_timestamp_are_skipped(self):
        rows = [{"agent": "grok", "totalCost": 1.0, "metadata": {}, "modelBreakdowns": []}]
        self.assertEqual(server.build_hourly(rows, []), [])


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
