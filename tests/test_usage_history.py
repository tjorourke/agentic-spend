import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))
os.environ.setdefault("AGENTIC_SPEND_DATA_DIR", tempfile.mkdtemp(prefix="agentic-spend-test-"))

import account_usage  # noqa: E402
import provider_usage  # noqa: E402
import statusline  # noqa: E402


class CodexEpochToIsoTests(unittest.TestCase):
    def test_converts_unix_seconds_to_iso(self):
        self.assertEqual(provider_usage._codex_epoch_to_iso(1796917247), "2026-12-10T15:40:47+00:00")

    def test_none_stays_none(self):
        self.assertIsNone(provider_usage._codex_epoch_to_iso(None))


class ParseCodexRolloutRateLimitsTests(unittest.TestCase):
    def test_extracts_populated_samples_and_skips_empty_ones(self):
        lines = [
            json.dumps({"timestamp": "2026-11-10T18:20:24.251Z", "type": "event_msg",
                        "payload": {"type": "token_count", "rate_limits": {
                            "primary": {"used_percent": 3.0, "window_minutes": 43200, "resets_at": 1796917247},
                        }}}),
            # no rate_limits.primary at all — must be skipped, not crash
            json.dumps({"timestamp": "2026-11-10T18:20:29.829Z", "type": "event_msg",
                        "payload": {"type": "token_count", "rate_limits": {"primary": None}}}),
            # unrelated line type
            json.dumps({"timestamp": "2026-11-10T18:20:40.000Z", "type": "response_item", "payload": {}}),
        ]
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
            f.write("\n".join(lines) + "\n")
            path = f.name
        try:
            samples = provider_usage._parse_codex_rollout_rate_limits(path)
            self.assertEqual(len(samples), 1)
            self.assertEqual(samples[0]["percent"], 3.0)
            self.assertEqual(samples[0]["windowMins"], 43200)
            self.assertEqual(samples[0]["resetsAt"], "2026-12-10T15:40:47+00:00")
        finally:
            os.remove(path)


class GetCodexRateLimitHistoryCachingTests(unittest.TestCase):
    def test_second_call_on_an_unchanged_file_uses_the_cache_not_a_reparse(self):
        with tempfile.TemporaryDirectory() as tmp:
            orig_dir = provider_usage.CODEX_SESSIONS_DIR
            provider_usage.CODEX_SESSIONS_DIR = tmp
            provider_usage._codex_history_file_cache.clear()
            try:
                path = os.path.join(tmp, "rollout-test.jsonl")
                with open(path, "w") as f:
                    f.write(json.dumps({"timestamp": "2026-11-10T18:20:24.251Z", "type": "event_msg",
                                         "payload": {"type": "token_count", "rate_limits": {
                                             "primary": {"used_percent": 5.0, "window_minutes": 43200, "resets_at": None},
                                         }}}) + "\n")
                first = provider_usage.get_codex_rate_limit_history()
                self.assertEqual(len(first), 1)
                calls = {"n": 0}
                real_parse = provider_usage._parse_codex_rollout_rate_limits

                def counting_parse(p):
                    calls["n"] += 1
                    return real_parse(p)

                provider_usage._parse_codex_rollout_rate_limits = counting_parse
                try:
                    second = provider_usage.get_codex_rate_limit_history()
                    self.assertEqual(len(second), 1)
                    self.assertEqual(calls["n"], 0)  # unchanged file: served from cache
                finally:
                    provider_usage._parse_codex_rollout_rate_limits = real_parse
            finally:
                provider_usage.CODEX_SESSIONS_DIR = orig_dir
                provider_usage._codex_history_file_cache.clear()


def _codex_sample_line(time, percent, window_minutes):
    return json.dumps({"timestamp": time, "type": "event_msg", "payload": {"type": "token_count", "rate_limits": {
        "limit_id": "codex", "primary": {"used_percent": percent, "window_minutes": window_minutes, "resets_at": None},
    }}})


