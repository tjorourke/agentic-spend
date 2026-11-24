import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))
os.environ.setdefault("AGENTIC_SPEND_DATA_DIR", tempfile.mkdtemp(prefix="agentic-spend-test-"))

import account_usage  # noqa: E402


class ComputeAccountsTests(unittest.TestCase):
    """compute_accounts is the function that turns raw ccusage session rows
    into the per-account dollar totals the whole UI is built on — the most
    load-bearing piece of logic in the app, and pure (no I/O), so it's
    cheap to pin down exactly."""

    def setUp(self):
        patcher = mock.patch("account_usage.detect_grok_email", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_claude_session_attributed_to_known_account(self):
        rows = [{
            "agent": "claude",
            "period": "sess-1",
            "totalCost": 12.5,
            "totalTokens": 1000,
            "metadata": {"lastActivity": "2026-11-20T10:00:00Z"},
            "modelBreakdowns": [{"modelName": "claude-sonnet-5", "cost": 12.5}],
        }]
        accounts, no_account_data, daily_by_bucket, account_sessions = account_usage.compute_accounts(
            rows, {"sess-1": "acct-1"}, {"acct-1": {"label": "you@example.com", "note": None}},
        )

        self.assertEqual(len(accounts), 1)
        self.assertEqual(accounts[0]["accountUuid"], "acct-1")
        self.assertEqual(accounts[0]["label"], "you@example.com")
        self.assertAlmostEqual(accounts[0]["cost"], 12.5)
        self.assertEqual(accounts[0]["sessions"], 1)
        self.assertEqual(accounts[0]["models"], {"claude-sonnet-5": 12.5})
        self.assertEqual(no_account_data["cost"], 0.0)
        self.assertEqual(len(account_sessions), 1)
        self.assertEqual(account_sessions[0]["period"], "2026-11-20")
        self.assertEqual(daily_by_bucket, [{"period": "2026-11-20", "you@example.com": 12.5}])

    def test_non_claude_agent_goes_to_no_account_bucket(self):
        rows = [{
            "agent": "opencode",
            "period": "sess-2",
            "totalCost": 3.0,
            "totalTokens": 500,
            "metadata": {"lastActivity": "2026-11-21T09:00:00Z"},
            "modelBreakdowns": [],
        }]
        accounts, no_account_data, _daily, _sessions = account_usage.compute_accounts(rows, {}, {})
        self.assertEqual(accounts, [])
        self.assertAlmostEqual(no_account_data["cost"], 3.0)
        self.assertEqual(no_account_data["sessions"], 1)
        self.assertEqual(no_account_data["agents"], {"opencode": 3.0})

    def test_claude_session_with_no_account_record_gets_unknown_label(self):
        rows = [{
            "agent": "claude",
            "period": "sess-3",
            "totalCost": 1.0,
            "totalTokens": 10,
            "metadata": {"lastActivity": "2026-11-22T00:00:00Z"},
            "modelBreakdowns": [],
        }]
        accounts, _no_account, _daily, _sessions = account_usage.compute_accounts(rows, {}, {})
        self.assertEqual(accounts[0]["accountUuid"], "unknown-session")
        self.assertEqual(accounts[0]["label"], account_usage.UNKNOWN_SESSION_LABEL["label"])

    def test_two_sessions_same_account_are_summed_not_duplicated(self):
        rows = [
            {
                "agent": "claude", "period": "sess-a", "totalCost": 5.0, "totalTokens": 100,
                "metadata": {"lastActivity": "2026-11-20T08:00:00Z"},
                "modelBreakdowns": [{"modelName": "claude-sonnet-5", "cost": 5.0}],
            },
            {
                "agent": "claude", "period": "sess-b", "totalCost": 7.0, "totalTokens": 200,
                "metadata": {"lastActivity": "2026-11-20T18:00:00Z"},
                "modelBreakdowns": [{"modelName": "claude-sonnet-5", "cost": 7.0}],
            },
        ]
        accounts, _no_account, daily_by_bucket, account_sessions = account_usage.compute_accounts(
            rows, {"sess-a": "acct-1", "sess-b": "acct-1"}, {"acct-1": {"label": "you@example.com", "note": None}},
        )
        self.assertEqual(len(accounts), 1)
        self.assertAlmostEqual(accounts[0]["cost"], 12.0)
        self.assertEqual(accounts[0]["sessions"], 2)
        self.assertEqual(accounts[0]["models"], {"claude-sonnet-5": 12.0})
        # same day, same account: one row in account_sessions per underlying session (caller sums these)
        self.assertEqual(len(account_sessions), 2)
        self.assertAlmostEqual(daily_by_bucket[0]["you@example.com"], 12.0)

    def test_session_project_comes_from_the_real_cwd_not_a_guess(self):
        rows = [
            {
                "agent": "claude", "period": "sess-1", "totalCost": 1.0, "totalTokens": 10,
                "metadata": {"lastActivity": "2026-11-20T08:00:00Z"}, "modelBreakdowns": [],
            },
            {
                "agent": "claude", "period": "sess-2", "totalCost": 2.0, "totalTokens": 20,
                "metadata": {"lastActivity": "2026-11-20T09:00:00Z"}, "modelBreakdowns": [],
            },
        ]
        _accounts, _no_account, _daily, account_sessions = account_usage.compute_accounts(
            rows, {}, {}, session_project_map={"sess-1": "/Users/tom/code/agentic-spend"},
        )
        found_known = [s for s in account_sessions if s["totalCost"] == 1.0][0]
        found_unknown = [s for s in account_sessions if s["totalCost"] == 2.0][0]
        self.assertEqual(found_known["project"], "/Users/tom/code/agentic-spend")
        self.assertEqual(found_unknown["project"], "Unattributed (no cwd recorded)")


if __name__ == "__main__":
    unittest.main()
