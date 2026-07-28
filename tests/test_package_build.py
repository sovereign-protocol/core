import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

# Run in isolated mode with no PYTHONPATH so the checkout on sys.path cannot
# satisfy an import the wheel failed to package. Without -I this passes on a
# wheel that ships no assets at all.
SMOKE = """
from importlib.metadata import version
from importlib.resources import files
import sovereign

assert version('sovereign-protocol') == sovereign.__version__
assert files('sovereign.assets').joinpath('shared.js').is_file()
assert files('sovereign.assets').joinpath('shared-api.js').is_file()
assert files('sovereign.assets').joinpath('shared.css').is_file()
assert files('sovereign.assets').joinpath('manual.html').is_file()
assert sovereign.__all__
"""


class PackageBuildTests(unittest.TestCase):
    def test_wheel_installs_and_imports_in_an_isolated_environment(self):
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            wheels = work / "wheels"
            wheels.mkdir()
            environment = dict(os.environ)
            environment.pop("PYTHONPATH", None)
            environment["PYTHONNOUSERSITE"] = "1"

            def run(*command):
                subprocess.run(
                    [str(item) for item in command],
                    cwd=work, env=environment,
                    check=True, capture_output=True, text=True,
                )

            run(sys.executable, "-m", "pip", "wheel", "--no-deps",
                "--no-build-isolation", "--wheel-dir", wheels, ROOT)

            virtual_environment = work / "venv"
            run(sys.executable, "-m", "venv", virtual_environment)
            python = virtual_environment / (
                "Scripts/python.exe" if os.name == "nt" else "bin/python"
            )
            built = sorted(wheels.glob("*.whl"))
            self.assertEqual(len(built), 1, built)
            run(python, "-m", "pip", "install", "--no-index", "--no-deps", *built)
            run(python, "-I", "-c", SMOKE)


if __name__ == "__main__":
    unittest.main()
