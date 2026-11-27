"""Several Claude config directories (CLAUDE_CONFIG_DIR): transcripts, accounts
and the status line hook in each of them. Everything runs against temp
directories, so no real settings.json or .claude.json is read or written."""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.join(os.path.dirname(__file__), os.pardir)
sys.path.insert(0, ROOT)

import account_usage  # noqa: E402
import claude_dirs  # noqa: E402
import statusline  # noqa: E402
import statusline_multi  # noqa: E402
import statusline_setup as ss  # noqa: E402
import token_sources  # noqa: E402


class ClaudeDirsTests(unittest.TestCase):
    def test_unset_is_the_single_default(self):
        self.assertEqual(claude_dirs.config_dirs({}), [os.path.expanduser("~/.claude")])
        self.assertEqual(claude_dirs.config_dirs({"CLAUDE_CONFIG_DIR": " , "}), [os.path.expanduser("~/.claude")])

    def test_comma_list_keeps_order_and_drops_duplicates(self):
        got = claude_dirs.config_dirs({"CLAUDE_CONFIG_DIR": "/a/.claude, /b/work,/a/.claude"})
        self.assertEqual(got, ["/a/.claude", "/b/work"])

    def test_config_json_lives_inside_a_custom_dir(self):
        self.assertEqual(claude_dirs.config_json("/b/work"), "/b/work/.claude.json")

    def test_default_dir_uses_the_home_file(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {"HOME": home}):
            default = os.path.join(home, ".claude")
            os.makedirs(default)
            home_file = os.path.join(home, ".claude.json")
            self.assertEqual(claude_dirs.config_json(default), home_file)
            # A leftover file inside ~/.claude never wins over ~/.claude.json,
            # in the server or in the hook.
            for p in (home_file, os.path.join(default, ".claude.json")):
                with open(p, "w") as f:
                    f.write("{}")
            self.assertEqual(claude_dirs.config_json(default), home_file)
            self.assertEqual(statusline._claude_config_path({"CLAUDE_CONFIG_DIR": default}), home_file)
            self.assertEqual(statusline._claude_config_path({}), home_file)


