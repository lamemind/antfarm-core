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
LOOP="$ENGINE/bin/loop.py"
GRANTS="$ENGINE/test/grants"
CYCLE=0.2

# Every process a scenario starts: the loop, and the script sessions it launched in a session of
# their own, which no signal to the test reaches.
kill_state() { # <state directory>
  python3 - "$1" <<'PY' | xargs -r kill -9 2>/dev/null
import json, sys
from pathlib import Path
loop = Path(sys.argv[1]) / "loop"
for name in ("status.json",):
    try:
        print(json.loads((loop / name).read_text())["pid"])
    except (OSError, ValueError, KeyError):
        pass
try:
    for entry in json.loads((loop / "sessions.json").read_text())["sessions"].values():
        print(entry["pid"])
except (OSError, ValueError, KeyError):
    pass
PY
}

cleanup() {
  [[ -n ${LOOP_PID:-} ]] && kill -9 "$LOOP_PID" 2>/dev/null
  for state in "$TMP"/state-*; do
    [[ -d $state ]] && kill_state "$state"
  done
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

new_state() { # <name> <grants set>: a state root with a copy of the grants; sets STATE
  STATE="$TMP/state-$1"
  mkdir -p "$STATE/grants"
  cp "$GRANTS/$2"/*.json "$STATE/grants/"
  export ANTFARM_STATE_DIR="$STATE"
}

start_loop() { # starts the loop on $STATE in the background and waits for its first status
  "$LOOP" --cycle "$CYCLE" > "$STATE/loop.out" 2>&1 &
  LOOP_PID=$!
  wait_for 15 test -f "$STATE/loop/status.json"
}

stop_loop() {
  kill_state "$STATE"
  wait "$LOOP_PID" 2>/dev/null
  LOOP_PID=
}

wait_for() { # <seconds> <command...>: retries the command until it succeeds or the time runs out
  local limit=$((SECONDS + $1)); shift
  until "$@" > /dev/null 2>&1; do
    [[ $SECONDS -ge $limit ]] && return 1
    sleep 0.2
  done
}

rows() { # <key=value>...: how many rows of the loop log carry all the given fields
  python3 - "$STATE/registry/loop.jsonl" "$@" <<'PY'
import json, sys
wanted = [argument.split("=", 1) for argument in sys.argv[2:]]
try:
    rows = [json.loads(line) for line in open(sys.argv[1], encoding="utf-8")]
except OSError:
    rows = []
print(sum(1 for row in rows if all(str(row.get(key)) == value for key, value in wanted)))
PY
}

rows_are() { # <count> <key=value>...
  local expected=$1; shift
  [[ $(rows "$@") -eq $expected ]]
}

session_field() { # <session> <field>: a field of the session in the registry
  python3 -c "import json, sys; print(json.load(open(sys.argv[1]))['sessions'][sys.argv[2]][sys.argv[3]])" \
    "$STATE/loop/sessions.json" "$1" "$2"
}

session_alive() { # <session>: the registry marks it alive and its process exists
  [[ $(session_field "$1" status) == alive ]] && kill -0 "$(session_field "$1" pid)"
}

switch_is() { # <position> <True|False>
  [[ $(python3 -c "import json, sys; print(json.load(open(sys.argv[1]))[sys.argv[2]]['on'])" \
       "$STATE/loop/switches.json" "$1") == "$2" ]]
}

forge() { # <from> <kind> [json fields]: a request file written by hand, as a session with Write could
  python3 - "$STATE/loop/requests" "$1" "$2" "${3:-{\}}" <<'PY'
import json, sys, time, uuid
from pathlib import Path
directory, sender, kind, fields = Path(sys.argv[1]), sys.argv[2], sys.argv[3], json.loads(sys.argv[4])
request = {"id": f"r-{uuid.uuid4().hex[:12]}", "from": sender, "kind": kind, **fields}
(directory / f"{time.time_ns()}-{request['id']}.json").write_text(json.dumps(request))
print(request["id"])
PY
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

echo "loop: start"
new_state invalid invalid-two-roots
"$LOOP" > "$STATE/loop.out" 2>&1
verify "invalid grants at start → exit 2" test $? -eq 2
verify "… with the reason in the log" rows_are 1 event=grants-rejected reason=invalid-at-start
verify "… and on stderr" grep -q "found 2" "$STATE/loop.out"
new_state no-grants script
rm -rf "$STATE/grants"
"$LOOP" > "$STATE/loop.out" 2>&1
verify "no grants directory → exit 2" test $? -eq 2

echo "loop: neutral, switches, spawn"
new_state main script
start_loop
sleep 3
verify "in neutral: 0 spawn rows after 3 s" rows_are 0 event=spawn
verify "every switch off" bash -c "! grep -q '\"on\": true' '$STATE/loop/switches.json'"
verify "status lists the three positions" python3 -c \
  "import json, sys; assert sorted(json.load(open(sys.argv[1]))['positions']) == ['lead', 'root', 'worker']" \
  "$STATE/loop/status.json"
verify "the log lives in registry/loop.jsonl of the state root" test -s "$STATE/registry/loop.jsonl"
# A process that no session explains: the request command waits 20 s for a registration that
# never comes, so it runs in the background while the other checks go on.
"$ENGINE/bin/request" spawn lead > "$STATE/stranger.out" 2>&1 &
STRANGER=$!
"$LOOP" switch root on > /dev/null
verify "switch root on → one spawn row on root, reason switch" \
  wait_for 5 rows_are 1 event=spawn position=root reason=switch
verify "… root-1 alive" session_alive root-1
"$LOOP" switch root off > /dev/null
sleep 1
verify "switch root off → switch-off row" rows_are 1 event=switch-off position=root
verify "… no new spawn on root" rows_are 1 event=spawn position=root
"$LOOP" spawn ghost > /dev/null
verify "human spawn on an undeclared position → CLI exit 1" test $? -eq 1
verify "… rejected row, undeclared-position" rows_are 1 event=rejected position=ghost reason=undeclared-position
"$LOOP" spawn lead "spawn ghost
spawn root
spawn worker sleep 0" > /dev/null
verify "human spawn on lead switched off → queued" rows_are 1 event=queued position=lead reason=switched-off
"$LOOP" switch lead on > /dev/null
verify "switch lead on → the queued spawn runs" wait_for 5 rows_are 1 event=spawn position=lead reason=request
verify "… lead-1 reads the spawn text from its mailbox" wait_for 5 rows_are 1 event=received session=lead-1
verify "session spawn on an undeclared position → rejected" \
  wait_for 5 rows_are 1 event=rejected position=ghost from=lead-1 reason=undeclared-position
verify "session spawn on a position that is not its child → rejected" \
  wait_for 5 rows_are 1 event=rejected position=root from=lead-1 reason=not-a-child
verify "session spawn on its child switched off → queued" \
  wait_for 5 rows_are 1 event=queued position=worker from=lead-1 reason=switched-off
"$LOOP" switch worker on > /dev/null
verify "switch worker on → spawn on worker asked by lead-1" \
  wait_for 5 rows_are 1 event=spawn position=worker from=lead-1
verify "… launched_by is lead-1 in the registry" bash -c "[[ \$(python3 -c \"import json; print(json.load(open('$STATE/loop/sessions.json'))['sessions']['worker-1']['launched_by']['session'])\") == lead-1 ]]"
forge lead-1 switch '{"position": "worker", "on": false}' > /dev/null
verify "a switch from a session → rejected, human-only" wait_for 5 rows_are 1 event=rejected reason=human-only
forge nobody spawn '{"position": "lead"}' > /dev/null
verify "a request from no live session → rejected, unknown-sender" wait_for 5 rows_are 1 event=rejected reason=unknown-sender
forge human dance '{"position": "lead"}' > /dev/null
verify "a kind the loop does not know → rejected, unknown-kind" wait_for 5 rows_are 1 event=rejected reason=unknown-kind
wait "$STRANGER"
verify "request from a process that is not a session → exit 1, unknown-sender" test $? -eq 1
verify "… and it says so" grep -q unknown-sender "$STATE/stranger.out"

echo "loop: grants reread while running"
printf '{ broken\n' > "$STATE/grants/bad.json"
verify "an invalid file → grants-rejected row" wait_for 10 rows_are 1 event=grants-rejected reason=invalid
verify "… the loop keeps running" kill -0 "$LOOP_PID"
rm "$STATE/grants/bad.json"
python3 - "$STATE/grants" <<'PY'
import json, sys
from pathlib import Path
grant = json.loads((Path(sys.argv[1]) / "worker.json").read_text())
grant.update(position="extra", parent="root")
(Path(sys.argv[1]) / "extra.json").write_text(json.dumps(grant))
PY
verify "a new position → grants-loaded row" wait_for 10 rows_are 1 event=grants-loaded "detail=4 positions, root root; added ['extra'], removed -"
verify "… with its switch off" switch_is extra False
mv "$STATE/grants/worker.json" "$TMP/worker.json"
verify "removing a position with live sessions → grants-rejected" \
  wait_for 10 rows_are 2 event=grants-rejected reason=invalid
verify "… worker is still a position" python3 -c \
  "import json, sys; assert 'worker' in json.load(open(sys.argv[1]))['positions']" "$STATE/loop/status.json"
mv "$TMP/worker.json" "$STATE/grants/worker.json"
stop_loop

echo
if [[ $failed -gt 0 ]]; then
  echo "loop tests: $failed FAILED"
  exit 1
fi
echo "loop tests: all ok"
