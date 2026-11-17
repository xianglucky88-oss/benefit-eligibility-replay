"""Reproducible packaging / verification / release orchestration.

Subcommands
-----------
build       stage -> verify -> promote into dist/releases, atomic symlink swap;
            any failure isolates the partial tree under dist/quarantine
verify      run the full verification matrix against a tree (default: current)
rollback    point dist/current at a previous complete release
audit       offline re-verification of an unpacked release directory
list        list complete releases known to the history log
history     append-only JSONL event log (builds, rollbacks, quarantines)

Everything runs under ``python3 -I`` with the standard library only. See
release/README.md for the threat model and the offline auditor workflow.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    EPOCH,
    RELEASE_FILES,
    assert_isolated,
    canonical_json,
    check_requires_python,
    current_symlink,
    dist_root,
    hash_tree,
    history_file,
    minimal_child_env,
    monotonic_stamp,
    parse_python_version,
    python_probe,
    quarantine_root,
    read_pyproject,
    release_path,
    repo_root,
    sha256_bytes,
    sha256_file,
    snapshot_parent_env,
    staging_path,
    utc_now,
    write_canonical_json,
)

TOOLING_FILES = (
    "release/common.py",
    "release/run_cli.py",
    "release/run_tests.py",
    "release/releng.py",
)
MANIFEST_NAME = "manifest.json"
REBUILD_NAME = "REBUILD.md"
SCHEMA_VERSION = 1

# A second, fully synthetic input used for the explicit-input check. It lives
# only in the release tree (inputs/), never next to the package source, and is
# plain arithmetic so the expected result is derived independently here.
EXPLICIT_INPUT = {
    "applications": [
        {"case": "X1", "household": 1, "income": 500, "at": "2026-03-01"},
        {"case": "X2", "household": 3, "income": 4000, "at": "2026-03-01"},
    ],
    "policy": {"income_limit_per_person": 1000, "benefit": 250},
    "changes": [{"case": "X2", "income": 2500, "at": "2026-04-01"}],
}


# --------------------------------------------------------------------------- #
# Small records
# --------------------------------------------------------------------------- #
@dataclass
class CheckResult:
    id: str
    title: str
    passed: bool
    details: dict[str, Any] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "passed": self.passed,
            "problems": self.problems,
            "details": self.details,
        }


class BuildFailure(Exception):
    def __init__(self, message: str, checks: list[CheckResult] | None = None):
        super().__init__(message)
        self.checks = checks or []


# --------------------------------------------------------------------------- #
# Logging / quarantine
# --------------------------------------------------------------------------- #
def log_event(root: Path, event: str, **fields: Any) -> None:
    entry = {"at": utc_now(), "event": event, **fields}
    history_file(root).parent.mkdir(parents=True, exist_ok=True)
    with history_file(root).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, sort_keys=True) + "\n")


def quarantine(root: Path, target: Path, reason: str, stamp: str | None = None) -> Path:
    """Move a partial/foreign artifact into the quarantine area."""
    qroot = quarantine_root(root)
    qroot.mkdir(parents=True, exist_ok=True)
    label = stamp or f"{monotonic_stamp()}-{sha256_bytes(reason.encode())[:8]}"
    destination = qroot / f"{label}-{target.name}"
    counter = 1
    while destination.exists():
        destination = qroot / f"{label}-{counter}-{target.name}"
        counter += 1
    shutil.move(str(target), str(destination))
    note = destination.parent / f"{destination.name}.reason.txt"
    note.write_text(f"{utc_now()} {reason}\n", encoding="utf-8")
    log_event(root, "quarantine", target=str(target), moved_to=str(destination), reason=reason)
    return destination


def reset_mtimes(tree: Path) -> None:
    """Normalize every mtime to the UNIX epoch for byte-stable archives."""
    epoch_ts = EPOCH.timestamp()
    os.utime(tree, (epoch_ts, epoch_ts))
    for path in tree.rglob("*"):
        os.utime(path, (epoch_ts, epoch_ts))


def strip_bytecode(tree: Path) -> None:
    """Remove every __pycache__ and .pyc generated during verification.

    Bytecode embeds the interpreter minor version and absolute paths, so
    shipping it would make release directories machine-specific. The source
    allow-list hash is recomputed after this cleanup.
    """
    for cache in tree.rglob("__pycache__"):
        if cache.is_dir():
            shutil.rmtree(cache)
    for pyc in tree.rglob("*.py[cod]"):
        pyc.unlink()


# --------------------------------------------------------------------------- #
# Subprocess execution (fixed minimal env, isolated mode, temp CWD)
# --------------------------------------------------------------------------- #
def run_isolated(
    root: Path,
    runner: str,
    args: list[str],
    cwd: Path,
    extra_env: dict[str, str] | None = None,
    timeout: int = 120,
) -> dict[str, Any]:
    env = minimal_child_env()
    if extra_env:
        env.update(extra_env)
    cmd = [sys.executable, "-I", str(root / runner), *args]
    proc = subprocess.run(
        cmd,
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return {
        "cmd": [Path(cmd[0]).name, "-I", runner, *args],
        "cwd": str(cwd),
        "env": dict(sorted(env.items())),
        "returncode": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
    }


# --------------------------------------------------------------------------- #
# Preflight: interpreter, metadata, workspace hygiene, stale artifacts
# --------------------------------------------------------------------------- #
def preflight(root: Path) -> tuple[list[str], list[str]]:
    """Return (hard problems, informational notes).

    Hard problems fail the build; notes are recorded in the manifest for the
    auditor but do not block the release.
    """
    problems: list[str] = []
    notes: list[str] = []
    meta = read_pyproject(root)
    spec = meta.get("requires_python", "")
    current = (sys.version_info.major, sys.version_info.minor)
    problems.extend(check_requires_python(spec, current))
    parsed = parse_python_version(spec)
    if parsed and current != parsed[1]:
        # Not a failure: the declared floor is a minimum. The exact minor used
        # is recorded in the manifest so an auditor can replay with the same
        # one; byte-level output comparison catches any divergence anyway.
        notes.append(
            f"interpreter minor {current[0]}.{current[1]} differs from the "
            f"declared floor {spec}; exact build interpreter is recorded in the manifest"
        )

    missing = [rel for rel in RELEASE_FILES if not (root / rel).is_file()]
    if missing:
        problems.append(f"missing required source files: {sorted(missing)}")
    missing_tools = [rel for rel in TOOLING_FILES if not (root / rel).is_file()]
    if missing_tools:
        problems.append(f"missing release tooling files: {sorted(missing_tools)}")

    dist = dist_root(root)
    dist.mkdir(parents=True, exist_ok=True)
    managed = {"releases", "quarantine"}
    stamp = monotonic_stamp()
    for entry in sorted(dist.iterdir()):
        if entry.name in managed or entry.name == "history.jsonl":
            continue
        if entry.is_symlink() and entry.name == "current":
            if not entry.exists():
                notes.append("dist/current was a dangling symlink; it will be replaced on build")
            continue
        if entry.name.startswith("staging-"):
            quarantine(root, entry, "stale staging directory from an interrupted build", stamp)
            notes.append(f"stale staging tree quarantined: {entry.name}")
        else:
            quarantine(root, entry, "unknown pre-existing artifact in dist/", stamp)
            notes.append(f"unknown dist artifact quarantined: {entry.name}")

    releases_dir = dist / "releases"
    if releases_dir.is_dir():
        for entry in sorted(releases_dir.iterdir()):
            if entry.is_dir() and not (entry / MANIFEST_NAME).is_file():
                quarantine(root, entry, "release directory without manifest (incomplete)", stamp)
                notes.append(f"incomplete release quarantined: {entry.name}")

    sym = current_symlink(root)
    if sym.is_symlink():
        target = os.readlink(sym)
        resolved = (sym.parent / target).resolve()
        if not str(resolved).startswith(str((dist / "releases").resolve())):
            quarantine(root, sym, "dist/current points outside dist/releases", stamp)
            notes.append("suspicious dist/current target quarantined")
    return problems, notes


# --------------------------------------------------------------------------- #
# Verification matrix
# --------------------------------------------------------------------------- #
def _make_working_dirs(root: Path) -> tuple[Path, Path, Path]:
    # The work area lives OUTSIDE the tree under verification so it can never
    # be copied into a release or influence source-tree hashes.
    work = Path(tempfile.mkdtemp(prefix="releng-verify-"))
    cwd_a = work / "cwd-a"
    cwd_b = work / "cwd-b"
    input_dir = work / "explicit-input"
    for directory in (cwd_a, cwd_b, input_dir):
        directory.mkdir(parents=True, exist_ok=True)
    # Planted module: if any verification step imported from the CWD or from the
    # input directory, this file would hijack the stdlib 'json' module.
    (cwd_a / "json.py").write_text(
        "raise RuntimeError('planted json.py imported from working directory')\n",
        encoding="utf-8",
    )
    (input_dir / "json.py").write_text(
        "raise RuntimeError('planted json.py imported from input directory')\n",
        encoding="utf-8",
    )
    input_path = input_dir / "explicit-input.json"
    write_canonical_json(input_path, EXPLICIT_INPUT)
    return cwd_a, cwd_b, input_path


def _check_module_import(root: Path, cwd: Path) -> CheckResult:
    # The -I interpreter excludes the CWD from sys.path, so the planted
    # json.py next to this process cannot shadow the stdlib. src is inserted
    # explicitly inside the snippet, the same way the runners do it.
    code = (
        "import sys, json;"
        f"sys.path.insert(0, {str(root / 'src')!r});"
        "import benefit_eligibility_replay as m;"
        "from pathlib import Path;"
        "print(json.dumps({"
        "'version': m.__version__,"
        " 'isolated': bool(sys.flags.isolated),"
        " 'stdlib_json': json.__file__,"
        " 'cwd_json_exists': Path('json.py').exists()}, sort_keys=True))"
    )
    env = minimal_child_env()
    cmd = [sys.executable, "-I", "-c", code]
    completed = subprocess.run(
        cmd, cwd=str(cwd), env=env, capture_output=True, text=True, timeout=120
    )
    problems = []
    details: dict[str, Any] = {
        "returncode": completed.returncode,
        "stdout": completed.stdout.strip(),
        "stderr": completed.stderr,
    }
    if completed.returncode != 0:
        problems.append("isolated module import failed")
    else:
        payload = json.loads(completed.stdout)
        if not payload.get("isolated"):
            problems.append("subprocess was not running in isolated mode")
        if not payload.get("cwd_json_exists"):
            problems.append("test setup error: planted json.py missing from CWD")
        stdlib_json = str(payload.get("stdlib_json", ""))
        if stdlib_json and "json.py" in stdlib_json and "lib" not in stdlib_json:
            problems.append("stdlib json was shadowed by a planted module")
    return CheckResult("module-import", "module import under -I with planted json.py in CWD",
                       not problems, details, problems)


def _check_probe(root: Path, cwd: Path, meta: dict[str, str]) -> CheckResult:
    proc = run_isolated(root, "release/run_cli.py", ["--probe"], cwd)
    problems = []
    details = {"returncode": proc["returncode"], "stdout": proc["stdout"], "stderr": proc["stderr"]}
    if proc["returncode"] != 0:
        problems.append("probe invocation failed")
        return CheckResult("interpreter-probe", "interpreter/environment probe", False, details, problems)
    probe = json.loads(proc["stdout"])
    if not probe.get("isolated_mode"):
        problems.append("probe reports isolated_mode is false")
    if probe.get("package_version") != meta.get("version"):
        problems.append(
            f"package version {probe.get('package_version')!r} != pyproject version {meta.get('version')!r}"
        )
    if probe.get("pyproject_version") != meta.get("version"):
        problems.append("pyproject version mismatch inside probe")
    if probe.get("requires_python") != meta.get("requires_python"):
        problems.append("requires-python mismatch inside probe")
    if not probe.get("src_on_sys_path"):
        problems.append("runner did not place src/ on sys.path")
    details["parsed"] = probe
    return CheckResult("interpreter-probe", "version metadata and interpreter probe",
                       not problems, details, problems)


def _check_default_fixture(root: Path, cwd: Path) -> tuple[CheckResult, str]:
    proc = run_isolated(root, "release/run_cli.py", [], cwd)
    problems = []
    details = {"returncode": proc["returncode"], "stderr": proc["stderr"]}
    if proc["returncode"] != 0:
        problems.append("CLI default-fixture run failed")
        return CheckResult("cli-default-fixture", "CLI default fixture", False, details, problems), ""
    stdout = proc["stdout"]
    # Independent in-process expectation, computed from the pipeline itself but
    # re-serialized through the same canonical encoder.
    sys.path.insert(0, str(root / "src"))
    try:
        from benefit_eligibility_replay.pipeline import run as pipeline_run
        payload = json.loads((root / "fixtures" / "input.json").read_text(encoding="utf-8"))
        expected = canonical_json(pipeline_run(payload))
    finally:
        sys.path.remove(str(root / "src"))
    if stdout != expected:
        problems.append("CLI default output differs from in-process pipeline result")
    details["output_sha256"] = sha256_bytes(stdout.encode())
    details["byte_length"] = len(stdout.encode())
    return (
        CheckResult("cli-default-fixture", "CLI default fixture vs pipeline", not problems, details, problems),
        stdout,
    )


def _check_explicit_input(root: Path, cwd: Path, input_path: Path) -> CheckResult:
    proc = run_isolated(root, "release/run_cli.py", [str(input_path)], cwd)
    problems = []
    details: dict[str, Any] = {
        "returncode": proc["returncode"],
        "input_sha256": sha256_file(input_path),
        "stderr": proc["stderr"],
    }
    if proc["returncode"] != 0:
        problems.append("CLI explicit-input run failed")
        return CheckResult("cli-explicit-input", "CLI explicit input", False, details, problems)
    result = json.loads(proc["stdout"])
    decisions = {d["case"]: d for d in result["decisions"]}
    expected = {
        "X1": {"eligible": True, "income": 500, "limit": 1000, "benefit": 250,
               "evidence": ["application"]},
        "X2": {"eligible": True, "income": 2500, "limit": 3000, "benefit": 250,
               "evidence": ["application", "change"]},
    }
    for case, want in expected.items():
        got = decisions.get(case)
        if got is None:
            problems.append(f"missing decision for {case}")
            continue
        for key, value in want.items():
            if got.get(key) != value:
                problems.append(f"{case}.{key}: expected {value!r}, got {got.get(key)!r}")
    if result.get("eligible_count") != 2:
        problems.append(f"eligible_count expected 2, got {result.get('eligible_count')!r}")
    details["decisions"] = result.get("decisions")
    return CheckResult("cli-explicit-input", "CLI explicit input with independent expectations",
                       not problems, details, problems)


def _check_canonical_json(text: str) -> CheckResult:
    problems = []
    details: dict[str, Any] = {"output_sha256": sha256_bytes(text.encode())}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        problems.append(f"output is not valid JSON: {exc}")
        return CheckResult("json-canonical", "canonical sorted JSON output", False, details, problems)
    reencoded = json.dumps(parsed, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if reencoded != text:
        problems.append("output is not canonical (sorted keys, 2-space indent, trailing newline)")

    def keys_sorted(node: Any) -> bool:
        if isinstance(node, dict):
            keys = list(node.keys())
            if keys != sorted(keys):
                return False
            return all(keys_sorted(value) for value in node.values())
        if isinstance(node, list):
            return all(keys_sorted(item) for item in node)
        return True

    if not keys_sorted(parsed):
        problems.append("nested object keys are not sorted")
    return CheckResult("json-canonical", "canonical sorted JSON output", not problems, details, problems)


def _check_determinism(root: Path, cwd_a: Path, cwd_b: Path, baseline: str) -> CheckResult:
    problems = []
    runs = []
    variants = (
        (cwd_a, {"PYTHONHASHSEED": "0"}),
        (cwd_b, {"PYTHONHASHSEED": "random"}),
        (cwd_b, {}),
    )
    for cwd, extra in variants:
        proc = run_isolated(root, "release/run_cli.py", [], cwd, extra_env=extra)
        runs.append({"cwd": str(cwd), "extra_env": extra, "returncode": proc["returncode"],
                     "stdout_sha256": sha256_bytes(proc["stdout"].encode())})
        if proc["returncode"] != 0:
            problems.append(f"rerun in {cwd.name} failed")
        elif proc["stdout"] != baseline:
            problems.append(f"rerun in {cwd.name} with env {extra} produced different output")
    return CheckResult("determinism", "byte-identical reruns across CWD and hash seed",
                       not problems, {"runs": runs}, problems)


def _check_timezone(root: Path, cwd: Path, baseline: str) -> CheckResult:
    problems = []
    details: dict[str, Any] = {}
    for tz in ("UTC", "America/Los_Angeles", "Asia/Kolkata"):
        proc = run_isolated(root, "release/run_cli.py", [], cwd, extra_env={"TZ": tz})
        probe = run_isolated(root, "release/run_cli.py", ["--probe"], cwd, extra_env={"TZ": tz})
        parsed_probe = json.loads(probe["stdout"]) if probe["returncode"] == 0 else {}
        details[tz] = {
            "returncode": proc["returncode"],
            "observed_offset": parsed_probe.get("utc_offset_seconds"),
            "stdout_sha256": sha256_bytes(proc["stdout"].encode()),
        }
        if proc["returncode"] != 0 or proc["stdout"] != baseline:
            problems.append(f"output changed or failed under TZ={tz}")
    return CheckResult("timezone-invariance", "identical output across system time zones",
                       not problems, details, problems)


def _check_unittest(root: Path, cwd: Path) -> tuple[CheckResult, dict[str, Any]]:
    proc = run_isolated(root, "release/run_tests.py", [str(root)], cwd)
    problems = []
    details = {"returncode": proc["returncode"], "stderr": proc["stderr"][-2000:]}
    summary: dict[str, Any] = {}
    if proc["returncode"] != 0:
        problems.append("unittest suite failed")
        details["stdout_tail"] = proc["stdout"][-2000:]
    else:
        summary = json.loads(proc["stdout"])
        if not summary.get("successful"):
            problems.append("test summary reports failure")
        if summary.get("failures") or summary.get("errors"):
            problems.append("test summary contains failures/errors")
        if int(summary.get("tests_run", 0)) == 0:
            problems.append("no tests were executed")
    details["summary"] = {k: v for k, v in summary.items() if k != "text_output"}
    return CheckResult("unittest-suite", "bundled unittest suite and stable summary",
                       not problems, details, problems), summary


def verify_tree(root: Path) -> tuple[list[CheckResult], dict[str, Any]]:
    """Run the full verification matrix against an arbitrary tree root."""
    meta = read_pyproject(root)
    cwd_a, cwd_b, input_path = _make_working_dirs(root)
    try:
        checks, extras = _verify_tree_inner(root, meta, cwd_a, cwd_b, input_path)
    finally:
        shutil.rmtree(cwd_a.parent, ignore_errors=True)
    # Subprocesses leave bytecode inside the tree; remove it so neither a
    # staged release nor an audited tree ever carries interpreter-specific
    # artifacts, then refresh the recorded hashes from the clean tree.
    strip_bytecode(root)
    extras["source_hashes"] = hash_tree(root, RELEASE_FILES)
    extras["tooling_hashes"] = hash_tree(root, TOOLING_FILES)
    return checks, extras


def _verify_tree_inner(root: Path, meta: dict[str, str], cwd_a: Path,
                       cwd_b: Path, input_path: Path) -> tuple[list[CheckResult], dict[str, Any]]:
    checks: list[CheckResult] = []

    checks.append(_check_module_import(root, cwd_a))
    checks.append(_check_probe(root, cwd_a, meta))

    fixture_check, baseline = _check_default_fixture(root, cwd_a)
    checks.append(fixture_check)
    checks.append(_check_explicit_input(root, cwd_b, input_path))
    if baseline:
        checks.append(_check_canonical_json(baseline))
        checks.append(_check_determinism(root, cwd_a, cwd_b, baseline))
        checks.append(_check_timezone(root, cwd_a, baseline))

    test_check, summary = _check_unittest(root, cwd_a)
    checks.append(test_check)

    source_hashes = hash_tree(root, RELEASE_FILES)
    tooling_hashes = hash_tree(root, TOOLING_FILES)
    hash_check = CheckResult(
        "source-hashes",
        "record source and tooling hashes (recomputed by audit)",
        True,
        {"source_files": source_hashes, "tooling_files": tooling_hashes},
    )
    checks.append(hash_check)

    explicit = {
        "path": "inputs/explicit-input.json",
        "sha256": sha256_file(input_path),
        "payload": EXPLICIT_INPUT,
    }
    extras = {
        "test_summary": {k: v for k, v in summary.items() if k != "text_output"},
        "test_summary_text": summary.get("text_output", ""),
        "explicit_input": explicit,
        "source_hashes": source_hashes,
        "tooling_hashes": tooling_hashes,
        "default_output_sha256": sha256_bytes(baseline.encode()) if baseline else None,
        "default_output": baseline,
    }
    return checks, extras


# --------------------------------------------------------------------------- #
# REBUILD.md
# --------------------------------------------------------------------------- #
REBUILD_TEMPLATE = """# Rebuild and audit instructions (offline)

