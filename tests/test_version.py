"""The two version strings move together (README, "Releasing")."""

import os
import re
import unittest

import couchy_agent

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class VersionTest(unittest.TestCase):
    def test_pyproject_matches_init(self):
        with open(os.path.join(ROOT, "pyproject.toml")) as f:
            m = re.search(r'^version = "([^"]+)"', f.read(), re.M)
        self.assertIsNotNone(m, "pyproject.toml has no version")
        self.assertEqual(m.group(1), couchy_agent.__version__)


if __name__ == "__main__":
    unittest.main()
