import datetime
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))
os.environ.setdefault("AGENTIC_SPEND_DATA_DIR", tempfile.mkdtemp(prefix="agentic-spend-test-"))

import notify  # noqa: E402

UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 11, 26, 12, 0, tzinfo=UTC)


def plan(pct, key="grok-bot:a", reset="2026-11-29T09:06:55+00:00", credits=None):
    return {"key": key, "label": "Grok Bot", "pct": pct, "resetAt": reset, "credits": credits}


class FakeRun:
    def __init__(self, code=0):
        self.calls = []
        self.code = code

    def __call__(self, args, **kw):
        self.calls.append(args)

        class R:
            returncode = self.code
        return R()


class ThresholdTests(unittest.TestCase):
    def test_fires_once_per_window(self):
        settings = notify.validate_settings({})
        events, state = notify.evaluate([plan(81)], settings, {}, now=NOW)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["threshold"], 80)
        events, state = notify.evaluate([plan(85)], settings, state, now=NOW)
        self.assertEqual(events, [])
        events, state = notify.evaluate([plan(96)], settings, state, now=NOW)
        self.assertEqual([e["threshold"] for e in events], [95])

    def test_jumping_past_both_announces_highest_once(self):
        events, state = notify.evaluate([plan(97)], notify.validate_settings({}), {}, now=NOW)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["threshold"], 95)
        self.assertEqual(len(state["sent"]), 2)

    def test_new_window_fires_again(self):
        settings = notify.validate_settings({})
        _, state = notify.evaluate([plan(81)], settings, {}, now=NOW)
        events, _ = notify.evaluate([plan(81, reset="2026-12-06T09:06:50+00:00")], settings, state, now=NOW)
        self.assertEqual(len(events), 1)

    def test_reset_jitter_is_same_window(self):
        settings = notify.validate_settings({})
        _, state = notify.evaluate([plan(81, reset="2026-12-10T15:40:46+00:00")], settings, {}, now=NOW)
        events, _ = notify.evaluate([plan(82, reset="2026-12-10T15:40:47+00:00")], settings, state, now=NOW)
        self.assertEqual(events, [])

    def test_per_plan_thresholds_and_disabled(self):
        settings = notify.validate_settings({"perPlan": {"grok-bot:a": [50]}})
        events, _ = notify.evaluate([plan(55), plan(55, key="claude:u")], settings, {}, now=NOW)
        self.assertEqual([e["key"] for e in events], ["grok-bot:a"])
        off = notify.validate_settings({"enabled": False})
        events, _ = notify.evaluate([plan(99)], off, {}, now=NOW)
        self.assertEqual(events, [])

    def test_input_state_not_mutated(self):
        state = {}
        notify.evaluate([plan(99)], notify.validate_settings({}), state, now=NOW)
        self.assertEqual(state, {})

    def test_old_entries_pruned(self):
        old = {"sent": {"x|2026-01-01T00|80": "2026-01-01T00:00:00+00:00"}}
        _, state = notify.evaluate([], notify.validate_settings({}), old, now=NOW)
        self.assertEqual(state["sent"], {})


class CreditTests(unittest.TestCase):
    def test_remaining_drop_notifies_once_a_day(self):
        settings = notify.validate_settings({})
        c = lambda v: {"kind": "remaining", "value": v, "currency": "USD"}
        events, state = notify.evaluate([plan(10, credits=c(10.6))], settings, {}, now=NOW)
        self.assertEqual(events, [])  # first sighting only records
        events, state = notify.evaluate([plan(10, credits=c(9.0))], settings, state, now=NOW)
        self.assertEqual(len(events), 1)
        self.assertIn("$9.00 left", events[0]["message"])
        events, state = notify.evaluate([plan(10, credits=c(8.0))], settings, state, now=NOW)
        self.assertEqual(events, [])

    def test_used_increase(self):
        settings = notify.validate_settings({})
        c = lambda v: {"kind": "used", "value": v, "currency": "GBP", "exponent": 2}
        _, state = notify.evaluate([plan(10, key="claude:u", credits=c(0))], settings, {}, now=NOW)
        events, _ = notify.evaluate([plan(10, key="claude:u", credits=c(250))], settings, state, now=NOW)
        self.assertEqual(len(events), 1)
        self.assertIn("\u00a32.50", events[0]["message"])