class GetCodexRateLimitHistoryFilteringTests(unittest.TestCase):
    """Codex's own notion of which window is "primary" isn't stable on a
    real machine (seen: 7-day and 30-day, both under the same limit_id) —
    mixing both into one series would plot two different percentages as if
    they were comparable."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.orig_dir = provider_usage.CODEX_SESSIONS_DIR
        provider_usage.CODEX_SESSIONS_DIR = self.tmp.name
        provider_usage._codex_history_file_cache.clear()

    def tearDown(self):
        provider_usage.CODEX_SESSIONS_DIR = self.orig_dir
        provider_usage._codex_history_file_cache.clear()
        self.tmp.cleanup()

    def test_only_the_most_recent_window_type_is_kept(self):
        path = os.path.join(self.tmp.name, "rollout-test.jsonl")
        with open(path, "w") as f:
            f.write(_codex_sample_line("2026-11-01T00:00:00Z", 10.0, 10080) + "\n")  # 7-day window, older
            f.write(_codex_sample_line("2026-11-10T00:00:00Z", 3.0, 43200) + "\n")   # 30-day window, more recent
        samples = provider_usage.get_codex_rate_limit_history()
        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0]["windowMins"], 43200)

    def test_long_runs_of_an_identical_reading_collapse_to_first_and_last(self):
        path = os.path.join(self.tmp.name, "rollout-test.jsonl")
        with open(path, "w") as f:
            for i in range(5):
                f.write(_codex_sample_line(f"2026-11-10T00:0{i}:00Z", 3.0, 43200) + "\n")
            f.write(_codex_sample_line("2026-11-10T00:05:00Z", 4.0, 43200) + "\n")  # the move that matters
        samples = provider_usage.get_codex_rate_limit_history()
        percents = [s["percent"] for s in samples]
        self.assertEqual(percents, [3.0, 3.0, 4.0])  # first of the run, last of the run, then the change


class StatuslineHistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.orig_path = statusline.HISTORY_PATH
        statusline.HISTORY_PATH = os.path.join(self.tmpdir.name, "usage-history.jsonl")

    def tearDown(self):
        statusline.HISTORY_PATH = self.orig_path
        self.tmpdir.cleanup()

    def test_identical_reading_is_not_written_twice(self):
        five_hour = {"used_percentage": 13, "resets_at": "2026-11-24T23:30:00Z"}
        seven_day = {"used_percentage": 21, "resets_at": "2026-11-26T22:00:00Z"}
        statusline.record_usage_history("acct-1", five_hour, seven_day)
        statusline.record_usage_history("acct-1", five_hour, seven_day)
        with open(statusline.HISTORY_PATH) as f:
            lines = f.readlines()
        self.assertEqual(len(lines), 1)

    def test_a_changed_percent_is_appended(self):
        statusline.record_usage_history("acct-1", {"used_percentage": 13, "resets_at": "t"}, {"used_percentage": 21, "resets_at": "t"})
        statusline.record_usage_history("acct-1", {"used_percentage": 14, "resets_at": "t"}, {"used_percentage": 21, "resets_at": "t"})
        with open(statusline.HISTORY_PATH) as f:
            lines = f.readlines()
        self.assertEqual(len(lines), 2)

    def test_two_accounts_interleaved_each_dedupe_independently(self):
        reading = {"used_percentage": 50, "resets_at": "t"}
        statusline.record_usage_history("acct-1", reading, reading)
        statusline.record_usage_history("acct-2", reading, reading)
        statusline.record_usage_history("acct-1", reading, reading)  # unchanged for acct-1
        with open(statusline.HISTORY_PATH) as f:
            lines = f.readlines()
        self.assertEqual(len(lines), 2)


class GetUsageHistoryTests(unittest.TestCase):
    def test_reads_back_what_statusline_wrote_and_attaches_a_label(self):
        with tempfile.TemporaryDirectory() as tmp:
            orig_data_dir = account_usage.data_dir.DATA_DIR
            account_usage.data_dir.DATA_DIR = tmp
            try:
                path = account_usage.data_dir.path("usage-history.jsonl")
                with open(path, "w") as f:
                    f.write(json.dumps({"time": "2026-11-24T18:00:00+00:00", "accountUuid": "acct-1",
                                         "fiveHourPercent": 13, "sevenDayPercent": 21}) + "\n")
                history = account_usage.get_usage_history()
                self.assertEqual(len(history), 1)
                self.assertEqual(history[0]["fiveHourPercent"], 13)
                self.assertIn("label", history[0])
            finally:
                account_usage.data_dir.DATA_DIR = orig_data_dir


if __name__ == "__main__":
    unittest.main()
