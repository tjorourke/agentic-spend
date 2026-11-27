"""1.8.7: a Claude session's cost is spread over the days it ran, so account
and project pages add up to the same daily figures as the Overview."""
import datetime
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("AGENTIC_SPEND_DATA_DIR", tempfile.mkdtemp())

import account_usage  # noqa: E402
import session_days  # noqa: E402

UTC = datetime.timezone.utc


def line(mid, ts, model="claude-opus-4-5", inp=0, out=0, read=0, write=0):
    return json.dumps({"timestamp": ts, "requestId": "r" + mid, "message": {
        "id": mid, "model": model, "usage": {
            "input_tokens": inp, "output_tokens": out,
            "cache_read_input_tokens": read, "cache_creation_input_tokens": write}}})


class WeightTests(unittest.TestCase):
    def test_weight_follows_price_ratios(self):
        self.assertEqual(session_days.message_weight({"input_tokens": 10, "output_tokens": 2}), 20)
        self.assertAlmostEqual(session_days.message_weight({"cache_read_input_tokens": 100}), 10)
        self.assertAlmostEqual(session_days.message_weight({"cache_creation_input_tokens": 100}), 125)
        self.assertAlmostEqual(session_days.message_weight({"cache_creation": {
            "ephemeral_5m_input_tokens": 100, "ephemeral_1h_input_tokens": 100},
            "cache_creation_input_tokens": 200}), 325)


class CollectTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.proj = os.path.join(self.dir, "p1")
        os.makedirs(os.path.join(self.proj, "s1", "subagents"))
        self.cache = os.path.join(self.dir, "cache.json")

    def write(self, rel, lines, mode="w"):
        with open(os.path.join(self.proj, rel), mode) as f:
            f.write("".join(l + "\n" for l in lines))

    def test_days_models_duplicates_and_subagents(self):
        self.write("s1.jsonl", [
            line("a", "2026-11-24T10:00:00Z", inp=100),
            line("a", "2026-11-24T10:00:00Z", inp=100),  # same message, second content block
            line("b", "2026-11-25T23:30:00Z", inp=50, model="claude-sonnet-4-5"),
        ])
        self.write("s1/subagents/agent-x.jsonl", [line("c", "2026-11-25T01:00:00Z", inp=30)])
        w = session_days.collect(self.dir, self.cache, tz=UTC)
        self.assertEqual(w["s1"]["2026-11-24"], {"claude-opus-4-5": 100})
        self.assertEqual(w["s1"]["2026-11-25"], {"claude-sonnet-4-5": 50, "claude-opus-4-5": 30})

    def test_local_timezone_decides_the_day(self):
        self.write("s1.jsonl", [line("a", "2026-11-25T23:30:00Z", inp=10)])
        w = session_days.collect(self.dir, self.cache, tz=datetime.timezone(datetime.timedelta(hours=2)))
        self.assertEqual(list(w["s1"]), ["2026-11-26"])

    def test_appended_lines_are_read_once_and_half_lines_wait(self):
        self.write("s1.jsonl", [line("a", "2026-11-24T10:00:00Z", inp=10)])
        session_days.collect(self.dir, self.cache, tz=UTC)
        with open(os.path.join(self.proj, "s1.jsonl"), "a") as f:
            f.write(line("b", "2026-11-25T10:00:00Z", inp=5) + "\n" + line("c", "2026-11-25T11:00:00Z", inp=7)[:20])
        w = session_days.collect(self.dir, self.cache, tz=UTC)
        self.assertEqual(w["s1"], {"2026-11-24": {"claude-opus-4-5": 10}, "2026-11-25": {"claude-opus-4-5": 5}})
        with open(os.path.join(self.proj, "s1.jsonl"), "a") as f:
            f.write(line("c", "2026-11-25T11:00:00Z", inp=7)[20:] + "\n")
        w = session_days.collect(self.dir, self.cache, tz=UTC)
        self.assertEqual(w["s1"]["2026-11-25"], {"claude-opus-4-5": 12})

    def test_resumed_session_does_not_count_copied_messages_again(self):
        self.write("s1.jsonl", [line("a", "2026-11-24T10:00:00Z", inp=10)])
        self.write("s2.jsonl", [line("a", "2026-11-24T10:00:00Z", inp=10),
                                line("b", "2026-11-26T10:00:00Z", inp=4)])
        w = session_days.collect(self.dir, self.cache, tz=UTC)
        self.assertEqual(w["s2"], {"2026-11-26": {"claude-opus-4-5": 4}})

    def test_missing_folder_is_empty(self):
        self.assertEqual(session_days.collect(os.path.join(self.dir, "nope"), self.cache, tz=UTC), {})