This directory is a complete, self-contained release of
benefit-eligibility-replay {version}.

It requires only a Python {requires_python} interpreter (standard library
only). No network access, third-party package, credential or policy database
is involved.

## 1. Re-run the deterministic CLI on the bundled fixture

    python3 -I release/run_cli.py

The isolated flag (`-I`) ignores PYTHONPATH/PYTHONHOME and user site-packages,
so a planted `json.py` in the current directory cannot hijack imports.

## 2. Re-run with the bundled explicit input

    python3 -I release/run_cli.py inputs/explicit-input.json

## 3. Re-run the test suite (stable JSON summary)

    python3 -I release/run_tests.py "$(pwd)"

## 4. Verify every check and hash against the manifest

From a copy of the source repository (or directly against this directory):

    python3 -I release/releng.py audit --tree .

A successful audit prints `AUDIT PASSED` and exits 0. It recomputes all source
and tooling SHA-256 hashes, re-runs the CLI under three time zones and two
working directories, validates canonical sorted JSON, re-runs the test suite
and compares every result against `manifest.json`.

## 5. Inspect the manifest

`manifest.json` is itself canonical JSON (sorted keys). It records:

- exact interpreter version, implementation and interpreter-binary hash used
  when the release was built;
- hashed (never raw) parent-process environment variables and the fixed minimal
  environment handed to every subprocess;
