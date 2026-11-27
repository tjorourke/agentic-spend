"""The Linux port: a systemd user unit for start at login, notify-send, and the
region from the session locale. Everything runs against temp directories and
fake runners, so no real unit file or desktop is touched."""
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.join(os.path.dirname(__file__), os.pardir)
sys.path.insert(0, ROOT)

import login_item_linux  # noqa: E402
import notify  # noqa: E402
import pricing_region  # noqa: E402


class FakeSystemctl:
    def __init__(self):
        self.calls, self.enabled, self.active = [], False, False

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        args = argv[2:]
        out = ""
        if args[0] == "enable":
            self.enabled = True
        elif args[0] == "disable":
            self.enabled = False
        elif args[0] == "start":
            self.active = True
        elif args[0] == "show":
            out = "ActiveState=%s\nMainPID=%d\nUnitFileState=%s\n" % (
                "active" if self.active else "inactive", 4242 if self.active else 0,
                "enabled" if self.enabled else "disabled")
        r = mock.Mock()
        r.returncode, r.stdout, r.stderr = 0, out, ""
        return r


class LoginItemLinuxTests(unittest.TestCase):
    def test_unit_shape(self):
        unit = login_item_linux.build_unit(["/home/me/.local/bin/agentic-spend", "serve"], path_env="/x/bin:/usr/bin")
        self.assertIn('ExecStart="/home/me/.local/bin/agentic-spend" "serve"\n', unit)
        self.assertIn("AGENTIC_SPEND_NO_FIRST_RUN_OPEN=1", unit)
        self.assertIn('Environment="PATH=/home/me/.local/bin:/x/bin:/usr/bin:/usr/local/bin:/bin"', unit)
        self.assertIn("Restart=no", unit)
        self.assertIn("WantedBy=default.target", unit)
        self.assertIn("AGENTIC_SPEND_NO_OPEN=1", unit)

    def test_on_status_off_reads_back(self):
        with tempfile.TemporaryDirectory() as home:
            run = FakeSystemctl()
            self.assertFalse(login_item_linux.status(home, run)["enabled"])
            st = login_item_linux.turn_on(home, run)
            self.assertTrue(st["enabled"] and st["loaded"])
            self.assertEqual(st["pid"], 4242)
            self.assertEqual(st["manager"], "systemd")
            self.assertTrue(st["plistPath"].startswith(home))
            self.assertEqual(st["programArguments"][-1] in ("serve",) or st["programArguments"][-1].endswith("server.py"), True)
            self.assertTrue(all(c[:2] == ["systemctl", "--user"] for c in run.calls))
            st = login_item_linux.turn_off(home, run)
            self.assertFalse(st["enabled"])
            self.assertFalse(os.path.exists(st["plistPath"]))
            self.assertTrue(st["loaded"], "a running server is left alone")
            self.assertNotIn("stop", [c[2] for c in run.calls])

    def test_unit_uses_systemd_quoting(self):
        argv = ["/odd dir/50%/a$b/agentic-spend", "se$rve"]
        unit = login_item_linux.build_unit(argv, path_env="/ok/bin:/bad\\dir:/bad%dir")
        self.assertIn('ExecStart="/odd dir/50%%/a$b/agentic-spend" "se$$rve"\n', unit)
        self.assertNotIn("/bad", unit)
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "u.service")
            with open(path, "w") as f:
                f.write(unit)
            self.assertEqual(login_item_linux._exec_start(path), argv)

    def test_no_file_left_when_enable_fails(self):
        def no_systemd(argv, **kwargs):
            raise FileNotFoundError("systemctl")

        with tempfile.TemporaryDirectory() as home:
            st = login_item_linux.turn_on(home, no_systemd)
            self.assertFalse(st["enabled"])
            self.assertFalse(os.path.exists(st["plistPath"]))

    def test_file_without_enable_is_not_on(self):
        with tempfile.TemporaryDirectory() as home:
            run = FakeSystemctl()
            login_item_linux.turn_on(home, run)
            run.enabled = False
            self.assertFalse(login_item_linux.status(home, run)["enabled"])


class LinuxDeliveryTests(unittest.TestCase):
    def test_notify_send_args_follow_double_dash(self):
        calls = []

        def run(argv, **kwargs):
            calls.append(argv)
            return mock.Mock(returncode=0)

        with mock.patch.object(notify, "IS_LINUX", True):
            self.assertEqual(notify.deliver("--urgency=critical", "msg", runner=run), "notify-send")
        argv = calls[0]
        self.assertEqual(argv[0], "notify-send")
        self.assertEqual(argv[argv.index("--") + 1:], ["--urgency=critical", "msg"])

    def test_failure_is_reported(self):
        with mock.patch.object(notify, "IS_LINUX", True):
            self.assertEqual(notify.deliver("t", "m", runner=lambda *a, **k: mock.Mock(returncode=1)), "failed")

    def test_macos_path_unchanged(self):
        with mock.patch.object(notify, "IS_LINUX", False):
            self.assertEqual(notify.deliver("t", "m", runner=lambda *a, **k: mock.Mock(returncode=0)), "osascript")


class LinuxLocaleTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux only")
    def test_region_from_session_locale(self):
        with mock.patch.dict(os.environ, {"LC_ALL": "", "LC_MONETARY": "en_GB.UTF-8", "LANG": "en_US.UTF-8"}):
            self.assertEqual(pricing_region.region_from_locale(pricing_region.detect_mac_locale()), "UK")
        with mock.patch.dict(os.environ, {"LC_ALL": "", "LC_MONETARY": "", "LANG": "de_DE.UTF-8"}):
            self.assertIsNone(pricing_region.region_from_locale(pricing_region.detect_mac_locale()))


if __name__ == "__main__":
    unittest.main()
