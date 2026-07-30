import shutil
import subprocess
import unittest
from pathlib import Path


class SessionViewBrowserTests(unittest.TestCase):
    def test_optimistic_state_machine(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is not installed")
        script = Path(__file__).with_name("session_view_test.js")

        completed = subprocess.run(
            [node, str(script)],
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(
            completed.returncode,
            0,
            completed.stdout + completed.stderr,
        )


if __name__ == "__main__":
    unittest.main()
