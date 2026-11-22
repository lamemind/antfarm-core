#!/usr/bin/env bash
# Deterministic tests of the loop: grant validator, and the loop process with script sessions. No LLM.
#
#   antfarm-core/test/loop-tests.sh
#
# It runs on a copy of bin/, schema/ and test/ in a temporary folder, with the state root
# (ANTFARM_STATE_DIR) pointed at a sibling folder of the copy: grants, requests, mailboxes, sessions
# and the loop log are its own, and the registry of a project receives no test rows.
# Exits 1 if a test fails.
set -uo pipefail

ANTFARM=$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)
TMP=$(mktemp -d)
ENGINE="$TMP/engine"
mkdir -p "$ENGINE"
cp -r "$ANTFARM/bin" "$ANTFARM/schema" "$ANTFARM/test" "$ENGINE/"
VALIDATOR="$ENGINE/bin/validate-grants.py"
GRANTS="$ENGINE/test/grants"

cleanup() {
  rm -rf "$TMP"
}
trap cleanup EXIT

failed=0
verify() { # <description> <command...>
  local description=$1; shift
  if "$@" > /dev/null 2>&1; then
    printf '  ok   %s\n' "$description"
  else
    printf '  FAIL %s\n' "$description"
    failed=$((failed + 1))
  fi
}

rejects() { # <grants set> <text the validator prints>: exit 1 and the reason on stderr
  local errors code
  errors=$("$VALIDATOR" "$GRANTS/$1" 2>&1 >/dev/null)
  code=$?
  [[ $code -eq 1 && $errors == *"$2"* ]]
}

echo "grant validator"
verify "LLM fixture valid → 0" "$VALIDATOR" "$GRANTS/fixture"
verify "script fixture valid → 0" "$VALIDATOR" "$GRANTS/script"
verify "two roots → 1, says how many" rejects invalid-two-roots "found 2"
verify "parent not declared → 1" rejects invalid-missing-parent "the parent nobody is not a declared position"
verify "cycle of parents → 1" rejects invalid-cycle "cycle of parents"
verify "a position called human → 1" rejects invalid-human-position "not a position"
verify "file name other than the position → 1" rejects invalid-name-mismatch "must be called a.json"
verify "file not JSON → 1" rejects invalid-not-json "not readable as JSON"
verify "script launch with a prompt → 1" rejects invalid-script-with-prompt "launch/prompt"
verify "llm launch without a prompt → 1" rejects invalid-llm-without-prompt "'prompt' is a required property"
verify "max_sessions 0 → 1" rejects invalid-zero-sessions "less than the minimum of 1"
verify "empty directory → 1" bash -c "mkdir -p '$TMP/empty' && ! '$VALIDATOR' '$TMP/empty'"
verify "missing directory → 2" bash -c "'$VALIDATOR' '$TMP/missing'; [[ \$? -eq 2 ]]"

echo
if [[ $failed -gt 0 ]]; then
  echo "loop tests: $failed FAILED"
  exit 1
fi
echo "loop tests: all ok"
