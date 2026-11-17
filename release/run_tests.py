"""Deterministic unittest runner that emits a stable JSON summary.

Invocation::

    python3 -I release/run_tests.py [ROOT]

The root defaults to the directory containing this runner. The runner:

* requires isolated mode (``-I``) and inserts ``<root>/src`` itself;
* runs the bundled ``unittest`` suite with a fixed, sorted test order;
* prints one canonical JSON document on stdout containing per-test outcomes,
  counts and the wall-clock duration (duration is the only non-deterministic
  value; pass/fail outcomes and IDs are byte-stable across machines).
"""

from __future__ import annotations

import io
import json
import sys
import time
import unittest
from pathlib import Path


def main(argv: list[str]) -> int:
    if not sys.flags.isolated:
        raise SystemExit("run_tests.py must be invoked through 'python3 -I'")

    root = Path(argv[0]).resolve() if argv else Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "src"))

    loader = unittest.TestLoader()
    # sortTestMethodsUsing = sorted-name ordering: independent of dict/set
    # hashing and of filesystem directory iteration order.
    loader.sortTestMethodsUsing = staticmethod(lambda a, b: (a > b) - (a < b))
    suite = loader.discover(str(root / "tests"), pattern="test_*.py")

    stream = io.StringIO()
    runner = unittest.TextTestRunner(stream=stream, verbosity=2)
    started = time.monotonic()
    result = runner.run(suite)
    duration = round(time.monotonic() - started, 6)

    def names(events: list) -> list[str]:
        return sorted(str(test) for test, _ in events)

    summary = {
        "framework": "unittest",
        "python": sys.version.split()[0],
        "tests_run": result.testsRun,
        "failures": names(result.failures),
        "errors": names(result.errors),
        "skipped": sorted(str(test) for test, _ in result.skipped),
        "successful": result.wasSuccessful(),
        "duration_seconds": duration,
        "text_output": stream.getvalue(),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
