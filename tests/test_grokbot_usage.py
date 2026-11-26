import datetime
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))
os.environ.setdefault("AGENTIC_SPEND_DATA_DIR", tempfile.mkdtemp(prefix="agentic-spend-test-"))
os.environ.setdefault("GROK_BOT_PERSISTENCE_DIR", tempfile.mkdtemp(prefix="grokbot-persist-test-"))

import grokbot_usage  # noqa: E402


# Clearly fake figures used only in tests — not a real account reading.
FAKE_PCT = 12.5
FAKE_RESET_MS = int(datetime.datetime(2026, 1, 5, 14, 30, tzinfo=datetime.timezone.utc).timestamp() * 1000)
FAKE_READ_MS = int(datetime.datetime(2026, 1, 3, 9, 0, tzinfo=datetime.timezone.utc).timestamp() * 1000)
FAKE_EXPIRE_MS = FAKE_READ_MS + 24 * 60 * 60 * 1000
FAKE_CENTS = 125  # $1.25
FAKE_ACCOUNT_A = "test-account-personal"
FAKE_ACCOUNT_B = "test-account-work"
FAKE_PLAN = "Pro"
FAKE_PLAN_LABEL = "Example Bot Plan"


def _cache_payload(percent=FAKE_PCT, cents=FAKE_CENTS, plan=FAKE_PLAN, label=FAKE_PLAN_LABEL, kind="present"):
    return {
        "schemaVersion": 1,
        "value": {
            "kind": kind,
            "selectedTeamId": None,
            "reading": {
                "usage": {
                    "percentUsed": percent,
                    "nextResetMs": FAKE_RESET_MS,
                    "isSandTrial": False,
                    "hasNonZeroIncludedLimit": True,
                    "isTeamSeat": False,
                    "onDemand": None,
                    "prepaidBalanceCents": cents,
                    "wouldRefuseSend": False,
                    "grokPlanLabel": label,
                    "unifiedPlanName": plan,
                    "readForTeamId": None,
                },
                "readAtMs": FAKE_READ_MS,
            },
            "expiresAtMs": FAKE_EXPIRE_MS,
        },
    }


def _write_blob(directory, key, obj):
    path = os.path.join(directory, grokbot_usage.encode_slice_key(key) + ".blob")
    with open(path, "w") as f:
        json.dump(obj, f)
    return path


class SliceKeyTests(unittest.TestCase):
    def test_round_trip(self):
        key = f"sand.client.slice.account.{FAKE_ACCOUNT_A}.weekly-usage.cache"
        encoded = grokbot_usage.encode_slice_key(key)
        self.assertEqual(encoded, encoded.lower())
        self.assertFalse(encoded.endswith("="))
        self.assertEqual(grokbot_usage.decode_slice_key(encoded + ".blob"), key)

    def test_rejects_garbage(self):
        self.assertIsNone(grokbot_usage.decode_slice_key("!!!!.blob"))


class ParseCacheTests(unittest.TestCase):
    def test_reads_known_field_names(self):
        snap = grokbot_usage.parse_cache_payload(_cache_payload())
        self.assertEqual(snap["weeklyPct"], FAKE_PCT)
        self.assertEqual(snap["plan"], FAKE_PLAN)
        self.assertEqual(snap["planLabel"], FAKE_PLAN_LABEL)
        self.assertEqual(snap["creditsUsd"], 1.25)
        self.assertEqual(snap["resetAt"], "2026-01-05T14:30:00+00:00")
        self.assertEqual(snap["updatedAt"], "2026-01-03T09:00:00+00:00")
        self.assertEqual(snap["expiresAt"], "2026-01-04T09:00:00+00:00")
        self.assertEqual(snap["source"], "local-cache")
        self.assertIn("weekly limit shared with other apps", snap["note"])

    def test_rejects_missing_percent(self):
        obj = _cache_payload()
        del obj["value"]["reading"]["usage"]["percentUsed"]
        self.assertIsNone(grokbot_usage.parse_cache_payload(obj))

    def test_rejects_negative_percent(self):
        self.assertIsNone(grokbot_usage.parse_cache_payload(_cache_payload(percent=-1)))


