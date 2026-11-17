"""Unified QA entry point.

Runs the independent QA layer (reference model, properties, shrinking,
fault localisation) plus the cross-layer production contracts and the
original unit tests with a single stdlib-only command:

    python -m qa_tests.run_all
"""
import sys
from pathlib import Path

# Make the repository root importable before importing any QA package,
# including under ``python -I`` where PYTHONPATH is ignored.
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import qa_support.paths  # noqa: F401,E402  (puts src/ on sys.path)

import unittest


def build_suite() -> unittest.TestSuite:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    # QA layer is a proper package under the repository root.
    suite.addTests(loader.discover(
        start_dir=str(_ROOT / "qa_tests"), top_level_dir=str(_ROOT)
    ))
    # The legacy tests/ directory is not a package; discover it in place so
    # the original ``unittest discover -s tests`` layout stays untouched.
    suite.addTests(loader.discover(
        start_dir=str(_ROOT / "tests"), pattern="test_*.py",
        top_level_dir=str(_ROOT / "tests"),
    ))
    return suite


def main() -> int:
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(build_suite())
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
