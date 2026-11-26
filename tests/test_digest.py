import datetime
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))
os.environ.setdefault("AGENTIC_SPEND_DATA_DIR", tempfile.mkdtemp(prefix="agentic-spend-test-"))

import digest  # noqa: E402

UTC = datetime.timezone.utc


def day(period, cost, agent="claude", model="m1", inp=100, out=50):
    mb = {"modelName": model, "cost": cost, "inputTokens": inp, "outputTokens": out}
    return {"period": period, "totalCost": cost, "modelBreakdowns": [mb],
            "agents": [{"agent": agent, "totalCost": cost, "modelBreakdowns": [mb]}],
            "metadata": {"agents": [agent]}}


def payload():
    return {
        "daily": [
            day("2026-11-10", 5.0),               # week before last (Mon 9 - Sun 15)
            day("2026-11-17", 10.0),              # last complete week (Mon 16 - Sun 22)
            day("2026-11-20", 6.0, agent="opencode", model="m2"),
            day("2026-11-24", 99.0),              # this week, excluded
        ],
        "accountSessions": [
            {"accountUuid": "u1", "agent": "claude", "period": "2026-11-17", "totalCost": 10.0, "project": "/code/a"},
            {"accountUuid": "u1", "agent": "claude", "period": "2026-11-24", "totalCost": 99.0, "project": "/code/b"},
        ],
        "sources": [
            {"id": "u1", "tool": "claude", "kind": "official-live", "maxUsage": {"usageCredits": {"usedMinor": 0}}},
            {"id": "opencode", "tool": "opencode", "kind": "none"},
        ],
        "plans": {},
        "grokBot": {"accounts": [{
            "accountId": "a", "plan": "Super",
            "history": [{"at": "2026-11-12T10:00:00+00:00", "weeklyPct": 50.0},
                        {"at": "2026-11-21T10:00:00+00:00", "weeklyPct": 72.0}],
            "windows": [{"resetAt": "2026-11-22T09:00:00+00:00", "peakPct": 80.0,
                         "lastReadingAt": "2026-11-22T08:00:00+00:00"}],
        }]},
        "usageHistory": {"claude": [], "codex": []},
        "maxUsage": [],
    }


class DigestTests(unittest.TestCase):
    def test_last_complete_week(self):
        self.assertEqual(digest.last_complete_week(datetime.date(2026, 11, 26)),
                         (datetime.date(2026, 11, 16), datetime.date(2026, 11, 22)))
        # On a Monday, the week that ended yesterday.
        self.assertEqual(digest.last_complete_week(datetime.date(2026, 11, 23)),
                         (datetime.date(2026, 11, 16), datetime.date(2026, 11, 22)))

    def test_build(self):
        d = digest.build_digest(payload(), today=datetime.date(2026, 11, 26), tz=UTC)
        self.assertEqual(d["weekStart"], "2026-11-16")
        self.assertEqual(d["spend"]["list"], 16.0)
        # Claude covered (no credits used); opencode has no plan, so it is real.
        self.assertEqual(d["spend"]["real"], 6.0)
        self.assertEqual(d["spend"]["covered"], 10.0)
        self.assertEqual(d["spend"]["previousList"], 5.0)
        self.assertEqual(d["spend"]["changePct"], 220.0)
        self.assertEqual(d["tokens"]["total"], 300)
        self.assertEqual(d["topModels"][0]["model"], "m1")
        self.assertEqual(d["topProjects"], [{"project": "/code/a", "cost": 10.0, "sessions": 1, "tokens": 0}])
        self.assertEqual(d["agents"][0], {"agent": "claude", "cost": 10.0, "tokens": 150})
        gb = d["plans"][0]
        self.assertEqual(gb["peakPct"], 80.0)
        self.assertEqual(gb["previousPeakPct"], 50.0)
        self.assertEqual(gb["deltaPct"], 30.0)

    def test_no_previous_data_hides_change(self):
        p = payload()
        p["daily"] = [x for x in p["daily"] if x["period"] >= "2026-11-16"]
        d = digest.build_digest(p, today=datetime.date(2026, 11, 26), tz=UTC)
        self.assertIsNone(d["spend"]["changePct"])
        self.assertIsNone(d["tokens"]["previous"])

    def test_empty_payload(self):
        d = digest.build_digest({}, today=datetime.date(2026, 11, 26), tz=UTC)
        self.assertEqual(d["spend"]["list"], 0.0)
        self.assertEqual(d["plans"], [])


if __name__ == "__main__":
    unittest.main()