class DiscoverTests(unittest.TestCase):
    def test_reads_two_accounts_and_marks_the_current_slot(self):
        with tempfile.TemporaryDirectory() as directory:
            _write_blob(
                directory,
                f"sand.client.slice.account.{FAKE_ACCOUNT_A}.weekly-usage.cache",
                _cache_payload(percent=12.5),
            )
            _write_blob(
                directory,
                f"sand.client.slice.account.{FAKE_ACCOUNT_B}.weekly-usage.cache",
                _cache_payload(percent=44.0, plan="Team"),
            )
            _write_blob(
                directory,
                grokbot_usage.ACCOUNT_SLOT_KEY,
                {"schemaVersion": 1, "value": FAKE_ACCOUNT_B},
            )
            # Unrelated slices must be ignored (no transcripts, no secrets).
            _write_blob(directory, "sand.client.slice.ui-layout", {"schemaVersion": 1, "value": {}})
            found = grokbot_usage.discover(directory)
            self.assertEqual(len(found), 2)
            by_id = {a["accountId"]: a for a in found}
            self.assertTrue(by_id[FAKE_ACCOUNT_B]["current"])
            self.assertFalse(by_id[FAKE_ACCOUNT_A]["current"])
            self.assertEqual(by_id[FAKE_ACCOUNT_A]["weeklyPct"], 12.5)
            self.assertEqual(by_id[FAKE_ACCOUNT_B]["plan"], "Team")
            # current account is sorted first
            self.assertEqual(found[0]["accountId"], FAKE_ACCOUNT_B)

    def test_missing_directory_is_empty_not_an_error(self):
        self.assertEqual(grokbot_usage.discover("/no/such/grok-bot-cache-dir"), [])

    def test_percent_encoded_pipe_in_the_filename_is_normalised(self):
        with tempfile.TemporaryDirectory() as directory:
            raw_id = "oauth-provider%7Cuser_TESTACCOUNT0001"
            _write_blob(
                directory,
                f"sand.client.slice.account.{raw_id}.weekly-usage.cache",
                _cache_payload(),
            )
            _write_blob(
                directory,
                grokbot_usage.ACCOUNT_SLOT_KEY,
                {"schemaVersion": 1, "value": "oauth-provider|user_TESTACCOUNT0001"},
            )
            found = grokbot_usage.discover(directory)
            self.assertEqual(len(found), 1)
            self.assertEqual(found[0]["accountId"], "oauth-provider|user_TESTACCOUNT0001")
            self.assertTrue(found[0]["current"])


class HistoryTests(unittest.TestCase):
    def test_dedupes_the_same_value_within_30_minutes(self):
        now = datetime.datetime(2026, 1, 3, 10, 0, tzinfo=datetime.timezone.utc)
        first = grokbot_usage.append_history([], 12.5, "2026-01-03T10:00:00+00:00", now=now)
        again = grokbot_usage.append_history(first, 12.5, "2026-01-03T10:20:00+00:00", now=now)
        self.assertEqual(len(again), 1)
        later = grokbot_usage.append_history(again, 12.5, "2026-01-03T10:31:00+00:00", now=now)
        self.assertEqual(len(later), 2)

    def test_keeps_a_change_even_within_30_minutes(self):
        now = datetime.datetime(2026, 1, 3, 10, 0, tzinfo=datetime.timezone.utc)
        first = grokbot_usage.append_history([], 12.5, "2026-01-03T10:00:00+00:00", now=now)
        changed = grokbot_usage.append_history(first, 13.0, "2026-01-03T10:05:00+00:00", now=now)
        self.assertEqual([p["weeklyPct"] for p in changed], [12.5, 13.0])

    def test_drops_points_older_than_60_days(self):
        now = datetime.datetime(2026, 3, 10, tzinfo=datetime.timezone.utc)
        history = [
            {"at": "2025-12-01T00:00:00+00:00", "weeklyPct": 1.0},
            {"at": "2026-02-01T00:00:00+00:00", "weeklyPct": 2.0},
        ]
        out = grokbot_usage.append_history(history, 3.0, "2026-03-10T00:00:00+00:00", now=now)
        self.assertEqual([p["weeklyPct"] for p in out], [2.0, 3.0])


