"""ccusage path resolution and the no-node path."""
import os
import stat
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))
os.environ.setdefault("AGENTIC_SPEND_DATA_DIR", tempfile.mkdtemp(prefix="agentic-spend-test-"))

import account_usage  # noqa: E402
import ccusage_bin  # noqa: E402
import server  # noqa: E402


def make_exe(folder, name="ccusage"):
    path = os.path.join(folder, name)
    with open(path, "w") as f:
        f.write("#!/bin/sh\necho ok\n")
    os.chmod(path, stat.S_IRWXU)
    return path


def no_which(_name):
    return None


class ResolveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name

    def test_env_var_wins_over_bundled_and_path(self):
        env_bin = make_exe(self.dir, "mine")
        bundled = make_exe(self.dir, "bundled")
        found = ccusage_bin.resolve(env={ccusage_bin.ENV_VAR: env_bin}, bundled=bundled, which=lambda n: "/usr/bin/ccusage")
        self.assertEqual((found["source"], found["argv"]), ("env", [env_bin]))

    def test_env_var_may_be_a_command_name_on_path(self):
        env_bin = make_exe(self.dir, "ccu")
        found = ccusage_bin.resolve(env={ccusage_bin.ENV_VAR: "ccu"}, bundled="/nope", which=lambda n: env_bin if n == "ccu" else None)
        self.assertEqual(found["source"], "env")

    def test_bad_env_var_is_an_error_not_a_silent_fallback(self):
        bundled = make_exe(self.dir, "bundled")
        found = ccusage_bin.resolve(env={ccusage_bin.ENV_VAR: "/does/not/exist"}, bundled=bundled, which=no_which)
        self.assertEqual(found["source"], "missing")
        self.assertIn(ccusage_bin.ENV_VAR, found["error"])
        self.assertIsNone(found["argv"])

    def test_bundled_binary_is_used_before_path(self):
        bundled = make_exe(self.dir, "bundled")
        found = ccusage_bin.resolve(env={}, bundled=bundled, which=lambda n: "/usr/local/bin/ccusage")
        self.assertEqual((found["source"], found["argv"]), ("bundled", [bundled]))

    def test_path_ccusage_when_nothing_bundled(self):
        found = ccusage_bin.resolve(env={}, bundled=os.path.join(self.dir, "missing"),
                                    which=lambda n: "/usr/local/bin/ccusage" if n == "ccusage" else None)
        self.assertEqual((found["source"], found["argv"]), ("path", ["/usr/local/bin/ccusage"]))

    def test_non_executable_bundled_file_is_skipped(self):
        path = os.path.join(self.dir, "ccusage")
        open(path, "w").close()
        os.chmod(path, 0o644)
        found = ccusage_bin.resolve(env={}, bundled=path, which=no_which)
        self.assertEqual(found["source"], "missing")

    def test_npx_is_only_the_last_resort_and_says_so(self):
        found = ccusage_bin.resolve(env={}, bundled="/nope", which=lambda n: "/usr/bin/npx" if n == "npx" else None)
        self.assertEqual(found["source"], "npx")
        self.assertEqual(found["argv"], ["/usr/bin/npx", "--yes", f"ccusage@{ccusage_bin.CCUSAGE_VERSION}"])
        text = ccusage_bin.describe(found)
        self.assertIn("falling back to npx", text)
        self.assertIn("Node.js", text)

    def test_nothing_available_is_a_clear_error(self):
        found = ccusage_bin.resolve(env={}, bundled="/nope", which=no_which)
        self.assertEqual(found["source"], "missing")
        self.assertIsNone(found["argv"])
        self.assertIn("Homebrew", found["error"])

    def test_pinned_version(self):
        self.assertEqual(ccusage_bin.CCUSAGE_VERSION, "20.1.2")


class NoNodeTests(unittest.TestCase):
    def test_command_never_mentions_npx_when_bundled_exists(self):
        with tempfile.TemporaryDirectory() as d:
            bundled = make_exe(d, "ccusage")
            with mock.patch.object(ccusage_bin, "BUNDLED", bundled), mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop(ccusage_bin.ENV_VAR, None)
                argv = ccusage_bin.command("--sections", "daily,session", "--json", "--by-agent")
        self.assertEqual(argv, [bundled, "--sections", "daily,session", "--json", "--by-agent"])
        self.assertNotIn("npx", " ".join(argv))

    def test_command_raises_with_ccusage_filename_when_nothing_found(self):
        with self.assertRaises(FileNotFoundError) as cm:
            ccusage_bin.command("blocks", env={}, bundled="/nope", which=no_which)
        self.assertEqual(cm.exception.filename, "ccusage")

    def test_server_runs_the_bundled_binary_with_no_node_on_path(self):
        with tempfile.TemporaryDirectory() as d:
            bundled = make_exe(d, "ccusage")
            seen = []

            def fake_run(cmd, **kw):
                seen.append(cmd)
                raise RuntimeError("stop here")

            with mock.patch.object(ccusage_bin, "BUNDLED", bundled), \
                 mock.patch.dict(os.environ, {"PATH": "/usr/bin:/bin"}), \
                 mock.patch.object(server.subprocess, "run", fake_run):
                os.environ.pop(ccusage_bin.ENV_VAR, None)
                result = server.refresh_usage()
        self.assertEqual(seen[0][0], bundled)
        self.assertNotIn("npx", seen[0])
        self.assertIn("stop here", result["error"])

    def test_missing_ccusage_reports_a_ccusage_error_not_node(self):
        with mock.patch.object(ccusage_bin, "resolve", return_value={
                "argv": None, "source": "missing", "path": None, "error": "ccusage was not found. Install via Homebrew."}):
            result = server.refresh_usage()
        self.assertEqual(result["errorType"], "ccusage_missing")
        self.assertIn("ccusage was not found", result["error"])
        self.assertNotIn("Node", result["error"])

    def test_account_usage_session_call_uses_resolver(self):
        with tempfile.TemporaryDirectory() as d:
            bundled = make_exe(d, "ccusage")
            seen = []

            class R:
                stdout = '{"session": []}'

            def fake_run(cmd, **kw):
                seen.append(cmd)
                return R()

            with mock.patch.object(ccusage_bin, "BUNDLED", bundled), \
                 mock.patch.object(account_usage.subprocess, "run", fake_run):
                os.environ.pop(ccusage_bin.ENV_VAR, None)
                self.assertEqual(account_usage.run_ccusage_sessions(since="20261101"), [])
        self.assertEqual(seen[0], [bundled, "session", "--json", "--by-agent", "--since", "20261101"])


if __name__ == "__main__":
    unittest.main()
