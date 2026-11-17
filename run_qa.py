#!/usr/bin/env python3
"""Convenience entry point for the independent quality-assurance layer.

Runs the full QA + legacy suite under isolated Python (``-I`` safe) without
requiring any installed package or environment variable:

    python3 run_qa.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from qa_tests.run_all import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
