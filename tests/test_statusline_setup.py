"""The live-usage hook: unwrapping our own stacked entries, a stable path that
survives brew upgrades, atomic live-file writes, and on-by-default setup.

Every test points the module at a temp directory, so the real
~/.claude/settings.json is never read or written."""
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.join(os.path.dirname(__file__), os.pardir)
sys.path.insert(0, ROOT)

import statusline_setup as ss  # noqa: E402

CELLAR = "/opt/homebrew/Cellar/agentic-spend/{v}/libexec/statusline.py"
OPT = "/opt/homebrew/opt/agentic-spend/libexec/statusline.py"
REAL_STABLE = ss.stable_script_path


def hook(script, wrapped=None):
    return ss._hook_command(script, wrapped)


class SetupCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = self.tmp.name
        self.settings_path = os.path.join(d, "claude", "settings.json")
        os.makedirs(os.path.dirname(self.settings_path))
        patches = [
            mock.patch.object(ss, "CLAUDE_SETTINGS_PATH", self.settings_path),
            mock.patch.object(ss, "BACKUP_PATH", os.path.join(d, "backup.json")),
            mock.patch.object(ss, "SETUP_STATE_PATH", os.path.join(d, "setup-state.json")),
            mock.patch.object(ss, "SCRIPT_PATH", CELLAR.format(v="1.5.1")),
            mock.patch.object(ss, "stable_script_path", lambda: OPT),
            mock.patch.dict(os.environ, {"AGENTIC_SPEND_NO_AUTO_ENABLE": ""}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)

    def write_settings(self, obj):
        with open(self.settings_path, "w") as f:
            json.dump(obj, f)

    def read_settings(self):
        with open(self.settings_path) as f:
            return json.load(f)


class UnwrapTests(SetupCase):
    def nested_chain(self):
        cmd = "python3 -I /Users/x/code/agentic-spend/statusline.py"
        for v in ("1.3.0", "1.3.4", "1.5.0"):
            cmd = hook(CELLAR.format(v=v), cmd)
        return cmd

    def test_unwraps_toms_four_deep_chain_to_nothing(self):
        with mock.patch.object(ss, "_is_our_script", lambda p: "agentic-spend" in p and p.endswith("statusline.py")):
            user, layers = ss.unwrap(self.nested_chain())
        self.assertIsNone(user)
        self.assertEqual(layers, 4)

    def test_keeps_a_genuine_user_line_under_our_layers(self):
        cmd = hook(CELLAR.format(v="1.5.0"), hook(CELLAR.format(v="1.3.4"), "~/bin/my-status --fancy 'a b'"))
        user, layers = ss.unwrap(cmd)
        self.assertEqual(user, "~/bin/my-status --fancy 'a b'")
        self.assertEqual(layers, 2)

    def test_foreign_command_is_not_ours(self):
        self.assertEqual(ss.unwrap("~/bin/my-status"), ("~/bin/my-status", 0))
        self.assertEqual(ss.unwrap("python3 -I /some/other/statusline.py"), ("python3 -I /some/other/statusline.py", 0))

    def test_enable_collapses_nested_entries_to_one_hook_at_the_stable_path(self):
        cmd = hook(CELLAR.format(v="1.5.0"), hook(CELLAR.format(v="1.3.4"), hook(OPT, "~/bin/my-status")))
        self.write_settings({"model": "x", "statusLine": {"type": "command", "command": cmd, "padding": 1}})
        result = ss.enable()
        self.assertEqual(result["status"], "repaired")
        entry = self.read_settings()["statusLine"]
        self.assertEqual(entry["command"], hook(OPT, "~/bin/my-status"))
        self.assertEqual(entry["padding"], 1)
        self.assertEqual(self.read_settings()["model"], "x")
        with open(ss.BACKUP_PATH) as f:
            self.assertEqual(json.load(f)["command"], "~/bin/my-status")
        # running it again changes nothing
        self.assertEqual(ss.enable()["status"], "already_enabled")

    def test_enable_with_only_our_old_entries_leaves_a_plain_single_hook(self):
        self.write_settings({"statusLine": {"type": "command", "command": hook(CELLAR.format(v="1.3.0"), hook(CELLAR.format(v="1.3.4")))}})
        ss.enable()
        self.assertEqual(self.read_settings()["statusLine"]["command"], hook(OPT))
        self.assertFalse(os.path.exists(ss.BACKUP_PATH))

    def test_enable_wraps_a_foreign_status_line_once_and_disable_restores_it(self):
        self.write_settings({"statusLine": {"type": "command", "command": "~/bin/my-status"}})
        self.assertEqual(ss.enable()["status"], "wrapped_existing")
        self.assertEqual(self.read_settings()["statusLine"]["command"], hook(OPT, "~/bin/my-status"))
        self.assertEqual(ss.get_status()["state"], "wraps_other")
        self.assertFalse(ss.get_status()["needsRepair"])
        self.assertEqual(ss.disable()["status"], "restored_previous")
        self.assertEqual(self.read_settings()["statusLine"]["command"], "~/bin/my-status")

    def test_disable_never_touches_a_foreign_line_and_removes_a_plain_hook(self):
        self.write_settings({"statusLine": {"type": "command", "command": "~/bin/my-status"}})
        self.assertEqual(ss.disable()["status"], "not_ours")
        self.write_settings({"statusLine": {"type": "command", "command": hook(CELLAR.format(v="1.5.0"))}})
        self.assertEqual(ss.disable()["status"], "removed")
        self.assertNotIn("statusLine", self.read_settings())

    def test_own_script_detection_covers_cellar_opt_and_checkouts(self):
        self.assertTrue(ss._is_our_script("/usr/local/Cellar/agentic-spend/9.9.9/libexec/statusline.py"))
        self.assertTrue(ss._is_our_script(OPT))
        self.assertFalse(ss._is_our_script("/some/other/statusline.py"))
        self.assertFalse(ss._is_our_script("/opt/homebrew/Cellar/other-tool/1.0/libexec/statusline.py"))
        with tempfile.TemporaryDirectory() as d:
            for name in ("server.py", "statusline_setup.py", "statusline.py"):
                open(os.path.join(d, name), "w").close()
            self.assertTrue(ss._is_our_script(os.path.join(d, "statusline.py")))

    def test_stable_path_uses_opt_for_a_cellar_install(self):
        with mock.patch.object(ss, "SCRIPT_PATH", "/opt/homebrew/Cellar/agentic-spend/1.5.1/libexec/statusline.py"):
            self.assertEqual(REAL_STABLE(), OPT)
        with mock.patch.object(ss, "SCRIPT_PATH", "/usr/local/Cellar/agentic-spend/1.2.3/libexec/statusline.py"):
            self.assertEqual(REAL_STABLE(), "/usr/local/opt/agentic-spend/libexec/statusline.py")

    def test_settings_write_is_atomic_and_keeps_mode(self):
        self.write_settings({"statusLine": {"type": "command", "command": "x"}})
        os.chmod(self.settings_path, 0o640)
        ss.enable()
        self.assertEqual(stat.S_IMODE(os.stat(self.settings_path).st_mode), 0o640)
        self.assertEqual([n for n in os.listdir(os.path.dirname(self.settings_path)) if n.startswith(".tmp-")], [])


class AutoEnableTests(SetupCase):
    def test_first_start_turns_it_on_and_records_it(self):
        self.write_settings({})
        self.assertEqual(ss.auto_enable_once()["status"], "enabled")
        self.assertEqual(self.read_settings()["statusLine"]["command"], hook(OPT))
        with open(ss.SETUP_STATE_PATH) as f:
            self.assertIn("liveUsageAutoEnabledAt", json.load(f))

    def test_user_who_turned_it_off_stays_off(self):
        self.write_settings({})
        ss.auto_enable_once()
        ss.disable()
        self.assertEqual(ss.auto_enable_once()["status"], "skipped_already_decided")
        self.assertNotIn("statusLine", self.read_settings())

    def test_disabled_before_first_auto_enable_also_stays_off(self):
        self.write_settings({"statusLine": {"type": "command", "command": hook(OPT)}})
        ss.disable()
        os.remove(ss.SETUP_STATE_PATH) if os.path.exists(ss.SETUP_STATE_PATH) else None
        ss._record_setup({"liveUsageDisabledAt": "2026-11-26T00:00:00+00:00"})
        self.assertEqual(ss.auto_enable_once()["status"], "skipped_already_decided")

    def test_second_start_does_not_rewrap(self):
        self.write_settings({"statusLine": {"type": "command", "command": "~/bin/my-status"}})
        self.assertEqual(ss.auto_enable_once()["status"], "wrapped_existing")
        before = self.read_settings()
        ss.auto_enable_once()
        self.assertEqual(self.read_settings(), before)

    def test_tidies_old_nested_entries_on_start(self):
        self.write_settings({"statusLine": {"type": "command", "command": hook(CELLAR.format(v="1.5.0"), hook(CELLAR.format(v="1.3.4")))}})
        self.assertEqual(ss.auto_enable_once()["status"], "repaired")
        self.assertEqual(self.read_settings()["statusLine"]["command"], hook(OPT))

    def test_env_opt_out_and_no_claude(self):
        with mock.patch.dict(os.environ, {"AGENTIC_SPEND_NO_AUTO_ENABLE": "1"}):
            self.assertEqual(ss.auto_enable_once()["status"], "skipped_env")
        os.rmdir(os.path.dirname(self.settings_path)) if not os.listdir(os.path.dirname(self.settings_path)) else None
        self.assertEqual(ss.auto_enable_once()["status"], "skipped_no_claude")


class LiveFileWriteTests(unittest.TestCase):
    def test_write_is_atomic_0600_and_valid_json(self):
        import statusline
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "abc.json")
            statusline.write_live_file(out, {"a": 1})
            statusline.write_live_file(out, {"a": 2, "pad": "x" * 5000})
            with open(out) as f:
                self.assertEqual(json.load(f)["a"], 2)
            self.assertEqual(stat.S_IMODE(os.stat(out).st_mode), 0o600)
            self.assertEqual(os.listdir(d), ["abc.json"])

    def test_failed_write_leaves_the_old_file_and_no_temp(self):
        import statusline
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "abc.json")
            statusline.write_live_file(out, {"a": 1})

            class Boom:
                pass
            with self.assertRaises(TypeError):
                statusline.write_live_file(out, {"a": Boom()})
            with open(out) as f:
                self.assertEqual(json.load(f), {"a": 1})
            self.assertEqual(os.listdir(d), ["abc.json"])

    def test_hook_end_to_end_fills_a_valid_file_and_nested_copy_skips_write(self):
        with tempfile.TemporaryDirectory() as home:
            os.makedirs(os.path.join(home, ".claude"))
            with open(os.path.join(home, ".claude.json"), "w") as f:
                json.dump({"oauthAccount": {"accountUuid": "uuid-1", "emailAddress": "a@example.com"}}, f)
            payload = json.dumps({"rate_limits": {"five_hour": {"used_percentage": 12, "resets_at": 1}, "seven_day": {"used_percentage": 9, "resets_at": 2}}})
            env = {**os.environ, "HOME": home, "AGENTIC_SPEND_DATA_DIR": os.path.join(home, "data")}
            env.pop("CLAUDE_CONFIG_DIR", None)
            script = os.path.abspath(os.path.join(ROOT, "statusline.py"))
            r = subprocess.run([sys.executable, "-I", script], input=payload, text=True, capture_output=True, env=env, timeout=20)
            self.assertEqual(r.returncode, 0, r.stderr)
            live = os.path.join(home, ".claude", "cc-usage-live", "uuid-1.json")
            with open(live) as f:
                data = json.load(f)
            self.assertEqual(data["sevenDayPercent"], 9)
            os.remove(live)
            env["AGENTIC_SPEND_STATUSLINE_ACTIVE"] = "1"
            subprocess.run([sys.executable, "-I", script], input=payload, text=True, capture_output=True, env=env, timeout=20)
            self.assertFalse(os.path.exists(live))


if __name__ == "__main__":
    unittest.main()
