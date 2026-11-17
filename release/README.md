# Reproducible release / verify / audit chain

This directory contains a dependency-free, offline toolchain that turns the
repository into a self-contained release directory an auditor can rebuild and
check byte-for-byte on another machine. No network service, third-party
package, credential or real policy data is involved; the only input beyond
the bundled fixture is a small synthetic arithmetic input shipped in
`inputs/explicit-input.json`.

## Why every entry point runs under `python3 -I`

Isolated mode (`-I`) ignores `PYTHONPATH`, `PYTHONHOME`, user site-packages and
the script-directory/CWD injection that normal Python performs. The
verification matrix deliberately plants a hostile `json.py` in both the working
directory and the explicit-input directory; without `-I`, `import json` would
execute that file. Every runner:

* asserts `sys.flags.isolated` and aborts otherwise;
* inserts `<tree>/src` into `sys.path` itself, so package resolution never
  depends on the caller's current directory or environment;
* receives a fixed minimal environment (`PATH`, `LANG`, `TZ` plus an explicit
  `PYTHONHASHSEED=0`, which is documented even though `-I` ignores `PYTHON*`).

## Commands

All commands are run from the repository root and use the standard library
only.

```bash
python3 -I release/releng.py verify            # full matrix against the source tree
python3 -I release/releng.py build             # stage -> verify -> promote a release
python3 -I release/releng.py build --inject-failure   # self-test: force quarantine
python3 -I release/releng.py list              # list complete releases
python3 -I release/releng.py rollback [--to NAME]     # point current at a prior release
python3 -I release/releng.py history           # append-only JSONL event log
python3 -I release/releng.py audit --tree DIR  # offline re-verification of a release
bash release/selftest.sh                       # end-to-end self-test in a temp copy
```

The two runners can also be used directly inside any release directory:

```bash
python3 -I release/run_cli.py [INPUT.json]     # default fixture when omitted
python3 -I release/run_cli.py --probe          # interpreter / environment / version facts
python3 -I release/run_tests.py "$(pwd)"       # unittest suite as stable JSON
```

## What `build` does

1. **Preflight.** Checks `requires-python` against the running interpreter,
   confirms every allow-listed source/tooling file exists, and moves anything
   unexpected in `dist/` (stale `staging-*` trees, release directories without
   a manifest, unknown files, a `current` symlink escaping `dist/releases`)
   into `dist/quarantine/` with a timestamped `.reason.txt`. Hard problems
   abort before any artifact is written; handled hygiene items are recorded as
   notes in the manifest.
2. **Staging.** Copies an explicit allow-list of files into
   `dist/staging-<id>/`, adds the synthetic input and `REBUILD.md`, and
   normalizes every mtime to the UNIX epoch.
3. **Verification.** Runs the matrix below inside temporary working
   directories *outside* the tree.
4. **Manifest.** Writes a canonical (sorted-key) `manifest.json` containing
   source/tooling/output hashes, interpreter facts, hashed environment
   variables, check results and the unittest summary.
5. **Promotion.** On full success, atomically renames the staging tree into
   `dist/releases/<seq>-<version>-<treehash12>/` and swaps the `current`
   symlink via `os.replace`. On any failure the staging tree is moved to
   `dist/quarantine/`; `current` continues to point at the last complete
   release, so a downstream consumer can never observe a partial artifact.

## Verification matrix

| Check | What it proves |
| --- | --- |
| `module-import` | package imports under `-I` while a planted `json.py` sits in the CWD |
| `interpreter-probe` | package version equals `pyproject.toml` version; isolated mode and `sys.path` are correct |
| `cli-default-fixture` | CLI output for the bundled default fixture equals an independent in-process pipeline result |
| `cli-explicit-input` | explicit synthetic input reproduces hand-derived decisions |
| `json-canonical` | output parses as JSON and is byte-identical to `json.dumps(..., sort_keys=True, indent=2)` with sorted nested keys |
| `determinism` | reruns from different CWDs and hash seeds are byte-identical |
| `timezone-invariance` | identical bytes under `TZ=UTC`, `America/Los_Angeles`, `Asia/Kolkata` |
| `unittest-suite` | bundled suite passes; stable JSON summary with sorted test IDs |
| `source-hashes` | SHA-256 of every source and tooling file for later recomputation |

## Manifest contents

```text
schema_version, release{name,id,sequence,version,requires_python,built_at_utc,
  source_tree_sha256,parent_release}, interpreter{version,minor,implementation,
  byteorder,isolated_mode,interpreter_sha256,...}, environment{
  observed_parent_env_hashed, fixed_subprocess_env}, files{source,tooling,
  explicit_input,default_output_sha256}, verification{all_passed,checks[]},
  test_summary, fixture_output, explicit_input_payload, preflight_*
```

Parent-process environment values are **hashed, never stored raw**, so secrets
on a build machine cannot leak into an auditable artifact. The manifest also
records the exact Python minor version and the interpreter-binary hash; a
replay on a different minor is allowed but the byte-level output comparison in
`audit` will expose any divergence.

## Layout produced under `dist/`

```text
dist/
  current -> releases/<latest-complete>/      # relative, atomically swapped
  releases/<seq>-<version>-<treehash12>/      # complete, immutable-looking trees
    manifest.json  REBUILD.md  inputs/  src/  tests/  fixtures/  release/  pyproject.toml
  quarantine/<stamp>-<name>/ + .reason.txt    # every failed/foreign artifact
  history.jsonl                                # append-only build/rollback/quarantine events
```

## Offline auditor workflow

1. Receive one release directory (or an archive unpacked into a new, empty
   directory).
2. Read `REBUILD.md` and rerun the CLI, the explicit input and the test suite
   using only a compatible Python interpreter.
3. Run `python3 -I release/releng.py audit --tree .`. The audit recomputes all
   hashes, re-runs the entire matrix (three time zones, two working
   directories, planted-module checks), validates the canonical manifest and
   writes `audit-report.json`. Exit code 0 means the eligibility results and
   every source file match the recorded release; any tamper or drift yields a
   named failing check and exit code 1.

## Rollback

`rollback` (default: most recent complete release other than current, or
`--to NAME`) swaps the same atomic symlink and appends a rollback event to
`history.jsonl`. Quarantined artifacts are never deleted automatically, so a
forensic comparison remains possible; releases are never overwritten — a name
collision also goes to quarantine.
