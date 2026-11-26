"""Plan-window merge: per-field live/history/cached, reset-to-0%, as-of notes."""
import datetime
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))
os.environ.setdefault("AGENTIC_SPEND_DATA_DIR", tempfile.mkdtemp(prefix="agentic-spend-test-"))

import account_usage as au  # noqa: E402
import insights  # noqa: E402
import statusline  # noqa: E402

UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 11, 26, 17, 34, tzinfo=UTC)
TZ = UTC


def r(**kw):
    return au.resolve_window([kw] if kw.get("percent") is not None or kw.get("resetsAt") else [], now=NOW, tz=TZ, last_activity=kw.pop("last_activity", None) if False else None)


class ResolveWindowTests(unittest.TestCase):
    def resolve(self, readings, last_activity=None, now=NOW):
        return au.resolve_window(readings, now=now, last_activity=last_activity, tz=TZ)

    def test_reset_window_shows_zero_and_no_use_since(self):
        out = self.resolve([{
            "percent": 24, "resetsAt": "2026-11-25T22:00:00+00:00",
            "at": "2026-11-25T09:36:21+00:00", "source": "live",
        }])
        self.assertEqual(out["percent"], 0)
        self.assertEqual(out["state"], "reset")
        self.assertEqual(out["note"], "reset Wed 22:00, no use since")
        self.assertTrue(out["live"])

    def test_in_window_with_no_later_activity_says_no_use_since(self):
        out = self.resolve([{
            "percent": 9, "resetsAt": "2026-11-27T15:00:00+00:00",
            "at": "2026-11-26T17:23:26+00:00", "source": "live",
        }], last_activity="2026-11-26T17:23:30+00:00")  # within 5-minute grace
        self.assertEqual(out["percent"], 9)
        self.assertEqual(out["state"], "current")
        self.assertEqual(out["note"], "as of 17:23, no use since")

    def test_in_window_without_activity_confirmation_says_last_reading(self):
        out = self.resolve([{
            "percent": 9, "resetsAt": "2026-11-27T15:00:00+00:00",
            "at": "2026-11-26T17:23:26+00:00", "source": "live",
        }], last_activity=None)
        self.assertEqual(out["percent"], 9)
        self.assertEqual(out["note"], "last reading 17:23")

    def test_in_window_with_later_activity_says_last_reading(self):
        out = self.resolve([{
            "percent": 9, "resetsAt": "2026-11-27T15:00:00+00:00",
            "at": "2026-11-26T12:00:00+00:00", "source": "live",
        }], last_activity="2026-11-26T16:00:00+00:00")
        self.assertEqual(out["note"], "last reading 12:00")

    def test_prefers_newer_live_week_over_cached_and_history_for_session(self):
        # Tom's Team case: live week 9% with fiveHourPercent null; history
        # still has a 0% session in the current window; cached session is stale.
        session = au.resolve_window([
            {"percent": None, "resetsAt": None, "at": "2026-11-26T17:23:26+00:00", "source": "live"},
            {"percent": 0, "resetsAt": "2026-11-26T22:10:00+00:00", "at": "2026-11-26T17:23:07+00:00", "source": "history"},
            {"percent": 3, "resetsAt": "2026-11-25T14:00:00+00:00", "at": "2026-11-25T10:25:11+00:00", "source": "cached"},
        ], now=NOW, last_activity="2026-11-26T17:23:07+00:00", tz=TZ)
        week = au.resolve_window([
            {"percent": 9, "resetsAt": "2026-11-27T15:00:00+00:00", "at": "2026-11-26T17:23:26+00:00", "source": "live"},
            {"percent": 9, "resetsAt": "2026-11-27T15:00:00+00:00", "at": "2026-11-26T17:23:07+00:00", "source": "history"},
            {"percent": 9, "resetsAt": "2026-11-27T15:00:00+00:00", "at": "2026-11-25T10:25:11+00:00", "source": "cached"},
        ], now=NOW, last_activity="2026-11-26T17:23:07+00:00", tz=TZ)
        self.assertEqual(session["percent"], 0)
        self.assertEqual(session["state"], "current")
        self.assertEqual(session["source"], "history")
        self.assertIn("no use since", session["note"])
        self.assertEqual(week["percent"], 9)
        self.assertEqual(week["source"], "live")
        self.assertIn("no use since", week["note"])

    def test_stale_cached_session_after_reset_is_zero_not_the_old_percent(self):
        out = self.resolve([{
            "percent": 3, "resetsAt": "2026-11-25T14:00:00+00:00",
            "at": "2026-11-25T10:25:11+00:00", "source": "cached",
        }])
        self.assertEqual(out["percent"], 0)
        self.assertEqual(out["state"], "reset")
        self.assertEqual(out["note"], "reset Wed 14:00, no use since")
        self.assertFalse(out["live"])