- SHA-256 of every source and tooling file, of the fixture output and of the
  explicit synthetic input;
- the full verification matrix results and the unittest summary;
- the release sequence, source-tree hash and parent release for rollback.

Rollback (from the repository):

    python3 -I release/releng.py list
    python3 -I release/releng.py rollback --to <release-name>
"""


def write_rebuild_doc(tree: Path, meta: dict[str, str]) -> None:
    (tree / REBUILD_NAME).write_text(
        REBUILD_TEMPLATE.format(
            version=meta.get("version", ""),
            requires_python=meta.get("requires_python", ""),
        ),
        encoding="utf-8",
    )


# --------------------------------------------------------------------------- #
# Build
# --------------------------------------------------------------------------- #
def cmd_build(args: argparse.Namespace) -> int:
    root = repo_root()
    assert_isolated()
    stamp = monotonic_stamp()
    preflight_problems, preflight_notes = preflight(root)
    for note in preflight_notes:
        print(f"preflight note: {note}", file=sys.stderr)
    if preflight_problems:
        for problem in preflight_problems:
            print(f"preflight error: {problem}", file=sys.stderr)
        log_event(root, "build", status="failure", stage="preflight",
                  error="; ".join(preflight_problems))
        return 2

    meta = read_pyproject(root)
    source_hashes = hash_tree(root, RELEASE_FILES)
    tooling_hashes = hash_tree(root, TOOLING_FILES)
    tree_hash = sha256_bytes(
        canonical_json({"files": source_hashes}).encode("utf-8")
    )
    sequence = next_sequence(root)
    release_name = f"{sequence:04d}-{meta['version']}-{tree_hash[:12]}"
    release_id = f"{stamp}-{tree_hash[:12]}"
    staging = staging_path(root, release_id)

    if staging.exists():
        quarantine(root, staging, "staging path already existed before build", stamp)
    staging.mkdir(parents=True)

    try:
        for rel in RELEASE_FILES:
            destination = staging / rel
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(root / rel, destination)
        for rel in TOOLING_FILES:
            destination = staging / rel
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(root / rel, destination)
        (staging / "inputs").mkdir()
        write_canonical_json(staging / "inputs" / "explicit-input.json", EXPLICIT_INPUT)
        write_rebuild_doc(staging, meta)

        if args.inject_failure:
            # Deliberately break the copied suite inside the STAGING tree only,
            # so that quarantine handling can be exercised without touching the
            # real source tree.
            (staging / "tests" / "test_injected_failure.py").write_text(
                "import unittest\n"
                "class InjectedFailure(unittest.TestCase):\n"
                "    def test_injected(self):\n"
                "        self.fail('injected build failure')\n",
                encoding="utf-8",
            )

        reset_mtimes(staging)
        checks, extras = verify_tree(staging)
        # verify_tree already strips generated bytecode and refreshes hashes.
        passed = all(check.passed for check in checks)
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "release": {
                "name": release_name,
                "id": release_id,
                "sequence": sequence,
                "version": meta["version"],
                "requires_python": meta["requires_python"],
                "built_at_utc": utc_now(),
                "source_tree_sha256": tree_hash,
                "parent_release": current_target_name(root),
                "inject_failure": bool(args.inject_failure),
            },
            "interpreter": python_probe(),
            "environment": {
                "observed_parent_env_hashed": snapshot_parent_env(),
                "fixed_subprocess_env": minimal_child_env(),
            },
            "files": {
                "source": extras["source_hashes"],
                "tooling": extras["tooling_hashes"],
                "explicit_input": {
                    "path": extras["explicit_input"]["path"],
                    "sha256": extras["explicit_input"]["sha256"],
                },
                "default_output_sha256": extras["default_output_sha256"],
            },
            "verification": {
                "all_passed": passed,
                "checks": [check.as_dict() for check in checks],
            },
            "test_summary": extras["test_summary"],
            "test_summary_text": extras["test_summary_text"],
            "fixture_output": extras["default_output"],
            "explicit_input_payload": extras["explicit_input"]["payload"],
            "preflight_warnings": preflight_notes,
            "preflight_errors": preflight_problems,
        }
        write_canonical_json(staging / MANIFEST_NAME, manifest)
        strip_bytecode(staging)
        residue = [str(p.relative_to(staging))
                   for p in staging.rglob("*")
                   if p.name == "__pycache__" or p.suffix in (".pyc", ".pyo")]
        if residue:
            raise BuildFailure(f"bytecode artifacts present in release tree: {residue}")
        reset_mtimes(staging)

        if not passed or args.inject_failure:
            failed = [check.id for check in checks if not check.passed]
            raise BuildFailure(
                f"verification failed ({failed or 'injected failure'})", checks
            )

        destination = release_path(root, release_name)
        if destination.exists():
            quarantine(root, destination, "release name collision during promotion", stamp)
        (destination.parent).mkdir(parents=True, exist_ok=True)
        shutil.move(str(staging), str(destination))
        # Re-normalize after the move so the promoted tree (including the
        # manifest) has uniform epoch mtimes for stable archive byte output.
        reset_mtimes(destination)
        swap_current_symlink(root, destination)
        log_event(
            root, "build", status="success", release=release_name,
            sequence=sequence, source_tree_sha256=tree_hash,
        )
        print(f"BUILD OK: {destination}")
        print(f"current -> {os.readlink(current_symlink(root))}")
        return 0

    except (BuildFailure, Exception) as exc:  # noqa: BLE001 - top-level guard
        if staging.exists():
            qpath = quarantine(
                root, staging,
                f"failed build: {exc} ({type(exc).__name__})", stamp,
            )
            print(f"BUILD FAILED; partial artifact isolated at {qpath}", file=sys.stderr)
        log_event(
            root, "build", status="failure",
            release=release_name, error=f"{type(exc).__name__}: {exc}",
            checks=[c.as_dict() for c in getattr(exc, "checks", [])],
        )
        return 1


def next_sequence(root: Path) -> int:
    releases = dist_root(root) / "releases"
    if not releases.is_dir():
        return 1
    highest = 0
    for entry in releases.iterdir():
        prefix = entry.name.split("-", 1)[0]
        if prefix.isdigit():
            highest = max(highest, int(prefix))
    return highest + 1


def current_target_name(root: Path) -> str | None:
    sym = current_symlink(root)
    if sym.is_symlink() and sym.exists():
        return Path(os.readlink(sym)).name
    return None


def swap_current_symlink(root: Path, destination: Path) -> None:
    sym = current_symlink(root)
    tmp = sym.parent / f".current.tmp.{monotonic_stamp()}"
    if tmp.exists() or tmp.is_symlink():
        tmp.unlink()
    os.symlink(os.path.relpath(destination, sym.parent), tmp)
    os.replace(tmp, sym)  # atomic on POSIX and modern Windows


# --------------------------------------------------------------------------- #
# Verify command
# --------------------------------------------------------------------------- #
def cmd_verify(args: argparse.Namespace) -> int:
    root = Path(args.tree).resolve() if args.tree else repo_root()
    assert_isolated()
    checks, _ = verify_tree(root)
    for check in checks:
        status = "PASS" if check.passed else "FAIL"
        print(f"[{status}] {check.id}: {check.title}")
        for problem in check.problems:
            print(f"    - {problem}")
    passed = all(check.passed for check in checks)
    print("VERIFY PASSED" if passed else "VERIFY FAILED")
    return 0 if passed else 1


# --------------------------------------------------------------------------- #
# Rollback
# --------------------------------------------------------------------------- #
def cmd_rollback(args: argparse.Namespace) -> int:
    root = repo_root()
    assert_isolated()
    releases_dir = dist_root(root) / "releases"
    if not releases_dir.is_dir():
        print("no releases directory; nothing to roll back", file=sys.stderr)
        return 2

    if args.to:
        target = releases_dir / args.to
        if not target.is_dir() or not (target / MANIFEST_NAME).is_file():
            print(f"unknown or incomplete release: {args.to}", file=sys.stderr)
            return 2
    else:
        complete = sorted(
            (p for p in releases_dir.iterdir() if (p / MANIFEST_NAME).is_file()),
            key=lambda p: p.name,
            reverse=True,
        )
        current = current_target_name(root)
        candidates = [p for p in complete if p.name != current]
        if not candidates:
            print("no previous complete release available", file=sys.stderr)
            return 2
        target = candidates[0]

    previous = current_target_name(root)
    swap_current_symlink(root, target)
    log_event(root, "rollback", previous=previous, target=target.name)
    print(f"ROLLBACK OK: current {previous or '-'} -> {target.name}")
    return 0


# --------------------------------------------------------------------------- #
# Audit (offline re-verification of an unpacked release directory)
# --------------------------------------------------------------------------- #
def cmd_audit(args: argparse.Namespace) -> int:
    tree = Path(args.tree).resolve()
    assert_isolated()
    manifest_path = tree / MANIFEST_NAME
    if not manifest_path.is_file():
        print(f"manifest missing: {manifest_path}", file=sys.stderr)
        return 2
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    checks: list[CheckResult] = []

    # 1. manifest itself must be canonical JSON
    raw = manifest_path.read_text(encoding="utf-8")
    checks.append(CheckResult(
        "audit-manifest-canonical", "manifest is canonical sorted JSON",
        raw == canonical_json(manifest),
        {"sha256": sha256_bytes(raw.encode())},
        [] if raw == canonical_json(manifest) else ["manifest re-encoding differs"],
    ))

    # 2. file hashes
    source_now = hash_tree(tree, RELEASE_FILES)
    tooling_now = hash_tree(tree, TOOLING_FILES)
    problems = []
    if source_now != manifest["files"]["source"]:
        problems.append("source file hashes do not match manifest")
    if tooling_now != manifest["files"]["tooling"]:
        problems.append("tooling file hashes do not match manifest")
    explicit = tree / manifest["files"]["explicit_input"]["path"]
    explicit_hash = sha256_file(explicit) if explicit.is_file() else None
    if explicit_hash != manifest["files"]["explicit_input"]["sha256"]:
        problems.append("explicit input hash does not match manifest")
    checks.append(CheckResult("audit-file-hashes", "all file hashes match manifest",
                              not problems,
                              {"source": source_now, "tooling": tooling_now,
                               "explicit_input_sha256": explicit_hash}, problems))

    # 3. re-run the full matrix against the unpacked tree. verify_tree cleans
    #    the bytecode its own subprocesses generate before returning, so any
    #    __pycache__ still present afterwards (or any undeclared file) is a
    #    genuine property of the released tree.
    matrix_checks, extras = verify_tree(tree)
    allowed = set(RELEASE_FILES) | set(TOOLING_FILES) | {
        MANIFEST_NAME, REBUILD_NAME,
        manifest["files"]["explicit_input"]["path"],
        "audit-report.json",
    }
    residue, unknown = [], []
    for path in tree.rglob("*"):
        if not path.is_file():
            continue
        rel = str(path.relative_to(tree))
        if path.name == "__pycache__" or path.suffix in (".pyc", ".pyo"):
            residue.append(rel)
        elif rel not in allowed:
            unknown.append(rel)
    hygiene_problems = []
    if residue:
        hygiene_problems.append(f"bytecode artifacts in release tree: {sorted(residue)}")
    if unknown:
        hygiene_problems.append(f"files not declared in the release allow-list: {sorted(unknown)}")
    checks.append(CheckResult("audit-tree-hygiene",
                              "no bytecode and no undeclared files in the tree",
                              not hygiene_problems,
                              {"allowed": sorted(allowed), "unknown": sorted(unknown),
                               "bytecode": sorted(residue)}, hygiene_problems))

    recorded = {c["id"]: c for c in manifest["verification"]["checks"]}
    matrix_problems: list[str] = []
    for check in matrix_checks:
        if not check.passed:
            matrix_problems.append(f"re-run check failed: {check.id}")
            continue
        old = recorded.get(check.id)
        if old is None:
            matrix_problems.append(f"check {check.id} absent from manifest")
        elif not old.get("passed"):
            matrix_problems.append(f"check {check.id} was not passing in manifest")
    if extras["default_output_sha256"] != manifest["files"]["default_output_sha256"]:
        matrix_problems.append("fixture output hash differs from manifest")
    checks.append(CheckResult(
        "audit-verification-matrix", "full verification matrix re-runs and matches",
        not matrix_problems,
        {"rerun_checks": [c.as_dict() for c in matrix_checks],
         "output_hash_now": extras["default_output_sha256"],
         "output_hash_manifest": manifest["files"]["default_output_sha256"]},
        matrix_problems,
    ))

    # 4. test summary consistency
    summary_problems = []
    recorded_summary = manifest.get("test_summary", {})
    if not recorded_summary.get("successful"):
        summary_problems.append("manifest test summary was not successful")
    if extras["test_summary"].get("tests_run") != recorded_summary.get("tests_run"):
        summary_problems.append("test count differs from manifest summary")
    if not extras["test_summary"].get("successful"):
        summary_problems.append("re-run test suite is not successful")
    checks.append(CheckResult("audit-test-summary", "unittest summary consistent",
                              not summary_problems,
                              {"manifest": recorded_summary, "now": extras["test_summary"]},
                              summary_problems))

    # 5. interpreter compatibility record
    interp_problems = []
    recorded_minor = manifest["interpreter"]["minor"]
    current_minor = f"{sys.version_info.major}.{sys.version_info.minor}"
    if recorded_minor != current_minor:
        interp_problems.append(
            f"interpreter minor differs: build used {recorded_minor}, audit uses {current_minor}; "
            "results are still compared byte-for-byte above"
        )
    spec = manifest["release"]["requires_python"]
    interp_problems.extend(
        check_requires_python(spec, (sys.version_info.major, sys.version_info.minor))
    )
    checks.append(CheckResult("audit-interpreter", "interpreter compatibility with manifest",
                              not interp_problems,
                              {"build_interpreter": manifest["interpreter"],
                               "audit_interpreter": python_probe()}, interp_problems))

    report = {
        "audited_at_utc": utc_now(),
        "tree": str(tree),
        "release": manifest["release"],
        "all_passed": all(c.passed for c in checks),
        "checks": [c.as_dict() for c in checks],
    }
    report_path = tree / "audit-report.json"
    write_canonical_json(report_path, report)
    for check in checks:
        print(f"[{'PASS' if check.passed else 'FAIL'}] {check.id}: {check.title}")
        for problem in check.problems:
            print(f"    - {problem}")
    print("AUDIT PASSED" if report["all_passed"] else "AUDIT FAILED")
    print(f"report: {report_path}")
    return 0 if report["all_passed"] else 1


# --------------------------------------------------------------------------- #
# list / history
# --------------------------------------------------------------------------- #
def cmd_list(_args: argparse.Namespace) -> int:
    root = repo_root()
    releases_dir = dist_root(root) / "releases"
    rows = []
    if releases_dir.is_dir():
        for entry in sorted(releases_dir.iterdir()):
            manifest = entry / MANIFEST_NAME
            if not manifest.is_file():
                continue
            data = json.loads(manifest.read_text(encoding="utf-8"))
            rows.append({
                "release": entry.name,
                "version": data["release"]["version"],
                "built_at_utc": data["release"]["built_at_utc"],
                "source_tree_sha256": data["release"]["source_tree_sha256"],
                "all_passed": data["verification"]["all_passed"],
            })
    current = current_target_name(root)
    for row in rows:
        marker = " (current)" if row["release"] == current else ""
        print(f"{row['release']}{marker} v{row['version']} "
              f"built={row['built_at_utc']} passed={row['all_passed']} "
              f"tree={row['source_tree_sha256'][:12]}")
    if not rows:
        print("no complete releases")
    return 0


def cmd_history(_args: argparse.Namespace) -> int:
    path = history_file(repo_root())
    if not path.is_file():
        print("no history yet")
        return 0
    for line in path.read_text(encoding="utf-8").splitlines():
        print(line)
    return 0


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    build = sub.add_parser("build", help="verify and publish a new release")
    build.add_argument("--inject-failure", action="store_true",
                       help="stage a failing test to exercise quarantine (self-test only)")
    build.set_defaults(func=cmd_build)

    verify = sub.add_parser("verify", help="run the verification matrix against a tree")
    verify.add_argument("--tree", help="tree root (defaults to the repository)")
    verify.set_defaults(func=cmd_verify)

    rollback = sub.add_parser("rollback", help="point dist/current at a previous release")
    rollback.add_argument("--to", help="release directory name; defaults to previous")
    rollback.set_defaults(func=cmd_rollback)

    audit = sub.add_parser("audit", help="offline re-verification of a release directory")
    audit.add_argument("--tree", required=True, help="unpacked release directory")
    audit.set_defaults(func=cmd_audit)

    sub.add_parser("list", help="list complete releases").set_defaults(func=cmd_list)
    sub.add_parser("history", help="print the append-only event log").set_defaults(func=cmd_history)
    return parser


def main(argv: list[str]) -> int:
    assert_isolated()
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