class TwoDirs(unittest.TestCase):
    """Two Claude config directories under a temp home."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = self.tmp.name
        self.a, self.b = os.path.join(d, ".claude"), os.path.join(d, ".claude-work")
        for p in (self.a, self.b):
            os.makedirs(os.path.join(p, "projects", "proj"))
        self.env = {"CLAUDE_CONFIG_DIR": "%s,%s" % (self.a, self.b)}
        self.data = os.path.join(d, "data")
        os.makedirs(self.data)
        for p in [
            mock.patch.dict(os.environ, self.env),
            mock.patch.object(statusline_multi.data_dir, "DATA_DIR", self.data),
            mock.patch.object(ss, "SETUP_STATE_PATH", os.path.join(self.data, "setup-state.json")),
            mock.patch.object(ss, "CLAUDE_SETTINGS_PATH", os.path.join(d, "unused", "settings.json")),
            mock.patch.object(ss, "BACKUP_PATH", os.path.join(d, "unused-backup.json")),
        ]:
            p.start()
            self.addCleanup(p.stop)

    def settings(self, config_dir):
        try:
            with open(os.path.join(config_dir, "settings.json")) as f:
                return json.load(f)
        except FileNotFoundError:
            return None

    def write_settings(self, config_dir, obj):
        with open(os.path.join(config_dir, "settings.json"), "w") as f:
            json.dump(obj, f)


class StatuslineMultiTests(TwoDirs):
    def test_enable_wires_both_and_keeps_other_keys(self):
        self.write_settings(self.a, {"hooks": {"Stop": []}, "permissions": {"allow": ["x"]}})
        r = statusline_multi.enable()
        self.assertTrue(r["ok"])
        self.assertEqual(len(r["perConfigDir"]), 2)
        for d in (self.a, self.b):
            self.assertIn("statusline.py", self.settings(d)["statusLine"]["command"])
        self.assertEqual(self.settings(self.a)["permissions"], {"allow": ["x"]})
        self.assertEqual(self.settings(self.a)["hooks"], {"Stop": []})
        self.assertEqual(statusline_multi.get_status()["state"], "ours")
        self.assertEqual(statusline_multi.enable()["status"], "already_enabled")

    def test_existing_status_line_is_wrapped_then_restored_exactly(self):
        mine = {"type": "command", "command": "my-line --fancy", "padding": 1}
        self.write_settings(self.b, {"statusLine": mine})
        statusline_multi.enable()
        self.assertIn("my-line --fancy", self.settings(self.b)["statusLine"]["command"])
        self.assertEqual(statusline_multi.get_status()["state"], "ours")  # first dir: plain ours
        statusline_multi.disable()
        self.assertEqual(self.settings(self.b)["statusLine"], mine)
        self.assertNotIn("statusLine", self.settings(self.a))

    def test_half_on_reads_as_not_on(self):
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": self.a}):
            statusline_multi.enable()
        self.assertEqual(statusline_multi.get_status()["state"], "none")
        self.assertEqual(statusline_multi.enable()["status"], "enabled")
        self.assertEqual(statusline_multi.get_status()["state"], "ours")

    def test_backup_follows_the_directory_not_its_position(self):
        mine = {"type": "command", "command": "my-line --fancy", "padding": 2}
        self.write_settings(self.a, {"statusLine": mine})
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": self.a}):
            statusline_multi.enable()
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": "%s,%s" % (self.b, self.a)}):
            statusline_multi.enable()
            statusline_multi.disable()
        self.assertEqual(self.settings(self.a)["statusLine"], mine)
        self.assertNotIn("statusLine", self.settings(self.b))

    def test_one_broken_settings_file_does_not_stop_the_other(self):
        with open(os.path.join(self.b, "settings.json"), "w") as f:
            f.write("{not json")
        r = statusline_multi.enable()
        self.assertFalse(r["ok"])
        self.assertIn(self.b, r["error"])
        self.assertIn("statusLine", self.settings(self.a))
        st = statusline_multi.get_status()
        self.assertEqual(st["state"], "ours")
        self.assertIn(self.b, st["error"])
        self.assertFalse(statusline_multi.auto_enable_once()["ok"])

    def test_enable_with_no_claude_directory_creates_the_first(self):
        fresh = os.path.join(self.tmp.name, "fresh", ".claude")
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": fresh}):
            self.assertEqual(statusline_multi.get_status()["state"], "none")
            self.assertEqual(statusline_multi.enable()["status"], "enabled")
        self.assertIn("statusLine", self.settings(fresh))

    def test_module_paths_are_put_back(self):
        before = (ss.CLAUDE_SETTINGS_PATH, ss.BACKUP_PATH)
        statusline_multi.enable()
        statusline_multi.disable()
        self.assertEqual((ss.CLAUDE_SETTINGS_PATH, ss.BACKUP_PATH), before)

    def test_auto_enable_does_both_once_and_stays_off_when_turned_off(self):
        self.assertEqual(statusline_multi.auto_enable_once()["status"], "enabled")
        for d in (self.a, self.b):
            self.assertIn("statusLine", self.settings(d))
        self.assertEqual(statusline_multi.auto_enable_once()["status"], "already_enabled")
        statusline_multi.disable()
        self.assertEqual(statusline_multi.auto_enable_once()["status"], "skipped_already_decided")
        for d in (self.a, self.b):
            self.assertNotIn("statusLine", self.settings(d))

    def test_auto_enable_opt_out(self):
        with mock.patch.dict(os.environ, {"AGENTIC_SPEND_NO_AUTO_ENABLE": "1"}):
            self.assertEqual(statusline_multi.auto_enable_once()["status"], "skipped_env")
        self.assertIsNone(self.settings(self.a))

    def test_missing_directory_is_skipped(self):
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": self.a + ",/nonexistent/claude"}):
            self.assertEqual(statusline_multi.enable()["status"], "enabled")
            self.assertEqual(statusline_multi.get_status()["state"], "ours")


class TranscriptsAcrossDirsTests(TwoDirs):
    def test_sessions_from_both_directories_are_mapped(self):
        for d, sid, acct in ((self.a, "s1", "acct-1"), (self.b, "s2", "acct-2")):
            with open(os.path.join(d, "projects", "proj", sid + ".jsonl"), "w") as f:
                f.write(json.dumps({"type": "session-owner", "accountUuid": acct}) + "\n")
                f.write(json.dumps({"type": "user", "cwd": "/work/" + sid}) + "\n")
        accounts, projects = account_usage.build_session_account_map()
        self.assertEqual(accounts, {"s1": "acct-1", "s2": "acct-2"})
        self.assertEqual(projects, {"s1": "/work/s1", "s2": "/work/s2"})
        found = sorted(os.path.basename(p) for p, _, _ in token_sources.iter_claude_session_files())
        self.assertEqual(found, ["s1.jsonl", "s2.jsonl"])

    def test_labels_come_from_each_config(self):
        with open(os.path.join(self.b, ".claude.json"), "w") as f:
            json.dump({"oauthAccount": {"accountUuid": "acct-2", "emailAddress": "work@example.com"}}, f)
        labels = account_usage.seed_labels_from_current_account({})
        self.assertEqual(labels["acct-2"]["label"], "work@example.com")

    def test_hook_reads_the_config_of_its_own_session(self):
        inside = os.path.join(self.b, ".claude.json")
        with open(inside, "w") as f:
            json.dump({}, f)
        self.assertEqual(statusline._claude_config_path({"CLAUDE_CONFIG_DIR": self.b}), inside)
        self.assertEqual(statusline._claude_config_path({}), os.path.expanduser("~/.claude.json"))


if __name__ == "__main__":
    unittest.main()
