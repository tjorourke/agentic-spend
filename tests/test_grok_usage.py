import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))
os.environ.setdefault("AGENTIC_SPEND_DATA_DIR", tempfile.mkdtemp(prefix="agentic-spend-test-"))

import grok_usage  # noqa: E402


def _write(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        if isinstance(obj, str):
            f.write(obj)
        else:
            json.dump(obj, f)


class TickConversionTests(unittest.TestCase):
    def test_docs_example_is_one_e10_ticks_per_usd(self):
        # grok headless guide: 126890500 ticks == $0.01268905
        self.assertAlmostEqual(grok_usage.ticks_to_usd(126890500), 0.01268905)


class CollectTests(unittest.TestCase):
    def test_turns_are_bucketed_by_local_day_and_cost_is_apportioned(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as data:
            project = "/home/example/app"
            encoded = project.replace("/", "%2F")
            session = os.path.join(root, "sessions", encoded, "sess-1")
            _write(os.path.join(session, "usage.json"), {
                "sessionId": "sess-1",
                "updatedAt": "2026-11-26T18:00:00Z",
                "session": {
                    "inputTokens": 100,
                    "outputTokens": 40,
                    "cachedReadTokens": 30,
                    "cacheCreationTokens": 10,
                    "reasoningTokens": 5,
                    "totalTokens": 150,
                    "costUsdTicks": 10 ** 10,
                },
                "turns": [
                    {
                        "turnNumber": 1,
                        "endedAt": "2026-11-25T12:00:00Z",
                        "inputTokens": 40,
                        "outputTokens": 10,
                        "cachedReadTokens": 10,
                        "cacheCreationTokens": 0,
                        "reasoningTokens": 0,
                        "totalTokens": 50,
                    },
                    {
                        "turnNumber": 2,
                        "endedAt": "2026-11-26T12:00:00Z",
                        "inputTokens": 60,
                        "outputTokens": 30,
                        "cachedReadTokens": 20,
                        "cacheCreationTokens": 10,
                        "reasoningTokens": 5,
                        "totalTokens": 100,
                    },
                ],
            })
            _write(os.path.join(session, "signals.json"), {"modelsUsed": ["grok-4"]})
            _write(os.path.join(session, "updates.jsonl"), json.dumps({
                "modelId": "grok-4",
                "modelUsage": {"grok-4": {"inputTokens": 100, "outputTokens": 40}},
            }) + "\n")
            attr = os.path.join(data, "grok-account-map.json")
            first = grok_usage.collect(root=root, attribution_path=attr, current_email="person@example.com")
            self.assertTrue(first["found"])
            self.assertEqual([d["period"] for d in first["days"]], ["2026-11-25", "2026-11-26"])
            by_day = {d["period"]: d for d in first["days"]}
            self.assertAlmostEqual(by_day["2026-11-25"]["totalCost"], 1 / 3, places=6)
            self.assertAlmostEqual(by_day["2026-11-26"]["totalCost"], 2 / 3, places=6)
            self.assertTrue(all(d["costKnown"] for d in first["days"]))
            self.assertEqual(by_day["2026-11-26"]["modelBreakdowns"][0]["modelName"], "grok-4")
            # inputTokens include cache, so uncached is input - cached read - cache write
            self.assertEqual(by_day["2026-11-26"]["modelBreakdowns"][0]["inputTokens"], 30)
            self.assertEqual(first["sessions"][0]["project"], project)
            self.assertEqual(first["sessions"][0]["accountUuid"], grok_usage.UNATTRIBUTED_ID)

            # A later file, updated after the watermark, takes the signed-in account.
            session2 = os.path.join(root, "sessions", encoded, "sess-2")
            _write(os.path.join(session2, "usage.json"), {
                "sessionId": "sess-2",
                "updatedAt": "2026-11-27T12:00:00Z",
                "session": {"totalTokens": 10, "inputTokens": 8, "outputTokens": 2, "costUsdTicks": 0},
                "turns": [{
                    "turnNumber": 1, "endedAt": "2026-11-27T12:00:00Z",
                    "inputTokens": 8, "outputTokens": 2, "totalTokens": 10,
                }],
            })
            second = grok_usage.collect(root=root, attribution_path=attr, current_email="person@example.com")
            tagged = [s for s in second["sessions"] if s["accountUuid"] == "grok:person@example.com"]
            self.assertEqual(len(tagged), 1)
            self.assertEqual(tagged[0]["label"], "person@example.com")
            old = [s for s in second["sessions"] if s["accountUuid"] == grok_usage.UNATTRIBUTED_ID]
            self.assertTrue(old)

    def test_missing_ticks_keep_tokens_and_mark_cost_unknown(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as data:
            session = os.path.join(root, "sessions", "cwd", "sess-3")
            _write(os.path.join(session, "usage.json"), {
                "sessionId": "sess-3",
                "updatedAt": "2026-11-26T00:00:00Z",
                "session": {"totalTokens": 5, "inputTokens": 4, "outputTokens": 1},
                "turns": [{
                    "turnNumber": 1, "endedAt": "2026-11-26T00:00:00Z",
                    "inputTokens": 4, "outputTokens": 1, "totalTokens": 5,
                }],
            })
            report = grok_usage.collect(root=root, attribution_path=os.path.join(data, "map.json"), current_email=None)
            self.assertFalse(report["days"][0]["costKnown"])
            self.assertEqual(report["days"][0]["totalCost"], 0.0)
            self.assertGreater(report["sessions"][0]["totalTokens"], 0)


class MergeDailyTests(unittest.TestCase):
    def test_ccusage_grok_slice_is_replaced_not_added(self):
        daily = [{
            "period": "2026-11-26",
            "totalCost": 5.0,
            "modelBreakdowns": [
                {"modelName": "claude", "cost": 3.0, "inputTokens": 10, "outputTokens": 1, "cacheReadTokens": 0, "cacheCreationTokens": 0},
                {"modelName": "old-grok", "cost": 2.0, "inputTokens": 4, "outputTokens": 1, "cacheReadTokens": 0, "cacheCreationTokens": 0},
            ],
            "metadata": {"agents": ["claude", "grok"]},
            "agents": [
                {"agent": "claude", "totalCost": 3.0, "modelBreakdowns": [
                    {"modelName": "claude", "cost": 3.0, "inputTokens": 10, "outputTokens": 1, "cacheReadTokens": 0, "cacheCreationTokens": 0},
                ]},
                {"agent": "grok", "totalCost": 2.0, "modelBreakdowns": [
                    {"modelName": "old-grok", "cost": 2.0, "inputTokens": 4, "outputTokens": 1, "cacheReadTokens": 0, "cacheCreationTokens": 0},
                ]},
            ],
        }]
        grok_days = [{
            "period": "2026-11-26",
            "totalCost": 0.5,
            "modelBreakdowns": [
                {"modelName": "grok-4", "cost": 0.5, "inputTokens": 8, "outputTokens": 2, "cacheReadTokens": 0, "cacheCreationTokens": 0},
            ],
        }]
        merged = grok_usage.merge_daily(daily, grok_days)
        self.assertEqual(len(merged), 1)
        self.assertAlmostEqual(merged[0]["totalCost"], 3.5)
        agents = {a["agent"]: a["totalCost"] for a in merged[0]["agents"]}
        self.assertEqual(agents["grok"], 0.5)
        self.assertNotIn("old-grok", {m["modelName"] for m in merged[0]["modelBreakdowns"]})


if __name__ == "__main__":
    unittest.main()