class SettingsTests(unittest.TestCase):
    def test_validation(self):
        s = notify.validate_settings({"thresholds": [95, "80", 80]})
        self.assertEqual(s["thresholds"], [80, 95])
        for bad in ({"thresholds": [0]}, {"thresholds": [101]}, {"thresholds": "80"},
                    {"enabled": "yes"}, {"perPlan": {"../x": [80]}}, {"thresholds": [True]}):
            with self.assertRaises(ValueError):
                notify.validate_settings(bad)

    def test_per_plan_remove(self):
        s = notify.validate_settings({"perPlan": {"codex:primary": [50]}})
        s = notify.validate_settings({"perPlan": {"codex:primary": None}}, s)
        self.assertEqual(s["perPlan"], {})


class DigestDueTests(unittest.TestCase):
    def test_monday_after_nine_once(self):
        settings = notify.validate_settings({})
        mon_8 = datetime.datetime(2026, 11, 30, 8, 59)
        mon_9 = datetime.datetime(2026, 11, 30, 9, 0)
        tue = datetime.datetime(2026, 12, 1, 10, 0)
        self.assertFalse(notify.digest_due(settings, {}, mon_8))
        self.assertTrue(notify.digest_due(settings, {}, mon_9))
        self.assertFalse(notify.digest_due(settings, {"digestWeek": notify.iso_week_id(mon_9)}, mon_9))
        self.assertFalse(notify.digest_due(settings, {}, tue))
        self.assertFalse(notify.digest_due(notify.validate_settings({"digest": False}), {}, mon_9))


class DeliveryTests(unittest.TestCase):
    def test_osascript_args_not_spliced(self):
        run = FakeRun()
        run_ok = notify.deliver_osascript('Plan "x"', "msg", runner=run)
        self.assertTrue(run_ok)
        args = run.calls[0]
        self.assertEqual(args[0], "osascript")
        self.assertNotIn('Plan "x"', args[2])
        self.assertEqual(args[3], 'Plan "x"')

    def test_app_heartbeat_routes_to_queue(self):
        with tempfile.TemporaryDirectory() as d:
            state_path = os.path.join(d, "state.json")
            settings_path = os.path.join(d, "settings.json")
            notify.record_heartbeat(now=NOW, path=state_path)
            run = FakeRun()
            delivered = notify.run_checks([plan(90)], now=NOW, settings_path=settings_path,
                                          state_path=state_path, runner=run)
            self.assertEqual(delivered[0]["via"], "app")
            self.assertEqual(run.calls, [])
            pending = notify.record_heartbeat(now=NOW, path=state_path)
            self.assertEqual(len(pending), 1)
            self.assertEqual(notify.record_heartbeat(now=NOW, path=state_path), [])

    def test_stale_heartbeat_uses_osascript(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(notify, "IS_LINUX", False):
            state_path = os.path.join(d, "state.json")
            notify.record_heartbeat(now=NOW - datetime.timedelta(minutes=10), path=state_path)
            run = FakeRun()
            delivered = notify.run_checks([plan(90)], now=NOW, settings_path=os.path.join(d, "s.json"),
                                          state_path=state_path, runner=run)
            self.assertEqual(delivered[0]["via"], "osascript")
            self.assertEqual(len(run.calls), 1)
            # Same reading again: de-duplicated through the saved state.
            delivered = notify.run_checks([plan(91)], now=NOW, settings_path=os.path.join(d, "s.json"),
                                          state_path=state_path, runner=run)
            self.assertEqual(delivered, [])


if __name__ == "__main__":
    unittest.main()