class CollectTests(unittest.TestCase):
    def test_collect_writes_history_and_returns_accounts(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as data:
            _write_blob(
                directory,
                f"sand.client.slice.account.{FAKE_ACCOUNT_A}.weekly-usage.cache",
                _cache_payload(),
            )
            store = os.path.join(data, "grokbot-usage.json")
            report = grokbot_usage.collect(directory=directory, store_path=store)
            self.assertTrue(report["found"])
            self.assertEqual(report["source"], "local-cache")
            self.assertEqual(len(report["accounts"]), 1)
            self.assertEqual(report["accounts"][0]["weeklyPct"], FAKE_PCT)
            self.assertEqual(len(report["accounts"][0]["history"]), 1)
            # second collect with the same reading does not duplicate within 30 min
            # (updatedAt is the cache's readAt, identical)
            again = grokbot_usage.collect(directory=directory, store_path=store)
            self.assertEqual(len(again["accounts"][0]["history"]), 1)


class ValidateAndParseTests(unittest.TestCase):
    def test_validate_accepts_a_well_formed_snapshot(self):
        snap = grokbot_usage.validate_snapshot({
            "accountId": "work",
            "plan": "Pro",
            "weeklyPct": 12.5,
            "resetAt": "2026-01-05T14:30:00+00:00",
            "creditsUsd": 1.25,
        })
        self.assertEqual(snap["accountId"], "work")
        self.assertEqual(snap["weeklyPct"], 12.5)
        self.assertEqual(snap["creditsUsd"], 1.25)
        self.assertEqual(snap["source"], "push")

    def test_validate_rejects_a_bad_percent_and_a_path_like_id(self):
        with self.assertRaises(ValueError):
            grokbot_usage.validate_snapshot({"weeklyPct": "lots"})
        with self.assertRaises(ValueError):
            grokbot_usage.validate_snapshot({"accountId": "../etc/passwd", "weeklyPct": 1})
        with self.assertRaises(ValueError):
            grokbot_usage.validate_snapshot({"weeklyPct": -4})
        with self.assertRaises(ValueError):
            grokbot_usage.validate_snapshot({"weeklyPct": 10, "resetAt": "next Monday"})
        with self.assertRaises(ValueError):
            grokbot_usage.validate_snapshot({"weeklyPct": 10, "creditsUsd": "none"})

    def test_parse_report_text_accepts_a_fake_usage_report(self):
        text = (
            "Plan: Pro\n"
            "12.5% used of the weekly limit, shared with other apps on the plan.\n"
            "Resets Monday, Jan 5 at 14:30\n"
            "$1.25 of additional credits left.\n"
        )
        now = datetime.datetime(2026, 1, 3, tzinfo=datetime.timezone.utc)
        parsed = grokbot_usage.parse_report_text(text, now=now)
        self.assertEqual(parsed["weeklyPct"], 12.5)
        self.assertEqual(parsed["plan"], "Pro")
        self.assertEqual(parsed["creditsUsd"], 1.25)
        self.assertEqual(parsed["resetAt"], "2026-01-05T14:30:00+00:00")

    def test_parse_report_text_requires_a_percentage(self):
        with self.assertRaises(ValueError):
            grokbot_usage.parse_report_text("Plan: Pro\nno figure here")

    def test_validate_can_fill_fields_from_raw_text(self):
        snap = grokbot_usage.validate_snapshot({
            "accountId": "personal",
            "rawText": "Plan: Pro\n12.5% used\nResets Jan 5 at 14:30\n$1.25 credits left",
        })
        self.assertEqual(snap["weeklyPct"], 12.5)
        self.assertEqual(snap["plan"], "Pro")
        self.assertEqual(snap["accountId"], "personal")


if __name__ == "__main__":
    unittest.main()
