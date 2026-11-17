"""Import-path bootstrap so the QA layer works with a plain stdlib runner.

The production code lives under ``src/`` (no installed package) and the QA
packages live at the repository root.  Test modules import this module first,
which makes both importable without setting ``PYTHONPATH`` manually.  The
repository root is derived from this file's location, so the bootstrap works
under ``python -I`` (isolated mode ignores ``PYTHONPATH``) regardless of the
caller's working directory.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

for path in (str(ROOT), str(SRC)):
    if path not in sys.path:
        sys.path.insert(0, path)
