"""Deterministic CLI runner used by every verification step.

Invocation (always with isolated mode)::

    python3 -I release/run_cli.py            # default bundled fixture
    python3 -I release/run_cli.py INPUT.json # explicit input path
    python3 -I release/run_cli.py --probe    # print interpreter/environment facts
    python3 -I release/run_cli.py --print-path INPUT.json

The runner resolves the tree root relative to this file instead of the current
working directory, inserts ``<root>/src`` into ``sys.path`` itself (``-I``
ignores PYTHONPATH) and refuses to run unless isolated mode is active. This
makes results independent of the caller's CWD and immune to planted modules
such as a hostile ``json.py`` next to an input file.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"


def _assert_isolated() -> None:
    if not sys.flags.isolated:
        raise SystemExit("run_cli.py must be invoked through 'python3 -I'")


def _pyproject_versions() -> dict:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    try:
        import tomllib

        project = tomllib.loads(text).get("project", {})
        return {
            "pyproject_version": str(project.get("version")),
            "requires_python": str(project.get("requires-python")),
        }
    except ModuleNotFoundError:  # pragma: no cover - Python 3.9/3.10 path
        import re

        found = {}
        match = re.search(r'^\s*version\s*=\s*["\']([^"\']+)["\']', text, re.MULTILINE)
        if match:
            found["pyproject_version"] = match.group(1)
        match = re.search(r'^\s*requires-python\s*=\s*["\']([^"\']+)["\']', text, re.MULTILINE)
        if match:
            found["requires_python"] = match.group(1)
        return found


def _probe() -> dict:
    import platform

    import benefit_eligibility_replay

    data = {
        "cwd": os.getcwd(),
        "tz_env": os.environ.get("TZ"),
        "timezone_name": _tz_name(),
        "utc_offset_seconds": _utc_offset(),
        "python": sys.version.split()[0],
        "python_minor": f"{sys.version_info.major}.{sys.version_info.minor}",
        "implementation": platform.python_implementation(),
        "isolated_mode": bool(sys.flags.isolated),
        "hashseed_env": os.environ.get("PYTHONHASHSEED"),
        "sys_path_head": sys.path[0],
        "src_on_sys_path": str(SRC) in sys.path,
        "default_fixture": str(ROOT / "fixtures" / "input.json"),
        "package_version": benefit_eligibility_replay.__version__,
    }
    data.update(_pyproject_versions())
    return data


def _tz_name() -> str | None:
    try:
        from datetime import datetime

        return datetime.now().astimezone().tzname()
    except Exception:  # pragma: no cover - defensive, environments differ
        return None


def _utc_offset() -> int | None:
    try:
        from datetime import datetime

        offset = datetime.now().astimezone().utcoffset()
        return None if offset is None else int(offset.total_seconds())
    except Exception:  # pragma: no cover
        return None


def main(argv: list[str]) -> int:
    _assert_isolated()
    # Insert explicitly: -I removes PYTHONPATH and the script directory only
    # contributes the directory of THIS runner, never the input's directory.
    sys.path.insert(0, str(SRC))

    if argv and argv[0] == "--probe":
        print(json.dumps(_probe(), ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    print_path = False
    if argv and argv[0] == "--print-path":
        print_path = True
        argv = argv[1:]

    input_arg = argv[0] if argv else str(ROOT / "fixtures" / "input.json")
    input_path = Path(input_arg).resolve()

    from benefit_eligibility_replay.cli import main as cli_main  # noqa: E402

    if print_path:
        print(str(input_path))
        return 0

    # Pass arguments explicitly instead of mutating sys.argv.
    cli_main([str(input_path)])
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
