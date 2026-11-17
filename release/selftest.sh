#!/usr/bin/env bash
# End-to-end offline self-test for the release chain.
#
# Runs entirely on the local machine with the standard library only. Creates
# all release artifacts under a throwaway copy of the repository in a temp
# directory, so the real working tree and its dist/ are never touched.
#
# Usage:  bash release/selftest.sh
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
WORK="$(mktemp -d -t releng-selftest-XXXXXX)"
trap 'rm -rf "$WORK"' EXIT

cp -R "$REPO" "$WORK/repo"
# Start from a clean tree: local build outputs never travel with the copy.
rm -rf "$WORK/repo/dist" "$WORK/repo/.verify-work"
find "$WORK/repo" -type d -name __pycache__ -prune -exec rm -rf {} +
ROOT="$WORK/repo"
RELENG=(python3 -I release/releng.py)

step() { printf '\n=== %s ===\n' "$1"; }
assert_rc() { # expected actual label
  if [ "$1" != "$2" ]; then
    echo "FAIL: $3 (expected rc=$1, got rc=$2)" >&2
    exit 1
  fi
  echo "ok: $3"
}

cd "$ROOT"

step "1. verification matrix on the source tree"
"${RELENG[@]}" verify
assert_rc 0 $? "verify source tree"

step "2. first build"
"${RELENG[@]}" build
assert_rc 0 $? "first build"
test -L dist/current
# current is a relative symlink into dist/releases; take the basename.
FIRST="$(basename "$(readlink dist/current)")"

step "3. second build is reproducible (same source/output hashes)"
"${RELENG[@]}" build
assert_rc 0 $? "second build"
SECOND="$(basename "$(readlink dist/current)")"
python3 -I - "$FIRST" "$SECOND" <<'PY'
import json, sys
from pathlib import Path
base = Path("dist/releases")
a = json.loads((base/sys.argv[1]/"manifest.json").read_text())
b = json.loads((base/sys.argv[2]/"manifest.json").read_text())
assert a["release"]["source_tree_sha256"] == b["release"]["source_tree_sha256"]
assert a["files"] == b["files"]
assert a["fixture_output"] == b["fixture_output"]
assert a["release"]["sequence"] < b["release"]["sequence"]
print("ok: reproducible hashes, monotonic sequence")
PY

step "4. injected failure isolates the partial artifact, current untouched"
set +e
"${RELENG[@]}" build --inject-failure
RC=$?
set -e
assert_rc 1 "$RC" "injected build fails"
test "$(basename "$(readlink dist/current)")" = "$SECOND"
test -n "$(ls -A dist/quarantine/*staging* 2>/dev/null)"
echo "ok: current symlink unchanged, staging quarantined"

step "5. rollback to the previous complete release"
"${RELENG[@]}" rollback
assert_rc 0 $? "rollback"
test "$(basename "$(readlink dist/current)")" = "$FIRST"
"${RELENG[@]}" rollback --to "$SECOND"
test "$(basename "$(readlink dist/current)")" = "$SECOND"
echo "ok: rollback both default and explicit"

step "6. offline audit of an unpacked release copy"
cp -R "dist/releases/$(basename "$SECOND")" "$WORK/audit-copy"
( cd "$WORK/audit-copy" && python3 -I release/releng.py audit --tree . )
assert_rc 0 $? "clean audit"

step "7. audit rejects a tampered release"
cp -R "$WORK/audit-copy" "$WORK/audit-tamper"
printf '\n# planted tamper\n' >> "$WORK/audit-tamper/src/benefit_eligibility_replay/engine.py"
set +e
( cd "$WORK/audit-tamper" && python3 -I release/releng.py audit --tree . )
RC=$?
set -e
assert_rc 1 "$RC" "tampered audit fails"

step "8. preflight quarantines stale/partial artifacts"
mkdir -p dist/staging-left-over dist/releases/0099-incomplete
echo x > dist/staging-left-over/x.txt
echo y > dist/releases/0099-incomplete/y.txt
"${RELENG[@]}" build >/dev/null
assert_rc 0 $? "build after dirty preflight"
ls dist/quarantine | grep -q staging-left-over
ls dist/quarantine | grep -q 0099-incomplete
echo "ok: stale staging and incomplete release quarantined"

step "9. history log captured every event"
test -s dist/history.jsonl
grep -q '"event": "build"' dist/history.jsonl
grep -q '"event": "rollback"' dist/history.jsonl
grep -q '"event": "quarantine"' dist/history.jsonl
echo "ok: history.jsonl complete"

printf '\nALL SELF-TESTS PASSED\n'
