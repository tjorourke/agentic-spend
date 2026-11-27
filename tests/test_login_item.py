import os
import plistlib
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))
os.environ.setdefault("AGENTIC_SPEND_DATA_DIR", tempfile.mkdtemp(prefix="agentic-spend-test-"))

import login_item  # noqa: E402


class FakeLaunchctl:
    """Simulates launchctl print/bootstrap/bootout for one label."""

    def __init__(self, loaded=False, pid=None):
        self.loaded = loaded
        self.pid = pid
        self.calls = []

    def __call__(self, args, **kw):
        self.calls.append(args[1:])
        out = ""
        code = 0
        if args[1] == "print":
            if self.loaded:
                out = "gui/501/%s = {\n\tstate = running\n%s}\n" % (
                    login_item.LABEL, ("\tpid = %d\n" % self.pid) if self.pid else "")
            else:
                code = 113
        elif args[1] == "bootstrap":
            self.loaded = True
        elif args[1] == "bootout":
            self.loaded = False
            self.pid = None

        class R:
            pass
        r = R()
        r.returncode = code
        r.stdout = out
        r.stderr = ""
        return r


class ResolveLauncherTests(unittest.TestCase):
    def test_prefers_stable_brew_path_over_cellar(self):
        here = "/opt/homebrew/Cellar/agentic-spend/1.4.0/libexec"
        exists = lambda p: p in ("/opt/homebrew/bin/agentic-spend", "/opt/homebrew/Cellar/agentic-spend/1.4.0/bin/agentic-spend")
        args, how = login_item.resolve_launcher(here=here, which=lambda n: "/opt/homebrew/Cellar/agentic-spend/1.4.0/bin/agentic-spend", exists=exists, env={})
        self.assertEqual(args, ["/opt/homebrew/bin/agentic-spend", "serve"])
        self.assertEqual(how, "brew launcher")

    def test_opt_libexec_maps_to_prefix_bin(self):
        self.assertEqual(login_item._brew_bin_from_libexec("/usr/local/opt/agentic-spend/libexec"), "/usr/local/bin/agentic-spend")
        self.assertIsNone(login_item._brew_bin_from_libexec("/Users/x/code/agentic-spend"))

    def test_git_clone_fallback_runs_server(self):
        args, how = login_item.resolve_launcher(here="/tmp/clone", which=lambda n: "/usr/bin/python3" if n == "python3" else None,
                                                exists=lambda p: False, env={})
        self.assertEqual(args, ["/usr/bin/python3", "/tmp/clone/server.py"])

    def test_env_override(self):
        args, _ = login_item.resolve_launcher(here="/tmp/x", which=lambda n: None, exists=lambda p: p == "/custom/agentic-spend",
                                              env={"AGENTIC_SPEND_LAUNCHER": "/custom/agentic-spend"})
        self.assertEqual(args[0], "/custom/agentic-spend")


class PlistTests(unittest.TestCase):
    def test_plist_shape(self):
        pl = login_item.build_plist(["/opt/homebrew/bin/agentic-spend", "serve"], home="/Users/test", claude_config_dir="")
        self.assertNotIn("CLAUDE_CONFIG_DIR", pl["EnvironmentVariables"])
        self.assertEqual(pl["Label"], "com.agenticspend.server")
        self.assertIs(pl["RunAtLoad"], True)
        self.assertIs(pl["KeepAlive"], False)
        self.assertTrue(pl["EnvironmentVariables"]["PATH"].startswith("/opt/homebrew/bin:"))
        self.assertEqual(pl["EnvironmentVariables"]["AGENTIC_SPEND_NO_OPEN"], "1")
        # Round-trips through plistlib (what launchd reads).
        data = plistlib.dumps(pl)
        self.assertEqual(plistlib.loads(data)["ProgramArguments"], ["/opt/homebrew/bin/agentic-spend", "serve"])


    def test_plist_carries_claude_config_dirs(self):
        pl = login_item.build_plist(["/opt/homebrew/bin/agentic-spend", "serve"], home="/Users/test",
                                    claude_config_dir="/Users/test/.claude,/Users/test/.claude-work")
        self.assertEqual(pl["EnvironmentVariables"]["CLAUDE_CONFIG_DIR"],
                         "/Users/test/.claude,/Users/test/.claude-work")


class OnOffTests(unittest.TestCase):
    def test_on_status_off_reads_back(self):
        with tempfile.TemporaryDirectory() as home:
            fake = FakeLaunchctl()
            st = login_item.turn_on(home=home, runner=fake)
            self.assertTrue(st["enabled"])
            self.assertTrue(st["loaded"])
            self.assertTrue(os.path.exists(login_item.plist_path(home)))
            self.assertIn(["bootstrap", "gui/%d" % os.getuid(), login_item.plist_path(home)], fake.calls)
            st = login_item.turn_off(home=home, runner=fake)
            self.assertFalse(st["enabled"])
            self.assertFalse(os.path.exists(login_item.plist_path(home)))

    def test_off_leaves_running_server_alone(self):
        with tempfile.TemporaryDirectory() as home:
            fake = FakeLaunchctl()
            login_item.turn_on(home=home, runner=fake)
            fake.pid = 4242
            st = login_item.turn_off(home=home, runner=fake)
            self.assertFalse(st["enabled"])
            self.assertNotIn("bootout", [c[0] for c in fake.calls])

    def test_status_when_absent(self):
        with tempfile.TemporaryDirectory() as home:
            st = login_item.status(home=home, runner=FakeLaunchctl())
            self.assertFalse(st["enabled"])
            self.assertFalse(st["loaded"])


if __name__ == "__main__":
    unittest.main()
