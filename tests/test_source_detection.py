import os
import stat
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))
os.environ.setdefault("AGENTIC_SPEND_DATA_DIR", tempfile.mkdtemp(prefix="agentic-spend-test-"))

import source_detection as sd  # noqa: E402


def _write_exec(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write("#!/bin/sh\nexit 0\n")
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)


def _touch(path, content="x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(content)


def _by_id(rows):
    return {row["id"]: row for row in rows}


class IsolatedDetectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="agentic-detect-")
        self.home = self.tmp.name
        self.bindir = os.path.join(self.home, "bin")
        self.apps = os.path.join(self.home, "Apps")
        self.data = os.path.join(self.home, "data")
        os.makedirs(self.bindir)
        os.makedirs(self.apps)
        os.makedirs(self.data)
        # Ensure a leftover host env cannot leak into these cases.
        self._env_hold = {
            key: os.environ.get(key)
            for key in (
                "GROK_HOME",
                "GROK_BOT_PERSISTENCE_DIR",
                "XDG_DATA_HOME",
                "XDG_CONFIG_HOME",
                "AGENTIC_SPEND_APP_ROOTS",
            )
        }
        for key in self._env_hold:
            os.environ.pop(key, None)

    def tearDown(self):
        for key, value in self._env_hold.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    def detect(self, **kwargs):
        kwargs.setdefault("home", self.home)
        kwargs.setdefault("path", self.bindir)
        kwargs.setdefault("app_roots_list", [self.apps])
        kwargs.setdefault("data_dir_path", self.data)
        kwargs.setdefault("xdg_data_home", os.path.join(self.home, ".local", "share"))
        kwargs.setdefault("xdg_config_home", os.path.join(self.home, ".config"))
        return sd.detect(**kwargs)

    def test_empty_home_finds_nothing(self):
        rows = _by_id(self.detect())
        self.assertEqual(set(rows), {sd.CLAUDE, sd.CODEX, sd.OPENCODE, sd.GROK_CLI, sd.GROK_BOT})
        for row in rows.values():
            self.assertFalse(row["installed"], row)
            self.assertFalse(row["hasData"], row)
        self.assertFalse(sd.any_present(rows.values()))

    def test_claude_data_dir_without_sessions_is_installed_only(self):
        os.makedirs(os.path.join(self.home, ".claude"))
        row = _by_id(self.detect())[sd.CLAUDE]
        self.assertTrue(row["installed"])
        self.assertFalse(row["hasData"])

    def test_claude_session_jsonl_sets_has_data(self):
        _touch(os.path.join(self.home, ".claude", "projects", "demo", "sess.jsonl"), "{}\n")
        row = _by_id(self.detect())[sd.CLAUDE]
        self.assertTrue(row["installed"])
        self.assertTrue(row["hasData"])

    def test_claude_binary_on_path(self):
        _write_exec(os.path.join(self.bindir, "claude"))
        row = _by_id(self.detect())[sd.CLAUDE]
        self.assertTrue(row["installed"])
        self.assertFalse(row["hasData"])

    def test_claude_app_bundle(self):
        os.makedirs(os.path.join(self.apps, "Claude.app", "Contents"))
        row = _by_id(self.detect())[sd.CLAUDE]
        self.assertTrue(row["installed"])
        self.assertFalse(row["hasData"])

    def test_claude_json_alone_counts_as_installed(self):
        _touch(os.path.join(self.home, ".claude.json"), "{}")
        row = _by_id(self.detect())[sd.CLAUDE]
        self.assertTrue(row["installed"])
        self.assertFalse(row["hasData"])

    def test_codex_sessions(self):
        _touch(os.path.join(self.home, ".codex", "sessions", "2026", "rollout-1.jsonl"), "{}\n")
        row = _by_id(self.detect())[sd.CODEX]
        self.assertTrue(row["installed"])
        self.assertTrue(row["hasData"])
        # Other tools stay dark.
        others = _by_id(self.detect())
        for key in (sd.CLAUDE, sd.OPENCODE, sd.GROK_CLI, sd.GROK_BOT):
            self.assertFalse(others[key]["installed"])
            self.assertFalse(others[key]["hasData"])

    def test_codex_binary_without_data_dir(self):
        _write_exec(os.path.join(self.bindir, "codex"))
        row = _by_id(self.detect())[sd.CODEX]
        self.assertTrue(row["installed"])
        self.assertFalse(row["hasData"])

    def test_opencode_share_db_counts_as_data(self):
        _touch(os.path.join(self.home, ".local", "share", "opencode", "opencode.db"), "sql")
        row = _by_id(self.detect())[sd.OPENCODE]
        self.assertTrue(row["installed"])
        self.assertTrue(row["hasData"])

    def test_opencode_config_dir_is_installed_only(self):
        _touch(os.path.join(self.home, ".config", "opencode", "opencode.jsonc"), "{}")
        row = _by_id(self.detect())[sd.OPENCODE]
        self.assertTrue(row["installed"])
        self.assertFalse(row["hasData"])

    def test_opencode_binary(self):
        _write_exec(os.path.join(self.bindir, "opencode"))
        row = _by_id(self.detect())[sd.OPENCODE]
        self.assertTrue(row["installed"])
        self.assertFalse(row["hasData"])

    def test_grok_cli_home_and_usage_json(self):
        _touch(os.path.join(self.home, ".grok", "sessions", "proj", "s1", "usage.json"), "{}")
        row = _by_id(self.detect())[sd.GROK_CLI]
        self.assertTrue(row["installed"])
        self.assertTrue(row["hasData"])

    def test_grok_home_override(self):
        other = os.path.join(self.home, "alt-grok")
        _touch(os.path.join(other, "sessions", "p", "s", "usage.json"), "{}")
        # Default ~/.grok is absent, so only the override should count.
        row = _by_id(self.detect(grok_home=other))[sd.GROK_CLI]
        self.assertTrue(row["installed"])
        self.assertTrue(row["hasData"])
        empty = _by_id(self.detect())[sd.GROK_CLI]
        self.assertFalse(empty["installed"])
        self.assertFalse(empty["hasData"])

    def test_grok_binary(self):
        _write_exec(os.path.join(self.bindir, "grok"))
        row = _by_id(self.detect())[sd.GROK_CLI]
        self.assertTrue(row["installed"])
        self.assertFalse(row["hasData"])

    def test_grok_bot_app_bundle_is_installed_only(self):
        os.makedirs(os.path.join(self.apps, "Grok Bot.app", "Contents"))
        row = _by_id(self.detect())[sd.GROK_BOT]
        self.assertTrue(row["installed"])
        self.assertFalse(row["hasData"])

    def test_grok_bot_cache_files_set_has_data(self):
        support = os.path.join(self.home, "Library", "Application Support", "Grok Bot")
        _touch(os.path.join(support, "sand-client-persistence", "ABCDE"), "{}")
        row = _by_id(self.detect())[sd.GROK_BOT]
        self.assertTrue(row["installed"])
        self.assertTrue(row["hasData"])

    def test_grok_bot_history_in_data_dir_counts_after_app_is_gone(self):
        _touch(os.path.join(self.data, "grokbot-usage.json"), '{"accounts":[]}')
        row = _by_id(self.detect())[sd.GROK_BOT]
        self.assertFalse(row["installed"])
        self.assertTrue(row["hasData"])
        self.assertTrue(sd.is_present([row], sd.GROK_BOT))

    def test_hints_can_mark_has_data_without_files(self):
        row = _by_id(self.detect(hints={sd.OPENCODE: True}))[sd.OPENCODE]
        self.assertFalse(row["installed"])
        self.assertTrue(row["hasData"])

    def test_empty_app_roots_env_ignores_applications(self):
        # Even if a bundle exists under the default apps folder we pass in,
        # an explicit empty list must win.
        os.makedirs(os.path.join(self.apps, "Claude.app"))
        row = _by_id(self.detect(app_roots_list=[]))[sd.CLAUDE]
        self.assertFalse(row["installed"])

    def test_does_not_scan_host_home_when_home_is_overridden(self):
        # A leftover host PATH binary must not leak in because we pass path=.
        rows = _by_id(self.detect(path="/__no_such_path__"))
        for row in rows.values():
            self.assertFalse(row["installed"])
            self.assertFalse(row["hasData"])


