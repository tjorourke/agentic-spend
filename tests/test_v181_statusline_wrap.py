"""1.8.1: the status line hook never wraps itself.

A leftover AGENTIC_SPEND_WRAP_STATUSLINE in a long-running Claude Code
session (the env observed on PID 25063) used to point this script at a
copy of itself and spawn hundreds of processes. These tests cover the
three guards — the active flag, a wrap command that resolves to this
project's statusline.py, and the recursion depth cap — and run the hook
end-to-end with that env.
"""
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
sys.path.insert(0, ROOT)

import statusline  # noqa: E402

SCRIPT = os.path.join(ROOT, "statusline.py")
# The exact wrap command taken from PID 25063 on Tom's Mac, 26 Nov 2026.
PID_25063_WRAP = (
    "python3 -I /Users/thomasorourke/code/agentic-spend/statusline.py"
)
PAYLOAD = json.dumps({
    "workspace": {"current_dir": "/tmp/demo-project"},
    "model": {"display_name": "Opus"},
    "rate_limits": {
        "five_hour": {"used_percentage": 12, "resets_at": 1},
        "seven_day": {"used_percentage": 9, "resets_at": 2},
    },
})
CLEAN_ENV_DROP = (
    "AGENTIC_SPEND_WRAP_STATUSLINE",
    "AGENTIC_SPEND_STATUSLINE_ACTIVE",
    "AGENTIC_SPEND_STATUSLINE_DEPTH",
)


def hook_env(home, extra=None):
    env = {k: v for k, v in os.environ.items() if k not in CLEAN_ENV_DROP}
    env["HOME"] = home
    env["AGENTIC_SPEND_DATA_DIR"] = os.path.join(home, "data")
    if extra:
        env.update(extra)
    return env


def run_hook(env, timeout=5):
    return subprocess.run(
        [sys.executable, "-I", SCRIPT],
        input=PAYLOAD,
        text=True,
        capture_output=True,
        env=env,
        timeout=timeout,
    )


class WrapIsSelfTests(unittest.TestCase):
    def test_repo_cellar_and_opt_paths(self):
        for command in (
            PID_25063_WRAP,
            "python3 -I /opt/homebrew/Cellar/agentic-spend/1.8.0/libexec/statusline.py",
            "python3 -I /opt/homebrew/opt/agentic-spend/libexec/statusline.py",
            "python3 -I /usr/local/Cellar/agentic-spend/1.8.1/libexec/statusline.py",
            "python3 -I /usr/local/opt/agentic-spend/libexec/statusline.py",
            "python3 -I '/Users/thomasorourke/code/agentic-spend/statusline.py'",
            'python3 -I "/opt/homebrew/opt/agentic-spend/libexec/statusline.py"',
            "python3 -I " + SCRIPT,
        ):
            self.assertTrue(statusline.wrap_is_self(command), command)

    def test_foreign_commands_are_not_self(self):
        for command in (
            "python3 -I /some/other/statusline.py",
            "python3 -I /tmp/not-agentic-spend/statusline.py",
            "~/bin/my-status",
            "echo hello",
            "AGENTIC_SPEND_WRAP_STATUSLINE=foo python3 -I /usr/local/bin/myline",
        ):
            self.assertFalse(statusline.wrap_is_self(command), command)

    def test_resolves_the_running_script_even_outside_the_usual_paths(self):
        with tempfile.TemporaryDirectory() as d:
            copy = os.path.join(d, "statusline.py")
            with open(copy, "w") as f:
                f.write("# copy\n")
            command = "python3 -I " + copy
            self.assertTrue(statusline.wrap_is_self(command, script_path=copy))
            self.assertFalse(statusline.wrap_is_self(command, script_path=SCRIPT))

    def test_empty_is_not_self(self):
        self.assertFalse(statusline.wrap_is_self(""))
        self.assertFalse(statusline.wrap_is_self(None))