def session_row(sid, last, cost, models):
    return {"period": sid, "agent": "claude", "totalCost": cost, "totalTokens": 1000,
            "metadata": {"lastActivity": last + "T12:00:00"},
            "modelBreakdowns": [{"modelName": m, "cost": c, "inputTokens": 1000} for m, c in models]}


class SplitTests(unittest.TestCase):
    def test_cost_split_by_model_share(self):
        row = session_row("s1", "2026-11-25", 30.0, [("opus", 20.0), ("sonnet", 10.0)])
        pieces = session_days.split_session(row, "2026-11-25", {
            "2026-11-24": {"opus": 3, "sonnet": 1}, "2026-11-25": {"opus": 1, "sonnet": 1}})
        by_day = {d: c for d, c, _t, _b in pieces}
        self.assertAlmostEqual(by_day["2026-11-24"], 15 + 5)
        self.assertAlmostEqual(by_day["2026-11-25"], 5 + 5)
        self.assertAlmostEqual(sum(by_day.values()), 30.0)

    def test_without_weights_it_stays_on_last_day(self):
        row = session_row("s1", "2026-11-25", 30.0, [("opus", 30.0)])
        self.assertEqual([p[0] for p in session_days.split_session(row, "2026-11-25", None)], ["2026-11-25"])

    def test_compute_accounts_rows_per_day_and_one_session_count(self):
        row = session_row("s1", "2026-11-25", 30.0, [("opus", 30.0)])
        _a, _n, daily, sessions = account_usage.compute_accounts(
            [row], {"s1": "u1"}, {"u1": {"label": "work@x"}}, {"s1": "proj"},
            {"s1": {"2026-11-24": {"opus": 2}, "2026-11-25": {"opus": 1}}})
        self.assertEqual([(s["period"], round(s["totalCost"], 2), s["sessions"]) for s in sessions],
                         [("2026-11-24", 20.0, 0), ("2026-11-25", 10.0, 1)])
        self.assertEqual({d["period"]: round(d["work@x"], 2) for d in daily},
                         {"2026-11-24": 20.0, "2026-11-25": 10.0})

    def test_reconcile_matches_daily_totals_and_keeps_session_totals(self):
        a = session_row("a", "2026-11-25", 30.0, [("opus", 30.0)])
        b = session_row("b", "2026-11-25", 10.0, [("opus", 10.0)])
        weights = {"a": {"2026-11-24": {"opus": 1}, "2026-11-25": {"opus": 1}}}
        _a, _n, daily, sessions = account_usage.compute_accounts(
            [a, b], {"a": "u1", "b": "u1"}, {"u1": {"label": "w"}}, {}, weights,
            {"2026-11-24": 12.0, "2026-11-25": 28.0})
        days = {d["period"]: d["w"] for d in daily}
        self.assertAlmostEqual(days["2026-11-24"], 12.0, places=2)
        self.assertAlmostEqual(days["2026-11-25"], 28.0, places=2)
        self.assertAlmostEqual(sum(s["totalCost"] for s in sessions if s["accountUuid"] == "u1"), 40.0)
        self.assertEqual(sum(s["sessions"] for s in sessions), 2)

    def test_reconcile_leaves_split_alone_when_sources_disagree(self):
        a = session_row("a", "2026-11-25", 30.0, [("opus", 30.0)])
        weights = {"a": {"2026-11-24": {"opus": 1}, "2026-11-25": {"opus": 1}}}
        _a, _n, daily, _s = account_usage.compute_accounts(
            [a], {"a": "u1"}, {"u1": {"label": "w"}}, {}, weights, {"2026-11-24": 100.0, "2026-11-25": 1.0})
        self.assertEqual({d["period"]: round(d["w"], 2) for d in daily}, {"2026-11-24": 15.0, "2026-11-25": 15.0})


if __name__ == "__main__":
    unittest.main()
