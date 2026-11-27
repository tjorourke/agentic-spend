"""1.8.3: fixed-size chart text, optional local signing, menu bar reset guidance."""
import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

from test_v180_features import NODE, html, run_js  # noqa: E402


def read(*parts):
    with open(os.path.join(ROOT, *parts)) as f:
        return f.read()


class ChartSizingTests(unittest.TestCase):
    """SVG charts must have a pixel height matching their viewBox, or the browser
    scales them (and their labels) with the card's width."""

    def test_every_drawn_chart_uses_size_helper(self):
        h = html()
        direct = [l.strip() for l in h.splitlines()
                  if "setAttribute('viewBox'" in l and "function sizeSvg" not in l]
        # the only direct call is the one inside sizeSvg itself
        self.assertEqual(len(direct), 1, direct)
        body = re.search(r"^function sizeSvg\([\s\S]*?\n}\n", h, re.M).group(0)
        self.assertIn("svg.style.height = H + 'px'", body)

    def test_cache_read_chart_has_fixed_height(self):
        h = html()
        self.assertRegex(h, r"#cacheReadChart \{[^}]*height: 110px")
        fn = re.search(r"^function drawCacheReadChart\([\s\S]*?\n}\n", h, re.M).group(0)
        self.assertEqual(fn.count("sizeSvg(svg, W, H)"), 2)  # single-day bar and the bar chart

    def test_inline_svgs_set_pixel_height(self):
        h = html()
        for m in re.finditer(r"<svg role=\"img\"[^>]*viewBox[^>]*>", h):
            self.assertIn("height:", m.group(0))
        self.assertIn("height:' + H + 'px", h)

    @unittest.skipUnless(NODE, "node not installed")
    def test_size_helper_sets_one_unit_per_pixel(self):
        out = run_js(["sizeSvg"], """
          const attrs = {}, style = {};
          const svg = { setAttribute: (k, v) => { attrs[k] = v; }, style };
          sizeSvg(svg, 1688, 110);
          out.attrs = attrs; out.style = style;
        """)
        self.assertEqual(out["attrs"]["viewBox"], "0 0 1688 110")
        self.assertEqual(out["style"]["height"], "110px")
        self.assertEqual(out["style"]["width"], "100%")


class SigningTests(unittest.TestCase):
    def test_local_identity_optional_ad_hoc_fallback(self):
        b = read("macos", "build.sh")
        self.assertIn("AGENTIC_SPEND_SIGN_IDENTITY", b)
        self.assertIn('"Agentic Spend Local"', b)
        self.assertIn('codesign --force --deep --sign - --timestamp=none "$APP"', b)
        self.assertLess(b.index('AgenticSpend" --self-test'), b.index("codesign --force"))

    def test_no_secrets_or_certificates_in_repo(self):
        for dirpath, dirs, files in os.walk(ROOT):
            dirs[:] = [d for d in dirs if d not in (".git", "out", "node_modules")]
            for f in files:
                self.assertFalse(f.endswith((".p12", ".pem", ".key", ".cer", ".keychain", ".keychain-db")),
                                 os.path.join(dirpath, f))


class MenuBarGuidanceTests(unittest.TestCase):
    def test_hidden_help_gives_reset(self):
        onb = read("macos", "Sources", "Onboarding.swift")
        self.assertIn("brew uninstall --cask agentic-spend-app", onb)
        self.assertIn("System Settings > Menu Bar", onb)
        cask = read("Casks", "agentic-spend-app.rb")
        self.assertIn("brew uninstall --cask agentic-spend-app", cask)

    def test_bundle_id_unchanged(self):
        # Changing it loses every user's settings; only with Tom's say-so.
        plist = read("macos", "Resources", "Info.plist")
        self.assertRegex(plist, r"<key>CFBundleIdentifier</key>\s*<string>com.agenticspend.menubar</string>")


if __name__ == "__main__":
    unittest.main()