class ShouldWrapTests(unittest.TestCase):
    FOREIGN = "echo FOREIGN-LINE"

    def test_empty_or_blank_is_not_wrapped(self):
        self.assertFalse(statusline.should_wrap(""))
        self.assertFalse(statusline.should_wrap("   "))
        self.assertFalse(statusline.should_wrap(None))

    def test_active_flag_skips_even_a_foreign_command(self):
        env = {statusline.ACTIVE_FLAG: "1"}
        self.assertFalse(statusline.should_wrap(self.FOREIGN, env=env))

    def test_depth_guard_skips_when_at_or_above_max(self):
        env = {statusline.DEPTH_FLAG: "1"}
        self.assertFalse(statusline.should_wrap(self.FOREIGN, env=env))
        env = {statusline.DEPTH_FLAG: "2"}
        self.assertFalse(statusline.should_wrap(self.FOREIGN, env=env))

    def test_unreadable_or_negative_depth_refuses_to_wrap(self):
        self.assertFalse(statusline.should_wrap(self.FOREIGN, env={statusline.DEPTH_FLAG: "nope"}))
        self.assertFalse(statusline.should_wrap(self.FOREIGN, env={statusline.DEPTH_FLAG: "-1"}))

    def test_self_command_is_not_wrapped(self):
        env = {}
        self.assertFalse(statusline.should_wrap(PID_25063_WRAP, env=env))
        self.assertFalse(statusline.should_wrap("python3 -I " + SCRIPT, env=env))

    def test_foreign_command_is_wrapped_from_a_clean_env(self):
        self.assertTrue(statusline.should_wrap(self.FOREIGN, env={}))

    def test_pid_25063_env_is_not_wrapped(self):
        # Reproduces the env taken from the long-running Claude Code session:
        # WRAP pointed at the repo script, and the active flag was not set
        # on the first hook invocation.
        env = {"AGENTIC_SPEND_WRAP_STATUSLINE": PID_25063_WRAP}
        self.assertFalse(statusline.should_wrap(env["AGENTIC_SPEND_WRAP_STATUSLINE"], env=env))

    def test_wrapped_env_sets_the_flag_and_increments_depth(self):
        out = statusline.wrapped_env(env={"PATH": "/bin"})
        self.assertEqual(out[statusline.ACTIVE_FLAG], "1")
        self.assertEqual(out[statusline.DEPTH_FLAG], "1")
        out = statusline.wrapped_env(env={"PATH": "/bin", statusline.DEPTH_FLAG: "0"})
        self.assertEqual(out[statusline.DEPTH_FLAG], "1")


class HookEndToEndTests(unittest.TestCase):
    def test_pid_25063_env_finishes_quickly_and_prints_our_line(self):
        with tempfile.TemporaryDirectory() as home:
            env = hook_env(home, {"AGENTIC_SPEND_WRAP_STATUSLINE": PID_25063_WRAP})
            start = time.monotonic()
            result = run_hook(env, timeout=5)
            elapsed = time.monotonic() - start
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertLess(elapsed, 2, "self-wrap would block until the 5s timeout")
            self.assertIn("Opus", result.stdout)
            self.assertIn("5h 12%", result.stdout)
            self.assertIn("7d 9%", result.stdout)

    def test_wrap_of_this_script_path_does_not_reenter(self):
        with tempfile.TemporaryDirectory() as home:
            wrap = "%s -I %s" % (sys.executable, SCRIPT)
            env = hook_env(home, {"AGENTIC_SPEND_WRAP_STATUSLINE": wrap})
            start = time.monotonic()
            result = run_hook(env, timeout=5)
            elapsed = time.monotonic() - start
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertLess(elapsed, 2)
            self.assertIn("Opus", result.stdout)

    def test_foreign_command_still_wrapped(self):
        with tempfile.TemporaryDirectory() as home:
            env = hook_env(home, {"AGENTIC_SPEND_WRAP_STATUSLINE": "echo FOREIGN-LINE"})
            result = run_hook(env, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.rstrip("\n"), "FOREIGN-LINE")

    def test_active_flag_prints_our_line_and_skips_the_foreign_command(self):
        with tempfile.TemporaryDirectory() as home:
            env = hook_env(home, {
                "AGENTIC_SPEND_WRAP_STATUSLINE": "echo FOREIGN-LINE",
                "AGENTIC_SPEND_STATUSLINE_ACTIVE": "1",
            })
            result = run_hook(env, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Opus", result.stdout)
            self.assertNotIn("FOREIGN-LINE", result.stdout)

    def test_recursion_guard_stops_reentry_through_a_helper(self):
        # The helper is not statusline.py, so wrap_is_self does not catch it.
        # Without the depth / active-flag guard this would re-enter until the
        # subprocess timeout. With the guard, one wrap is enough and the hook
        # returns quickly.
        with tempfile.TemporaryDirectory() as home:
            helper = os.path.join(home, "reenter.sh")
            with open(helper, "w") as f:
                f.write("#!/bin/sh\nexec %s -I %s\n" % (sys.executable, SCRIPT))
            os.chmod(helper, stat.S_IRWXU)
            env = hook_env(home, {"AGENTIC_SPEND_WRAP_STATUSLINE": helper})
            start = time.monotonic()
            result = run_hook(env, timeout=5)
            elapsed = time.monotonic() - start
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertLess(elapsed, 2, "helper re-entry should be stopped by the depth guard")
            self.assertIn("Opus", result.stdout)


if __name__ == "__main__":
    unittest.main()
