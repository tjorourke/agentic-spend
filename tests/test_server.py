import datetime
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

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


class TokenSourcesRangeTests(unittest.TestCase):
    def setUp(self):
        self._saved = dict(server._token_sources_cache)

    def tearDown(self):
        server._token_sources_cache.clear()
        server._token_sources_cache.update(self._saved)

    def test_long_span_is_capped_at_90_days(self):
        since, until, _since_dt, _until_dt, capped = server.parse_token_sources_range(
            "since=2020-01-01&until=2026-11-26", today=datetime.date(2026, 11, 26),
        )
        self.assertTrue(capped)
        self.assertEqual(until, datetime.date(2026, 11, 26))
        self.assertEqual((until - since).days + 1, 90)

    def test_bad_date_is_rejected(self):
        with self.assertRaises(ValueError):
            server.parse_token_sources_range("since=11-26-2026")

    def test_ranges_are_cached_separately(self):
        calls = []

        def fake(since_dt=None, until_dt=None):
            calls.append((since_dt, until_dt))
            return {"summary": {"claude": {}, "codex": {}}, "generatedAt": "t"}

        with mock.patch.object(server.token_sources, "get_token_sources", side_effect=fake):
            server._token_sources_cache.clear()
            first = server.get_token_sources_cached("since=2026-11-01&until=2026-11-07")
            second = server.get_token_sources_cached("since=2026-11-20&until=2026-11-26")
            again = server.get_token_sources_cached("since=2026-11-01&until=2026-11-07")
        self.assertEqual(len(calls), 2)
        self.assertEqual(first["since"], "2026-11-01")
        self.assertEqual(second["until"], "2026-11-26")
        self.assertIs(again, first)


class GetUsageStaleTests(unittest.TestCase):
    def setUp(self):
        self._saved = dict(server._cache)

    def tearDown(self):
        server._cache.clear()
        server._cache.update(self._saved)

    def test_forced_refresh_failure_returns_cached_payload_with_flag(self):
        server._cache["data"] = {"generatedAt": "2026-11-20T10:00:00", "daily": []}
        server._cache["ts"] = time.time()
        with mock.patch.object(server, "refresh_usage", return_value={"error": "ccusage failed"}):
            result = server.get_usage(force=True)
        self.assertTrue(result["stale"])
        self.assertEqual(result["refreshError"], "ccusage failed")
        self.assertEqual(result["generatedAt"], "2026-11-20T10:00:00")
        self.assertNotIn("stale", server._cache["data"])
        self.assertNotIn("refreshError", server._cache["data"])

    def test_successful_refresh_is_not_stale(self):
        with mock.patch.object(server, "refresh_usage", return_value={"generatedAt": "2026-11-26T12:00:00", "daily": []}):
            result = server.get_usage(force=True)
        self.assertFalse(result["stale"])
        self.assertNotIn("refreshError", result)
        self.assertEqual(server._cache["data"]["generatedAt"], "2026-11-26T12:00:00")

    def test_refresh_failure_with_no_cache_returns_the_error(self):
        server._cache["data"] = None
        server._cache["ts"] = 0.0
        with mock.patch.object(server, "refresh_usage", return_value={"error": "ccusage failed", "errorType": "node_missing"}):
            result = server.get_usage(force=True)
        self.assertEqual(result["error"], "ccusage failed")
        self.assertNotIn("stale", result)


class LabelValidationTests(unittest.TestCase):
    def test_accepts_a_short_name(self):
        account, label, note, note_provided = server.validate_label_body({
            "accountUuid": "acct-1",
            "label": "  Work laptop  ",
        })
        self.assertEqual(account, "acct-1")
        self.assertEqual(label, "Work laptop")
        self.assertIsNone(note)
        self.assertFalse(note_provided)

    def test_rejects_blank_path_and_control_characters(self):
        with self.assertRaises(ValueError):
            server.validate_label_body({"accountUuid": "acct-1", "label": "   "})
        with self.assertRaises(ValueError):
            server.validate_label_body({"accountUuid": "../etc/passwd", "label": "Work"})
        with self.assertRaises(ValueError):
            server.validate_label_body({"accountUuid": "acct-1", "label": "bad\nname"})
        with self.assertRaises(ValueError):
            server.validate_label_body({"accountUuid": "acct-1", "label": "x" * 81})

    def test_save_preserves_an_existing_note(self):
        server.save_account_label("acct-9", "Work", "checked by hand", True)
        server.save_account_label("acct-9", "Home", None, False)
        saved = server.account_usage.load_labels()["acct-9"]
        self.assertEqual(saved["label"], "Home")
        self.assertEqual(saved["note"], "checked by hand")

    def test_request_is_local_rejects_a_foreign_origin(self):
        self.assertTrue(server.request_is_local("127.0.0.1:8787", None))
        self.assertTrue(server.request_is_local("localhost:8787", "http://localhost:8787"))
        self.assertFalse(server.request_is_local("127.0.0.1:8787", "https://example.com"))
        self.assertFalse(server.request_is_local("evil.example", None))
        self.assertTrue(server.request_is_local("[::1]:8787", None))


class LabelEndpointTests(unittest.TestCase):
    def setUp(self):
        self.httpd = server.http.server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        # Isolate this test's labels from LabelValidationTests, which shares
        # the process-wide data dir set up before import.
        labels_path = server.account_usage.LABELS_PATH
        if os.path.exists(labels_path):
            os.remove(labels_path)

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def _request(self, method, path, body=None, headers=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=data,
            headers=headers or {},
            method=method,
        )
        try:
            with urllib.request.urlopen(req) as res:
                return res.status, json.load(res)
        except urllib.error.HTTPError as e:
            payload = e.read().decode()
            return e.code, json.loads(payload) if payload else {}

    def test_post_then_get_round_trip(self):
        code, payload = self._request(
            "POST", "/api/labels",
            {"accountUuid": "acct-1", "label": "Work"},
            {"Content-Type": "application/json"},
        )
        self.assertEqual(code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["label"], "Work")
        code, payload = self._request("GET", "/api/labels")
        self.assertEqual(code, 200)
        self.assertEqual(payload["labels"]["acct-1"]["label"], "Work")

    def test_foreign_origin_is_forbidden(self):
        code, payload = self._request(
            "POST", "/api/labels",
            {"accountUuid": "acct-1", "label": "Work"},
            {"Content-Type": "application/json", "Origin": "https://example.com"},
        )
        self.assertEqual(code, 403)
        self.assertFalse(payload["ok"])
        self.assertFalse(os.path.exists(server.account_usage.LABELS_PATH))


if __name__ == "__main__":
    unittest.main()
