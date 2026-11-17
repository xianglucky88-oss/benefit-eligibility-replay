"""Shared helpers for the reproducible release / verify / audit chain.

Design constraints (see release/README.md):

* standard library only, no third-party packages, no network access;
* every helper here is meant to be invoked through ``python3 -I`` so that
  ``sys.path`` cannot be poisoned by a planted module in the current working
  directory (for example a hostile ``json.py`` inside an input directory);
* all JSON documents are written canonically (sorted keys, fixed indent,
  trailing newline) so byte-level comparison is possible across machines;
* hashes use SHA-256 over bytes read from disk, never over in-memory objects.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

# Files copied into a release staging tree. Everything is listed explicitly:
# an allowlist (instead of walking the repository) keeps build outputs, local
# caches and stray files out of the reproducible source bundle.
RELEASE_FILES = (
    "pyproject.toml",
    "README.md",
    "fixtures/input.json",
    "src/benefit_eligibility_replay/__init__.py",
    "src/benefit_eligibility_replay/models.py",
    "src/benefit_eligibility_replay/parser.py",
    "src/benefit_eligibility_replay/engine.py",
    "src/benefit_eligibility_replay/report.py",
    "src/benefit_eligibility_replay/pipeline.py",
    "src/benefit_eligibility_replay/cli.py",
    "tests/test_core.py",
)

# Environment variables that may influence a Python run. Values are never
# written to the manifest verbatim: only their SHA-256 digest (or null when
# unset) is recorded, so no secrets leak into an audit artifact.
OBSERVED_ENV_VARS = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TZ",
    "PYTHONPATH",
    "PYTHONHOME",
    "PYTHONHASHSEED",
    "PYTHONIOENCODING",
    "VIRTUAL_ENV",
)

# Only these variables are forwarded to verification subprocesses. The chain
# deliberately starts each subprocess from a minimal, fixed environment.
FORWARDED_ENV_VARS = ("PATH", "LANG", "TZ")

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
CHUNK = 1024 * 1024


# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
def repo_root() -> Path:
    """Repository root: two levels above this file (release/common.py)."""
    return Path(__file__).resolve().parents[1]


def dist_root(root: Path) -> Path:
    return root / "dist"


def quarantine_root(root: Path) -> Path:
    return dist_root(root) / "quarantine"


def staging_path(root: Path, release_id: str) -> Path:
    return dist_root(root) / f"staging-{release_id}"


def release_path(root: Path, release_name: str) -> Path:
    return dist_root(root) / "releases" / release_name


def current_symlink(root: Path) -> Path:
    return dist_root(root) / "current"


def history_file(root: Path) -> Path:
    return dist_root(root) / "history.jsonl"


# --------------------------------------------------------------------------- #
# Hashing / canonical serialization
# --------------------------------------------------------------------------- #
def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def hash_tree(root: Path, relpaths: Iterable[str]) -> dict[str, str]:
    """Return {POSIX-relative path: sha256} sorted by relative path."""
    result = {}
    for rel in relpaths:
        result[rel] = sha256_file(root / rel)
    return dict(sorted(result.items()))


def canonical_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def write_canonical_json(path: Path, data: Any) -> None:
    path.write_text(canonical_json(data), encoding="utf-8")


def assert_isolated() -> None:
    """Fail loudly if a helper is ever run without ``python3 -I``."""
    if not sys.flags.isolated:
        raise SystemExit(
            "release tooling must be run with 'python3 -I' (isolated mode); "
            f"sys.flags.isolated={sys.flags.isolated}"
        )


# --------------------------------------------------------------------------- #
# Version metadata
# --------------------------------------------------------------------------- #
def read_pyproject(root: Path) -> dict[str, str]:
    """Extract version / requires-python without importing the project.

    ``tomllib`` is used on 3.11+. On older interpreters a tiny, dependency-free
    fallback parses the two scalar keys this project actually needs.
    """
    text = (root / "pyproject.toml").read_text(encoding="utf-8")
    try:
        import tomllib  # Python >= 3.11

        table = tomllib.loads(text)
        project = table.get("project", {})
        return {
            "version": str(project.get("version", "")),
            "requires_python": str(project.get("requires-python", "")),
        }
    except ModuleNotFoundError:
        values: dict[str, str] = {}
        for key in ("version", "requires-python"):
            match = re.search(rf'^\s*{key}\s*=\s*["\']([^"\']+)["\']', text, re.MULTILINE)
            if match:
                values["requires_python" if key == "requires-python" else key] = match.group(1)
        return values


def parse_python_version(spec: str) -> tuple[str, tuple[int, int]] | None:
    """Parse a single PEP 440-ish constraint such as '>=3.9'."""
    match = re.match(r"\s*(==|>=|<=|>|<)?\s*(\d+)\.(\d+)", spec)
    if not match:
        return None
    op = match.group(1) or ">="
    return op, (int(match.group(2)), int(match.group(3)))


def check_requires_python(spec: str, version_info: tuple[int, int]) -> list[str]:
    problems = []
    parsed = parse_python_version(spec)
    if parsed is None:
        problems.append(f"cannot interpret requires-python spec {spec!r}")
        return problems
    op, wanted = parsed
    if op == ">=" and not version_info >= wanted:
        problems.append(f"python {version_info[0]}.{version_info[1]} does not satisfy {spec}")
    elif op == ">" and not version_info > wanted:
        problems.append(f"python {version_info[0]}.{version_info[1]} does not satisfy {spec}")
    elif op == "==" and not version_info == wanted:
        problems.append(f"python {version_info[0]}.{version_info[1]} does not satisfy {spec}")
    elif op == "<=" and not version_info <= wanted:
        problems.append(f"python {version_info[0]}.{version_info[1]} does not satisfy {spec}")
    elif op == "<" and not version_info < wanted:
        problems.append(f"python {version_info[0]}.{version_info[1]} does not satisfy {spec}")
    return problems


# --------------------------------------------------------------------------- #
# Environment snapshots
# --------------------------------------------------------------------------- #
def hash_env(value: str | None) -> str | None:
    if value is None:
        return None
    return sha256_bytes(value.encode("utf-8"))


def snapshot_parent_env() -> dict[str, str | None]:
    """Hashed view of the environment that *invoked* the release chain."""
    return {
        name: hash_env(os.environ.get(name))
        for name in OBSERVED_ENV_VARS
    }


def minimal_child_env() -> dict[str, str]:
    """The fixed, minimal environment given to every verification subprocess."""
    env = {name: os.environ[name] for name in FORWARDED_ENV_VARS if name in os.environ}
    # Explicit and stable, even under -I where PYTHON* vars are ignored.
    env["PYTHONHASHSEED"] = "0"
    return dict(sorted(env.items()))


def python_probe() -> dict[str, Any]:
    """Interpreter facts recorded in the manifest (no machine names/paths)."""
    return {
        "version": sys.version,
        "version_info": list(sys.version_info[:3]),
        "minor": f"{sys.version_info.major}.{sys.version_info.minor}",
        "implementation": platform.python_implementation(),
        "byteorder": sys.byteorder,
        "platform": sys.platform,
        "stdlib_platform_machine": platform.machine(),
        "isolated_mode": bool(sys.flags.isolated),
        # Hash of the interpreter binary itself, so an auditor can tell whether
        # a different CPython build produced the artifact.
        "interpreter_sha256": sha256_file(Path(sys.executable)) if sys.executable else None,
    }


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def monotonic_stamp() -> str:
    return time.strftime("%Y%m%d%H%M%S", time.gmtime())