class VisibilityTests(unittest.TestCase):
    def test_filter_drops_tools_that_are_absent(self):
        detection = [
            {"id": "claude", "label": "Claude Code", "installed": True, "hasData": True},
            {"id": "codex", "label": "Codex", "installed": False, "hasData": False},
            {"id": "opencode", "label": "OpenCode", "installed": False, "hasData": False},
            {"id": "grok-cli", "label": "Grok CLI", "installed": False, "hasData": False},
            {"id": "grok-bot", "label": "Grok Bot", "installed": False, "hasData": False},
        ]
        sources = [
            {"id": "acct-1", "tool": "claude", "kind": "official-cached"},
            {"id": "codex", "tool": "codex", "kind": "none"},
            {"id": "opencode", "tool": "opencode", "kind": "none"},
            {"id": "grok-cli", "tool": "grok-cli", "kind": "local-live"},
            {"id": "grok-bot", "tool": "grok-bot", "kind": "none", "grokBot": True},
        ]
        kept = sd.filter_absent_sources(sources, detection)
        self.assertEqual([s["tool"] for s in kept], ["claude"])

    def test_filter_keeps_has_data_even_if_uninstalled(self):
        detection = [
            {"id": "grok-bot", "label": "Grok Bot", "installed": False, "hasData": True},
        ]
        sources = [{"id": "grok-bot", "tool": "grok-bot", "kind": "official-cached", "grokBot": True}]
        self.assertEqual(len(sd.filter_absent_sources(sources, detection)), 1)

    def test_unknown_catalog_tool_is_kept(self):
        detection = [{"id": "claude", "label": "Claude Code", "installed": True, "hasData": True}]
        sources = [{"id": "other", "tool": "other", "kind": "none"}]
        self.assertEqual(sd.filter_absent_sources(sources, detection), sources)

    def test_zero_spend_hidden_unless_plan_limit(self):
        detection = [
            {"id": "opencode", "label": "OpenCode", "installed": True, "hasData": True},
            {"id": "grok-cli", "label": "Grok CLI", "installed": True, "hasData": True},
            {"id": "grok-bot", "label": "Grok Bot", "installed": True, "hasData": True},
            {"id": "claude", "label": "Claude Code", "installed": True, "hasData": True},
            {"id": "codex", "label": "Codex", "installed": True, "hasData": True},
        ]
        opencode = {"tool": "opencode", "kind": "none"}
        grok_cli = {"tool": "grok-cli", "kind": "local-live"}
        grok_bot = {"tool": "grok-bot", "kind": "official-cached", "grokBot": True, "weeklyLimitPercent": 17.25}
        claude = {"tool": "claude", "kind": "official-live", "maxUsage": {"live": True}}
        codex = {"tool": "codex", "kind": "official-live", "codexLive": {"planType": "plus"}}
        self.assertFalse(sd.visible_in_range(opencode, 0, detection))
        self.assertFalse(sd.visible_in_range(grok_cli, 0, detection))
        self.assertTrue(sd.visible_in_range(grok_cli, 4.04, detection))
        self.assertTrue(sd.visible_in_range(grok_bot, 0, detection))
        self.assertTrue(sd.visible_in_range(claude, 0, detection))
        self.assertTrue(sd.visible_in_range(codex, 0, detection))

    def test_absent_tool_hidden_even_with_spend(self):
        detection = [{"id": "opencode", "label": "OpenCode", "installed": False, "hasData": False}]
        self.assertFalse(sd.visible_in_range({"tool": "opencode", "kind": "none"}, 3.0, detection))

    def test_attach_adds_detection_and_drops_absent_catalog_rows(self):
        payload = {
            "accounts": [{"accountUuid": "acct-1"}],
            "sources": [
                {"id": "acct-1", "tool": "claude", "kind": "none"},
                {"id": "opencode", "tool": "opencode", "kind": "none"},
            ],
            "noAccountData": {"agents": {"opencode": 1}},
            "grokBot": {"accounts": []},
        }
        detection = [
            {"id": "claude", "label": "Claude Code", "installed": True, "hasData": True},
            {"id": "codex", "label": "Codex", "installed": False, "hasData": False},
            {"id": "opencode", "label": "OpenCode", "installed": False, "hasData": False},
            {"id": "grok-cli", "label": "Grok CLI", "installed": False, "hasData": False},
            {"id": "grok-bot", "label": "Grok Bot", "installed": False, "hasData": False},
        ]
        out = sd.attach(payload, detect_fn=lambda **kw: detection)
        self.assertEqual(out["detection"], detection)
        self.assertEqual([s["tool"] for s in out["sources"]], ["claude"])
        self.assertTrue(out["sources"][0]["installed"])
        self.assertTrue(out["sources"][0]["hasData"])

    def test_hints_from_usage(self):
        hints = sd.hints_from_usage(
            account_list=[{"accountUuid": "a"}],
            no_account_data={"agents": {"codex": 1, "opencode": 2}},
            grok_report={"found": True},
            grokbot={"accounts": [{"accountId": "x"}]},
            codex_live=None,
        )
        self.assertTrue(hints[sd.CLAUDE])
        self.assertTrue(hints[sd.CODEX])
        self.assertTrue(hints[sd.OPENCODE])
        self.assertTrue(hints[sd.GROK_CLI])
        self.assertTrue(hints[sd.GROK_BOT])


if __name__ == "__main__":
    unittest.main()
