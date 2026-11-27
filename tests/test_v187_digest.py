"""1.8.7 weekly report: all seven days, Claude by account, and real spend
from that week's readings only."""
import datetime
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import digest  # noqa: E402

UTC = datetime.timezone.utc
MON, SUN = datetime.date(2026, 11, 16), datetime.date(2026, 11, 22)


def payload(history=None):
    return {
        "daily": [{"period": "2026-11-17", "totalCost": 40.0, "modelBreakdowns": [{"modelName": "m", "cost": 40.0, "inputTokens": 100}],
                   "agents": [{"agent": "claude", "totalCost": 30.0, "modelBreakdowns": [{"modelName": "m", "cost": 30.0, "inputTokens": 100}]},
                              {"agent": "codex", "totalCost": 10.0, "modelBreakdowns": [{"modelName": "g", "cost": 10.0}]}]}],
        "accountSessions": [
            {"accountUuid": "w", "label": "Work", "period": "2026-11-17", "totalCost": 20.0},
            {"accountUuid": "p", "label": "Personal", "period": "2026-11-17", "totalCost": 10.0},
        ],
        "sources": [
            {"id": "w", "label": "Work", "tool": "claude", "kind": "official-live", "maxUsage": {"usageCredits": {"usedMinor": 0}}},
            {"id": "p", "label": "Personal", "tool": "claude", "kind": "official-live", "maxUsage": {"usageCredits": {"usedMinor": 0}}},
            {"id": "codex", "label": "Codex", "tool": "codex", "kind": "official-live", "codexLive": {"ordinaryUsageAllowed": True}},
        ],
        "usageHistory": {"claude": history or [], "codex": []},
        "maxUsage": [{"accountUuid": "w", "label": "Work"}, {"accountUuid": "p", "label": "Personal"}],
    }


class WeekTests(unittest.TestCase):
    def build(self, p):
        return digest.build_digest(p, today=datetime.date(2026, 11, 26), tz=UTC)

    def test_all_seven_days_with_zeros(self):
        d = self.build(payload())
        self.assertEqual([x["date"] for x in d["days"]], [(MON + datetime.timedelta(days=i)).isoformat() for i in range(7)])
        self.assertEqual([x["cost"] for x in d["days"]], [0, 40.0, 0, 0, 0, 0, 0])

    def test_claude_row_split_by_account_adds_up(self):
        rows = self.build(payload())["agents"]
        claude = [r for r in rows if r["agent"] == "claude"]
        self.assertEqual([(r["account"], r["cost"]) for r in claude], [("Work", 20.0), ("Personal", 10.0)])
        self.assertAlmostEqual(sum(r["cost"] for r in rows), 40.0)

    def test_past_week_uses_that_weeks_readings_not_todays_status(self):
        d = self.build(payload())
        self.assertEqual((d["spend"]["real"], d["spend"]["covered"], d["spend"]["unknown"]), (0, 0, 40.0))
        self.assertEqual({n["label"]: n["status"] for n in d["spend"]["notes"]},
                         {"Work": "no-readings", "Personal": "no-readings", "Codex": "no-readings"})

    def test_reading_under_100_is_covered_and_at_100_not_known(self):
        hist = [{"accountUuid": "w", "time": "2026-11-18T10:00:00+00:00", "sevenDayPercent": 60},
                {"accountUuid": "p", "time": "2026-11-18T10:00:00+00:00", "sevenDayPercent": 100}]
        d = self.build(payload(hist))
        self.assertEqual((d["spend"]["covered"], d["spend"]["unknown"]), (20.0, 20.0))
        st = {n["label"]: (n["status"], n["peakPct"]) for n in d["spend"]["notes"]}
        self.assertEqual(st["Work"], ("covered", 60.0))
        self.assertEqual(st["Personal"], ("hit-limit", 100.0))

    def test_reading_from_another_week_does_not_count(self):
        hist = [{"accountUuid": "w", "time": "2026-11-25T10:00:00+00:00", "sevenDayPercent": 10}]
        d = self.build(payload(hist))
        self.assertEqual(d["spend"]["covered"], 0)

    def test_empty_week_has_no_data_but_still_seven_days(self):
        d = digest.build_digest(payload(), today=datetime.date(2026, 11, 26), tz=UTC, week=datetime.date(2026, 11, 2))
        self.assertFalse(d["hasData"])
        self.assertEqual(len(d["days"]), 7)


if __name__ == "__main__":
    unittest.main()
