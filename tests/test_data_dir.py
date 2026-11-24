import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))
os.environ.setdefault("AGENTIC_SPEND_DATA_DIR", tempfile.mkdtemp(prefix="agentic-spend-test-"))

import data_dir  # noqa: E402


class DataDirTests(unittest.TestCase):
    def test_path_joins_under_data_dir(self):
        self.assertEqual(data_dir.path("plans.json"), os.path.join(data_dir.DATA_DIR, "plans.json"))

    def test_data_dir_exists(self):
        self.assertTrue(os.path.isdir(data_dir.DATA_DIR))

    def test_data_dir_honors_env_override(self):
        self.assertEqual(data_dir.DATA_DIR, os.environ["AGENTIC_SPEND_DATA_DIR"])


if __name__ == "__main__":
    unittest.main()
