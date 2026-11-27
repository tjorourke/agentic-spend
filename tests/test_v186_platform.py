"""1.8.6: Linux support next to macOS. Each test names the OS it is about,
so the suite gives the same answers on a Mac and on Linux."""
import datetime
import os
import re
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
os.environ.setdefault("AGENTIC_SPEND_DATA_DIR", tempfile.mkdtemp(prefix="agentic-spend-test-"))

import desktop_sources  # noqa: E402
import grokbot_usage  # noqa: E402
import login_item  # noqa: E402
import notify  # noqa: E402
import platform_info as pi  # noqa: E402
import pricing_region  # noqa: E402
import source_detection as sd  # noqa: E402
from test_v180_features import html, run_js  # noqa: E402


class CleanEnv(unittest.TestCase):
    """Other test modules point GROK_BOT_PERSISTENCE_DIR and the app roots
    at temp folders; these tests need the defaults."""
    def setUp(self):
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        for key in ("GROK_BOT_PERSISTENCE_DIR", "AGENTIC_SPEND_APP_ROOTS", "XDG_CONFIG_HOME"):
            os.environ.pop(key, None)


def _touch(path, text=""):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


class PlatformInfoTests(unittest.TestCase):
    def test_system_names(self):
        self.assertEqual(pi.system("darwin"), "macos")
        self.assertEqual(pi.system("linux"), "linux")
        self.assertEqual(pi.system("win32"), "windows")
        self.assertEqual(pi.system("freebsd14"), "other")

    def test_wording(self):
        self.assertEqual(pi.here("macos"), "this Mac")
        self.assertEqual(pi.here("linux"), "this computer")
        self.assertEqual(pi.label("linux"), "Linux")

    def test_app_data_dir(self):
        self.assertEqual(pi.app_data_dir("/Users/a", "macos", env={}), "/Users/a/Library/Application Support")
        self.assertEqual(pi.app_data_dir("/home/a", "linux", env={}), "/home/a/.config")
        self.assertEqual(pi.app_data_dir("/home/a", "linux", env={"XDG_CONFIG_HOME": "/cfg"}), "/cfg")
        # A relative XDG_CONFIG_HOME is invalid per the spec and ignored.
        self.assertEqual(pi.app_data_dir("/home/a", "linux", env={"XDG_CONFIG_HOME": "cfg"}), "/home/a/.config")

    def test_app_dirs(self):
        self.assertEqual(pi.app_dirs("/Users/a", "macos"), ["/Applications", "/Users/a/Applications"])
        dirs = pi.app_dirs("/home/a", "linux", env={})
        self.assertIn("/home/a/.local/share/applications", dirs)
        self.assertIn("/usr/share/applications", dirs)
        self.assertIn("/var/lib/flatpak/exports/share/applications", dirs)
        self.assertFalse(any("Applications" in d for d in dirs))

    def test_open_url_command(self):
        self.assertEqual(pi.open_url_command("http://x/", "macos"), ["open", "http://x/"])
        self.assertEqual(pi.open_url_command("http://x/", "linux", which=lambda n: "/usr/bin/" + n if n == "xdg-open" else None),
                         ["/usr/bin/xdg-open", "http://x/"])
        self.assertEqual(pi.open_url_command("http://x/", "linux", which=lambda n: "/usr/bin/gio" if n == "gio" else None),
                         ["/usr/bin/gio", "open", "http://x/"])
        self.assertIsNone(pi.open_url_command("http://x/", "linux", which=lambda n: None))

    def test_info_has_no_mac_words_on_linux(self):
        info = pi.info(home="/home/a", sys_name="linux")
        self.assertEqual(info["os"], "linux")
        self.assertFalse(info["menuBarApp"])
        self.assertNotIn("Mac", repr(info))
        self.assertNotIn("Library", repr(info))

    def test_tilde(self):
        self.assertEqual(pi.tilde("/home/a/.config/Grok Bot", "/home/a"), "~/.config/Grok Bot")
        self.assertEqual(pi.tilde("/opt/x", "/home/a"), "/opt/x")


