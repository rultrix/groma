#!/usr/bin/env bash
# L1 walkthrough - self-checking.
#
#   bash examples/quickstart.sh
#
# Runs the documented L1 sequence in a throwaway directory and prints the exit
# code of every step. Exits 0 only if all of them match the contract in README.
# Never touches this repo, never uses the network, never reads credentials.

set -u

RAW_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_ROOT="$RAW_ROOT"
if command -v cygpath >/dev/null 2>&1; then
  REPO_ROOT="$(cygpath -w "$RAW_ROOT")"   # Python needs a native path on Windows
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
  echo "quickstart: Python 3.10+ not found; set PYTHON=/full/path/to/python" >&2
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

# Let Python pick the temp path: it is the only component that has to open it.
BASE="$(py_run -c 'import tempfile; print(tempfile.mkdtemp(prefix="groma-quickstart-"))')" || {
  echo "quickstart: cannot create a temp directory" >&2
  exit 2
}
trap 'rm -rf "$BASE"' EXIT

SPACE="$BASE/my-space"
EMPTY="$BASE/empty-space"
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

json_field() {  # FIELD, reads stdin
  sed -n "s/.*\"$1\": \"\\(.*\\)\".*/\\1/p" | head -1
}

echo "L1 walkthrough (repo: $REPO_ROOT)"
echo
echo "-- main flow: a unit with two files"
mkdir -p "$SPACE/sub"
printf 'hello\n' > "$SPACE/note.txt"
printf 'alpha\n' > "$SPACE/sub/inner.txt"

run 0 "init on a brand-new root" groma --root "$SPACE" init --name my-space
run 0 "attach an agent" groma --root "$SPACE" attach qoder --harness qoder

RECEIPT_OUT="$(groma --root "$SPACE" receipt --note 'quickstart')"
check 0 "receipt" "$?"
COUNT="$(printf '%s' "$RECEIPT_OUT" | sed -n 's/.*"file_count": \([0-9]*\).*/\1/p')"
if [ "$COUNT" = "2" ]; then
  printf '  ok    %-40s file_count=%s\n' "receipt covers two files" "$COUNT"
else
  printf '  WRONG %-40s file_count=%s (expected 2)\n' "receipt covers two files" "$COUNT" >&2
  FAILED=1
fi
REL="$(printf '%s' "$RECEIPT_OUT" | json_field receipt)"
if [ -z "$REL" ]; then
  echo "  WRONG could not read the receipt path from the JSON" >&2
  exit 1
fi

run 0 "verify a clean unit" groma --root "$SPACE" verify "$REL"
printf 'hellp\n' > "$SPACE/note.txt"
run 3 "verify after one byte changed" groma --root "$SPACE" verify "$REL"
printf 'hello\n' > "$SPACE/note.txt"
run 0 "verify after restore" groma --root "$SPACE" verify "$REL"
rm "$SPACE/sub/inner.txt"
run 3 "verify after deleting a file" groma --root "$SPACE" verify "$REL"

echo
echo "-- empty unit: no vacuous pass"
run 0 "init creates the root" groma --root "$EMPTY" init --name empty-space
run 2 "receipt on an empty unit" groma --root "$EMPTY" receipt
EMPTY_OUT="$(groma --root "$EMPTY" receipt --allow-empty)"
check 0 "receipt --allow-empty" "$?"
EREL="$(printf '%s' "$EMPTY_OUT" | json_field receipt)"
if [ -z "$EREL" ]; then
  echo "  WRONG could not read the receipt path from the JSON" >&2
  exit 1
fi
run 4 "verify an empty receipt" groma --root "$EMPTY" verify "$EREL"

echo
if [ "$FAILED" -eq 0 ]; then
  echo "RESULT: PASS - every step matched README."
else
  echo "RESULT: FAIL - see WRONG lines above." >&2
fi
exit "$FAILED"
