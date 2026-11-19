#!/usr/bin/env bash
# L2-A walkthrough - self-checking.
#
#   bash examples/l2a-walkthrough.sh
#
# Proves the collection boundary on this host: only declared roots appear in
# the scan, excluded dirs are recorded with a reason, and an undeclared
# directory never shows up. Runs in a throwaway directory; no network.

set -u

RAW_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_ROOT="$RAW_ROOT"
if command -v cygpath >/dev/null 2>&1; then
  REPO_ROOT="$(cygpath -w "$RAW_ROOT")"
fi
GROMA="$REPO_ROOT/groma.py"

if [ -n "${PYTHON:-}" ]; then
  PY="$PYTHON"
elif command -v python3 >/dev/null 2>&1 && python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
  PY="python3"
elif command -v python >/dev/null 2>&1 && python -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
  PY="python"
elif command -v py >/dev/null 2>&1 && py -3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
  PY="py -3"
else
  echo "l2a: Python 3.10+ not found; set PYTHON=/full/path/to/python" >&2
  exit 2
fi

py_run() {
  if [ "$PY" = "py -3" ]; then
    py -3 "$@"
  else
    "$PY" "$@"
  fi
}

groma() { py_run "$GROMA" "$@"; }

BASE="$(py_run -c 'import tempfile; print(tempfile.mkdtemp(prefix="groma-l2a-"))')" || {
  echo "l2a: cannot create a temp directory" >&2
  exit 2
}
trap 'rm -rf "$BASE"' EXIT

UNIT="$BASE/unit"
SRC="$BASE/src"
OUTSIDE="$BASE/outside"
FAILED=0

check() {  # EXPECTED LABEL ACTUAL
  if [ "$1" = "$3" ]; then
    printf '  ok    %-40s rc=%s\n' "$2" "$3"
  else
    printf '  WRONG %-40s rc=%s (expected %s)\n' "$2" "$3" "$1" >&2
    FAILED=1
  fi
}

run() {  # EXPECTED LABEL CMD...
  local expected="$1" label="$2"
  shift 2
  "$@" >/dev/null 2>&1
  check "$expected" "$label" "$?"
}

echo "L2-A walkthrough (repo: $REPO_ROOT)"
echo
echo "-- fixture: one declared root, one undeclared root"
mkdir -p "$UNIT" "$SRC/sub" "$SRC/.git" "$OUTSIDE"
printf 'alpha\n' > "$SRC/a.txt"
printf 'beta\n' > "$SRC/sub/b.txt"
printf 'ignored\n' > "$SRC/.git/config"
printf 'not yours\n' > "$OUTSIDE/secret.txt"

run 0 "init" groma --root "$UNIT" init --name l2a
run 0 "sources add" groma --root "$UNIT" sources add src --path "$SRC" --harness test
run 0 "sources list" groma --root "$UNIT" sources list
run 0 "scan" groma --root "$UNIT" scan

MANIFEST="$(ls "$UNIT/.groma/scans/"scan-*.json 2>/dev/null | head -1)"
if [ -z "$MANIFEST" ]; then
  echo "  WRONG no scan manifest was written" >&2
  exit 1
fi

py_run - "$MANIFEST" <<'PY'
import json
import sys

problem = []
with open(sys.argv[1], encoding="utf-8") as handle:
    scan = json.load(handle)

listed = sorted(entry["path"] for entry in scan["files"])
if listed != ["a.txt", "sub/b.txt"]:
    problem.append("files = %r, expected ['a.txt', 'sub/b.txt']" % (listed,))
if scan["file_count"] != 2:
    problem.append("file_count = %r, expected 2" % (scan["file_count"],))
if any("secret" in entry["path"] for entry in scan["files"]):
    problem.append("an undeclared file leaked into the scan")

reasons = {entry["path"]: entry["reason"] for entry in scan["skipped"]}
if reasons.get(".git") != "excluded_dir":
    problem.append("expected .git to be skipped as excluded_dir, got %r" % (reasons.get(".git"),))

if problem:
    for item in problem:
        print("  WRONG " + item, file=sys.stderr)
    raise SystemExit(1)
print("  ok    %-40s files=%d skipped=%d" % ("boundary held", scan["file_count"], scan["skipped_count"]))
PY
check 0 "boundary assertions" "$?"

echo
if [ "$FAILED" -eq 0 ]; then
  echo "RESULT: PASS - only declared roots were scanned."
else
  echo "RESULT: FAIL - see WRONG lines above." >&2
fi
exit "$FAILED"