class GrokBotPathTests(CleanEnv):
    def test_default_dir_per_os(self):
        self.assertEqual(grokbot_usage.default_persistence_dir("/home/a", "linux", env={}),
                         "/home/a/.config/Grok Bot/sand-client-persistence")
        self.assertEqual(grokbot_usage.default_persistence_dir("/Users/a", "macos", env={}),
                         "/Users/a/Library/Application Support/Grok Bot/sand-client-persistence")

    def test_missing_cache_is_empty_not_an_error(self):
        with tempfile.TemporaryDirectory() as d:
            out = grokbot_usage.collect(directory=os.path.join(d, "absent"), store_path=os.path.join(d, "s.json"))
            self.assertFalse(out["found"])
            self.assertNotIn("error", out)

    def test_linux_detection_reads_config_folder(self):
        with tempfile.TemporaryDirectory() as home:
            _touch(os.path.join(home, ".config", "Grok Bot", "sand-client-persistence", "ABCDE"), "{}")
            with mock.patch("platform_info.system", return_value="linux"):
                rows = {r["id"]: r for r in sd.detect(home=home, path="", app_roots_list=[], data_dir_path="")}
            self.assertTrue(rows[sd.GROK_BOT]["installed"])
            self.assertTrue(rows[sd.GROK_BOT]["hasData"])


class DesktopEntryTests(CleanEnv):
    def test_linux_desktop_entries_count_as_installed(self):
        with tempfile.TemporaryDirectory() as root:
            _touch(os.path.join(root, "com.anthropic.Claude.desktop"))
            _touch(os.path.join(root, "grok-bot.desktop"))
            with tempfile.TemporaryDirectory() as home:
                rows = {r["id"]: r for r in sd.detect(home=home, path="", app_roots_list=[root], data_dir_path="")}
            self.assertTrue(rows[sd.CLAUDE]["installed"])
            self.assertTrue(rows[sd.GROK_BOT]["installed"])
            self.assertFalse(rows[sd.GROK_BOT]["hasData"])

    def test_a_folder_named_like_a_desktop_entry_does_not_count(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, "grok-bot.desktop"))
            with tempfile.TemporaryDirectory() as home:
                rows = {r["id"]: r for r in sd.detect(home=home, path="", app_roots_list=[root], data_dir_path="")}
            self.assertFalse(rows[sd.GROK_BOT]["installed"])

    def test_chat_apps_on_linux(self):
        with tempfile.TemporaryDirectory() as root:
            _touch(os.path.join(root, "cursor.desktop"))
            rows = {r["id"]: r for r in desktop_sources.chat_app_coverage(home="/nonexistent", roots=[root])}
            self.assertTrue(rows["cursor"]["installed"])
            self.assertFalse(rows["chatgpt"]["installed"])


class ClaudeDesktopPathTests(unittest.TestCase):
    def test_path_per_os(self):
        self.assertEqual(desktop_sources.claude_desktop_path("/home/a", "linux"),
                         "/home/a/.config/Claude/plan-usage-history.json")
        self.assertEqual(desktop_sources.claude_desktop_path("/Users/a", "macos"),
                         "/Users/a/Library/Application Support/Claude/plan-usage-history.json")

    def test_linux_file_is_read_and_shown_with_a_linux_path(self):
        with tempfile.TemporaryDirectory() as home:
            _touch(os.path.join(home, ".config", "Claude", "plan-usage-history.json"),
                   '{"version": 2, "samples": [{"t": 1764000000000, "org": "o1", "u": {"fh": 12, "sd": 30}}]}')
            r = desktop_sources.claude_desktop_plan_usage(home=home, sys_name="linux")
            self.assertTrue(r["found"])
            self.assertEqual(r["path"], "~/.config/Claude/plan-usage-history.json")
            self.assertEqual(r["orgs"][0]["latest"]["sevenDayPct"], 30.0)


class PricingRegionTests(unittest.TestCase):
    def test_posix_locale_order(self):
        self.assertEqual(pricing_region.detect_posix_locale({"LANG": "en_GB.UTF-8"}), "en_GB.UTF-8")
        self.assertEqual(pricing_region.detect_posix_locale({"LC_MONETARY": "en_US.UTF-8", "LANG": "en_GB.UTF-8"}), "en_US.UTF-8")
        self.assertEqual(pricing_region.detect_posix_locale({"LC_ALL": "en_GB.UTF-8", "LC_MONETARY": "en_US.UTF-8"}), "en_GB.UTF-8")
        self.assertIsNone(pricing_region.detect_posix_locale({"LANG": "C.UTF-8"}))
        self.assertIsNone(pricing_region.detect_posix_locale({}))

    def test_linux_uses_env_not_defaults(self):
        with mock.patch.object(pricing_region, "detect_mac_locale", side_effect=AssertionError("no defaults on Linux")):
            self.assertEqual(pricing_region.detect_locale("linux", {"LANG": "en_GB.UTF-8"}), "en_GB.UTF-8")

    def test_region_from_linux_locale(self):
        out = pricing_region.current(saved="", locale="en_GB.UTF-8")
        self.assertEqual(out["region"], "UK")
        self.assertEqual(out["source"], "auto")