class LiveAgeLabelTests(unittest.TestCase):
    def test_within_24_hours_shows_clock_time(self):
        self.assertEqual(
            au.live_age_label("2026-11-26T17:23:26+00:00", now=NOW, tz=TZ),
            "live 17:23",
        )

    def test_after_24_hours_shows_days_old(self):
        self.assertEqual(
            au.live_age_label("2026-11-25T09:36:21+00:00", now=NOW, tz=TZ),
            "live, 1 day old",
        )
        self.assertEqual(
            au.live_age_label("2026-11-24T09:36:21+00:00", now=NOW, tz=TZ),
            "live, 2 days old",
        )


class MergeLiveRecordTests(unittest.TestCase):
    def test_keeps_in_window_session_when_payload_omits_five_hour(self):
        previous = {"fiveHourPercent": 0, "fiveHourResetsAt": int(datetime.datetime(2026, 11, 26, 22, 10, tzinfo=UTC).timestamp()),
                    "sevenDayPercent": 9, "sevenDayResetsAt": int(datetime.datetime(2026, 11, 27, 15, 0, tzinfo=UTC).timestamp())}
        new = {"fiveHourPercent": None, "fiveHourResetsAt": None, "sevenDayPercent": 9, "sevenDayResetsAt": previous["sevenDayResetsAt"]}
        out = statusline.merge_live_record(previous, new, now=NOW)
        self.assertEqual(out["fiveHourPercent"], 0)
        self.assertEqual(out["fiveHourResetsAt"], previous["fiveHourResetsAt"])

    def test_does_not_keep_a_session_whose_window_has_already_reset(self):
        previous = {"fiveHourPercent": 33, "fiveHourResetsAt": int(datetime.datetime(2026, 11, 24, 23, 30, tzinfo=UTC).timestamp())}
        new = {"fiveHourPercent": None, "fiveHourResetsAt": None, "sevenDayPercent": 24}
        out = statusline.merge_live_record(previous, new, now=NOW)
        self.assertIsNone(out["fiveHourPercent"])


class SnapshotMergeTests(unittest.TestCase):
    def test_live_week_is_used_even_when_live_session_is_null(self):
        live = {"acct": {
            "accountUuid": "acct", "email": "a@b.c", "fetchedAt": "2026-11-26T17:23:26+00:00", "live": True,
            "session": {"percent": None, "resetsAt": None},
            "week": {"percent": 9, "resetsAt": "2026-11-27T15:00:00+00:00"},
        }}
        snap = {"acct": {
            "accountUuid": "acct", "email": "a@b.c", "fetchedAt": "2026-11-25T10:25:11+00:00",
            "session": {"percent": 3, "resetsAt": "2026-11-25T14:00:00+00:00"},
            "week": {"percent": 9, "resetsAt": "2026-11-27T15:00:00+00:00"},
            "usageCredits": {},
        }}
        history = [
            {"accountUuid": "acct", "time": "2026-11-26T17:23:07+00:00",
             "fiveHourPercent": 0, "fiveHourResetsAt": "2026-11-26T22:10:00+00:00",
             "sevenDayPercent": 9, "sevenDayResetsAt": "2026-11-27T15:00:00+00:00"},
        ]
        with mock.patch.object(au, "_capture_current_max_usage", return_value=None), \
             mock.patch.object(au, "_load_usage_snapshots", return_value=snap), \
             mock.patch.object(au, "_save_usage_snapshots"), \
             mock.patch.object(au, "_load_live_usage", return_value=live), \
             mock.patch.object(au, "load_labels", return_value={"acct": {"label": "a@b.c"}}), \
             mock.patch.object(au, "seed_labels_from_current_account", side_effect=lambda x: x):
            result = au.get_max_usage_snapshots(
                now=NOW, last_activity={"acct": NOW}, history=history,
            )
        mu = result[0]
        self.assertTrue(mu["live"])
        self.assertEqual(mu["week"]["percent"], 9)
        self.assertEqual(mu["week"]["source"], "live")
        self.assertEqual(mu["session"]["percent"], 0)
        self.assertEqual(mu["session"]["source"], "history")
        self.assertEqual(mu["session"]["state"], "current")
        self.assertEqual(mu["liveLabel"], "live 17:23")


class InsightsResetTests(unittest.TestCase):
    def test_reset_week_has_zero_percent_note_and_no_forecast(self):
        mu = [{
            "accountUuid": "acct", "label": "Claude", "fetchedAt": "2026-11-25T09:36:21+00:00",
            "week": {"percent": 0, "resetsAt": "2026-11-25T22:00:00+00:00", "state": "reset",
                     "note": "reset Wed 22:00, no use since", "asOf": "2026-11-25T09:36:21+00:00"},
            "session": {"percent": 0, "state": "reset", "note": "reset Mon 23:30, no use since"},
            "usageCredits": {},
        }]
        out = insights._claude_insights(mu, [], NOW)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["pct"], 0)
        self.assertIsNone(out[0]["forecast"])
        self.assertEqual(out[0]["note"], "reset Wed 22:00, no use since")
        self.assertEqual(out[0]["week"]["state"], "reset")


if __name__ == "__main__":
    unittest.main()