class FakeRun:
    def __init__(self, code=0):
        self.calls = []
        self.code = code

    def __call__(self, args, **kw):
        self.calls.append(args)

        class R:
            returncode = self.code
        return R()


class NotifySendTests(unittest.TestCase):
    def test_args_are_not_spliced(self):
        run = FakeRun()
        ok = notify.deliver_notify_send('Plan "x"; rm -rf ~', "msg", runner=run, which=lambda n: "/usr/bin/notify-send")
        self.assertTrue(ok)
        self.assertEqual(run.calls[0], ["/usr/bin/notify-send", "--app-name=Agentic Spend", 'Plan "x"; rm -rf ~', "msg"])

    def test_missing_notify_send_fails_quietly(self):
        run = FakeRun()
        self.assertFalse(notify.deliver_notify_send("t", "m", runner=run, which=lambda n: None))
        self.assertEqual(run.calls, [])

    def test_dispatch_on_linux(self):
        run = FakeRun()
        _, delivered = notify.dispatch([{"title": "t", "message": "m"}], {}, runner=run, sys_name="linux",
                                       which=lambda n: "/usr/bin/notify-send")
        self.assertEqual(delivered[0]["via"], "notify-send")
        _, delivered = notify.dispatch([{"title": "t", "message": "m"}], {}, runner=run, sys_name="linux",
                                       which=lambda n: None)
        self.assertEqual(delivered[0]["via"], "failed")

    def test_no_osascript_on_linux(self):
        run = FakeRun()
        notify.dispatch([{"title": "t", "message": "m"}], {}, runner=run, sys_name="linux",
                        which=lambda n: "/usr/bin/notify-send")
        self.assertNotIn("osascript", repr(run.calls))


class FakeSystemctl:
    """systemctl --user: show-environment, is-enabled, is-active, show,
    daemon-reload, enable [--now], disable."""

    def __init__(self, available=True):
        self.available = available
        self.enabled = False
        self.active = False
        self.calls = []

    def __call__(self, args, **kw):
        assert args[:2] == ["systemctl", "--user"], args
        cmd = args[2:]
        self.calls.append(cmd)
        out, code = "", 0
        if not self.available:
            code, out = 1, "Failed to connect to bus"
        elif cmd[0] == "is-enabled":
            out, code = ("enabled", 0) if self.enabled else ("disabled", 1)
        elif cmd[0] == "is-active":
            out, code = ("active", 0) if self.active else ("inactive", 3)
        elif cmd[0] == "show":
            out = "4242" if self.active else "0"
        elif cmd[0] == "enable":
            self.enabled = True
            if "--now" in cmd:
                self.active = True
        elif cmd[0] == "disable":
            self.enabled = False

        class R:
            returncode = code
            stdout = out
            stderr = ""
        return R()


class LinuxLoginTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(login_item, "resolve_launcher",
                                    return_value=(["/home/linuxbrew/.linuxbrew/bin/agentic-spend", "serve"], "brew launcher"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_systemd_on_status_off(self):
        with tempfile.TemporaryDirectory() as home:
            fake = FakeSystemctl()
            st = login_item.turn_on(home=home, runner=fake, sys_name="linux")
            self.assertEqual(st["method"], "systemd")
            self.assertTrue(st["enabled"])
            self.assertTrue(st["loaded"])
            self.assertEqual(st["pid"], 4242)
            unit = login_item.unit_path(home)
            self.assertEqual(unit, os.path.join(home, ".config", "systemd", "user", "agentic-spend.service"))
            with open(unit) as f:
                text = f.read()
            self.assertIn("ExecStart=/home/linuxbrew/.linuxbrew/bin/agentic-spend serve", text)
            self.assertIn("Environment=AGENTIC_SPEND_NO_OPEN=1", text)
            self.assertIn("WantedBy=default.target", text)
            self.assertIn(["enable", "--now", "agentic-spend.service"], fake.calls)
            self.assertEqual(st["programArguments"], ["/home/linuxbrew/.linuxbrew/bin/agentic-spend", "serve"])
            st = login_item.turn_off(home=home, runner=fake, sys_name="linux")
            self.assertFalse(st["enabled"])
            self.assertFalse(os.path.exists(unit))
            # The running server is left alone: disable, never stop.
            self.assertNotIn("stop", [c[0] for c in fake.calls])

    def test_autostart_when_no_systemd_user_session(self):
        with tempfile.TemporaryDirectory() as home:
            fake = FakeSystemctl(available=False)
            st = login_item.turn_on(home=home, runner=fake, sys_name="linux")
            self.assertEqual(st["method"], "autostart")
            self.assertTrue(st["enabled"])
            path = login_item.autostart_path(home)
            with open(path) as f:
                text = f.read()
            self.assertIn("Exec=env AGENTIC_SPEND_NO_OPEN=1 /home/linuxbrew/.linuxbrew/bin/agentic-spend serve", text)
            self.assertFalse(os.path.exists(login_item.unit_path(home)))
            st = login_item.turn_off(home=home, runner=fake, sys_name="linux")
            self.assertFalse(st["enabled"])
            self.assertFalse(os.path.exists(path))

    def test_no_launchctl_on_linux(self):
        with tempfile.TemporaryDirectory() as home:
            calls = []

            def runner(args, **kw):
                calls.append(args)
                return FakeSystemctl()(args, **kw)
            login_item.status(home=home, runner=runner, sys_name="linux")
            self.assertFalse(any(a[0] == "launchctl" for a in calls))

    def test_exec_quoting(self):
        self.assertEqual(login_item._systemd_quote("/a b/c"), '"/a b/c"')
        self.assertEqual(login_item._systemd_quote("50%"), "50%%")
        self.assertEqual(login_item._desktop_quote("/a b/c"), '"/a b/c"')

    def test_other_os_is_unsupported_not_a_crash(self):
        st = login_item.status(sys_name="windows")
        self.assertFalse(st["enabled"])
        self.assertIn("macOS and Linux", st["error"])

    def test_describe_location(self):
        self.assertIn("systemd user service", login_item.describe_location({"method": "systemd", "path": "/x/y.service"}))
        self.assertIn("LaunchAgent", login_item.describe_location({"method": "launchd", "path": "/x/y.plist"}))


class ServerAndDashboardTests(unittest.TestCase):
    def test_usage_payload_carries_platform(self):
        with open(os.path.join(ROOT, "server.py")) as f:
            src = f.read()
        self.assertIn('usage["platform"] = platform_info.info()', src)
        self.assertIn("platform_info.open_url_command(u)", src)
        self.assertNotIn('["open", u]', src)
        self.assertNotIn("Check ~/Library/LaunchAgents", src)

    def test_dashboard_has_no_hard_coded_mac_wording(self):
        src = html()
        self.assertNotIn("this Mac", src.replace('"this Mac" on macOS', ""))
        self.assertNotIn("This Mac", src)
        self.assertNotIn("~/Library/Application Support", src)

    def test_here_follows_the_server(self):
        out = run_js(["platformInfo", "here", "Here"], """
          var data = {platform: {os: 'linux', here: 'this computer'}};
          out.linux = [here(), Here()];
          data = {platform: {os: 'macos', here: 'this Mac'}};
          out.mac = [here(), Here()];
          data = null;
          out.none = here();
        """)
        self.assertEqual(out["linux"], ["this computer", "This computer"])
        self.assertEqual(out["mac"], ["this Mac", "This Mac"])
        self.assertEqual(out["none"], "this computer")

    def test_grokbot_empty_copy_uses_the_os_folder(self):
        out = run_js(["platformInfo", "here", "grokBotEmptyCopy"], """
          var data = {platform: {os: 'linux', here: 'this computer', appDataDir: '~/.config'}};
          out.text = grokBotEmptyCopy();
        """)
        self.assertIn("~/.config/Grok Bot/", out["text"])
        self.assertNotIn("Mac", out["text"].replace("macOS", ""))


class FormulaTests(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(ROOT, "Formula", "agentic-spend.rb")) as f:
            self.rb = f.read()

    def test_not_mac_only(self):
        self.assertNotIn("depends_on :macos", self.rb)

    def test_linux_ccusage_resources(self):
        for arch, sha in (("x64", "5499db393a805f8b47e9738bc47eea9b020ef93be2930731443606feb1fca003"),
                          ("arm64", "daf02e2632fc15f216745d153b1c40f83e249635803da2f3877f37fde2b67903")):
            url = "https://registry.npmjs.org/@ccusage/ccusage-linux-%s/-/ccusage-linux-%s-20.1.2.tgz" % (arch, arch)
            i = self.rb.index(url)
            self.assertIn('sha256 "%s"' % sha, self.rb[i:i + 200])
        self.assertIn("on_linux do", self.rb)
        self.assertIn("on_macos do", self.rb)

    def test_launcher_opens_browser_per_os(self):
        self.assertIn("xdg-open", self.rb)
        self.assertNotIn('open "$URL" 2>/dev/null ||', self.rb)

    def test_menu_bar_caveat_is_mac_only(self):
        i = self.rb.index("return s unless OS.mac?")
        self.assertLess(i, self.rb.index("Menu bar app"))


if __name__ == "__main__":
    unittest.main()
