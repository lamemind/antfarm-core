#!/usr/bin/env bash
# Deterministic tests of the loop: grant validator, the loop process with script sessions, and its
# concurrency with a fake `claude` (test/fake-claude.py). No LLM.
#
#   antfarm-core/test/loop-tests.sh [--concurrency <n>]
#
# Without arguments it runs every scenario: the ones with script sessions on the loop's default
# concurrency, and the concurrency scenarios, each with the concurrency it measures. With
# --concurrency it runs the scenarios with script sessions on that concurrency, and skips the
# concurrency scenarios, which fix their own.
#
# It runs on a copy of bin/, schema/ and test/ in a temporary folder, with the state root
# (ANTFARM_STATE_DIR) pointed at a sibling folder of the copy: grants, requests, mailboxes, sessions
# and the loop log are its own, and the registry of a project receives no test rows. A scenario with
# the fake claude gives the loop a HOME of its own, where the fake sessions write their transcripts.
# LOOP_TESTS_KEEP=1 keeps the temporary folder, with the log of every scenario, for whoever debugs.
# Exits 1 if a test fails.
set -uo pipefail

CONCURRENCY_ARGS=()
if [[ ${1:-} == --concurrency && -n ${2:-} ]]; then
  CONCURRENCY_ARGS=(--concurrency "$2")
elif [[ $# -gt 0 ]]; then
  echo "usage: $0 [--concurrency <n>]" >&2
  exit 2
fi

ANTFARM=$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)
TMP=$(mktemp -d)
ENGINE="$TMP/engine"
mkdir -p "$ENGINE"
cp -r "$ANTFARM/bin" "$ANTFARM/schema" "$ANTFARM/test" "$ENGINE/"
VALIDATOR="$ENGINE/bin/validate-grants.py"
LOOP="$ENGINE/bin/loop.py"
GRANTS="$ENGINE/test/grants"
CYCLE=0.2

# Every process a scenario starts: the loop, the script sessions it launched in a session of their
# own, which no signal to the test reaches, and the commands and sessions of the fake claude.
kill_state() { # <state directory>
  pkill -9 -f "$1/fake-bin/claude" 2>/dev/null
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
  if [[ -n ${LOOP_TESTS_KEEP:-} ]]; then
    echo "LOOP_TESTS_KEEP: the temporary folder stays in $TMP"
  else
    rm -rf "$TMP"
  fi
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

start_loop() { # [concurrency]: starts the loop on $STATE in the background and waits for its status.
  # With a fake claude in $STATE/fake-bin, the loop has it first on its PATH and $STATE/home as HOME.
  local arguments=("${CONCURRENCY_ARGS[@]}")
  [[ -n ${1:-} ]] && arguments=(--concurrency "$1")
  if [[ -d $STATE/fake-bin ]]; then
    HOME="$STATE/home" PATH="$STATE/fake-bin:$PATH" \
      "$LOOP" --cycle "$CYCLE" "${arguments[@]}" --no-notify >> "$STATE/loop.out" 2>&1 &
  else
    "$LOOP" --cycle "$CYCLE" "${arguments[@]}" --no-notify >> "$STATE/loop.out" 2>&1 &
  fi
  LOOP_PID=$!
  wait_for 15 status_pid_is "$LOOP_PID"
}

status_pid_is() { # <pid>: the status file is written by that loop
  [[ $(python3 -c "import json, sys; print(json.load(open(sys.argv[1]))['pid'])" "$STATE/loop/status.json") == "$1" ]]
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

log_check() { # <check> [session]: a check on the times of the loop log, written below in Python
  python3 - "$STATE/registry/loop.jsonl" "$@" <<'PY'
import json, sys
from datetime import datetime
rows = [json.loads(line) for line in open(sys.argv[1], encoding="utf-8")]
check, arguments = sys.argv[2], sys.argv[3:]
at = lambda text: datetime.fromisoformat(text)
if check == "consistent":
    # ts is the time the loop writes the row: it never goes back. read_ts is the time of the read,
    # on every received row, and never later than the row that reports it.
    times = [at(row["ts"]) for row in rows]
    assert times == sorted(times), "ts goes back"
    for row in rows:
        if row["event"] == "received":
            assert row.get("read_ts") and at(row["read_ts"]) <= at(row["ts"]), row
elif check in ("after-close", "before-close"):
    closed = [index for index, row in enumerate(rows) if row["event"] == "closed" and row["session"] == arguments[0]]
    received = [index for index, row in enumerate(rows) if row["event"] == "received" and row["session"] == arguments[0]]
    assert len(closed) == 1 and received, (closed, received)
    if check == "after-close":
        assert received[-1] > closed[0], "the received row precedes the closed row"
    else:
        assert at(rows[received[-1]]["read_ts"]) < at(rows[closed[0]]["ts"]), rows[received[-1]]
elif check == "before-start":
    start = [row for row in rows if row["event"] == "start"][-1]
    [received] = [row for row in rows if row["event"] == "received" and row["reason"] == "before-start"]
    assert at(received["read_ts"]) < at(start["ts"]), (received, start)
PY
}
log_times_consistent() { log_check consistent; }
read_logged_after_close() { log_check after-close "$1"; }
read_before_close() { log_check before-close "$1"; }

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

message_for() { # <position> <session> <text>: a message addressed to a session, as a test deposits it
  python3 - "$ENGINE/bin" "$STATE" "$1" "$2" "$3" <<'PY2'
import sys
sys.path.insert(0, sys.argv[1])
import loopstate
loopstate.deposit_message(loopstate.Paths(sys.argv[2]), sys.argv[3], "human", sys.argv[5], to_session=sys.argv[4])
PY2
}

process_ended() { # <pid>: the process is gone, or a zombie that its parent has not collected yet
  [[ ! -e /proc/$1/stat ]] || [[ $(sed 's/.*) //' "/proc/$1/stat" | cut -d' ' -f1) == Z ]]
}

kill_session() { # <session>: kills the process group of a script session, as a crash would
  kill -9 -- "-$(session_field "$1" pid)" 2>/dev/null
}

identity() { # <session>: "pid starttime" of its process as /proc says, or "gone"
  python3 - "$ENGINE/bin" "$(session_field "$1" pid)" <<'PY2'
import sys
sys.path.insert(0, sys.argv[1])
import loopstate
try:
    print(sys.argv[2], loopstate.start_time(int(sys.argv[2])))
except OSError:
    print("gone")
PY2
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

# The environment of a launch, without claude: what bin/match.py passes to `claude --bg`, and what
# it reads back from /proc/<pid>/environ of a process born with a variable of its own.
launch_environment_check() { # <check>
  mkdir -p "$TMP/state-environment"
  ANTFARM_STATE_DIR="$TMP/state-environment" ANTFARM_TEST_LAUNCHER=1 python3 - "$ENGINE/bin" "$1" <<'PY'
import json, os, subprocess, sys
sys.path.insert(0, sys.argv[1])
import match
check = sys.argv[2]
if check == "passed":
    environment = match.launch_environment()
    assert sorted(environment) == sorted(name for name in match.PASSED_VARIABLES if name in os.environ), environment
    assert "ANTFARM_TEST_LAUNCHER" not in environment and "ANTFARM_STATE_DIR" not in environment, environment
elif check in ("foreign", "clean"):
    extra = {"ANTFARM_TEST_FOREIGN": "a value that is not written"} if check == "foreign" else {}
    process = subprocess.Popen(["sleep", "30"], env={**match.launch_environment(), **extra})
    try:
        found = match.foreign_variables(process.pid)
    finally:
        process.kill()
        process.wait()
    assert found == sorted(extra), found
elif check == "gone":
    process = subprocess.Popen(["true"])
    process.wait()
    assert match.foreign_variables(process.pid) is None
elif check.startswith("version-"):
    # A fake `claude` first on PATH, which answers only --version: the version EXPECTED_VARIABLES was
    # measured on, another one, or nothing. The loop and a match of the director note it twice: the
    # line is written once, and the launch is not stopped.
    import tempfile
    from pathlib import Path
    import loop
    state = Path(tempfile.mkdtemp(dir=os.environ["ANTFARM_STATE_DIR"]))
    fake = state / "fake-bin"
    fake.mkdir()
    answer = {"version-current": f"echo '{match.EXPECTED_VARIABLES_CLAUDE_VERSION} (Claude Code)'",
              "version-stale": "echo '9.9.9 (Claude Code)'", "version-mute": "exit 1"}[check]
    (fake / "claude").write_text(f"#!/bin/sh\n[ \"$1\" = --version ] || exit 2\n{answer}\n")
    (fake / "claude").chmod(0o755)
    os.environ["PATH"] = f"{fake}:{os.environ['PATH']}"
    the_loop = loop.Loop(loop.libstate.Paths(state), 1.0, notify=False)
    the_loop.note_stale_expected_variables()
    the_loop.note_stale_expected_variables()
    log_file = state / "registry" / "loop.jsonl"
    rows = [json.loads(line) for line in log_file.read_text().splitlines()] if log_file.exists() else []
    stale = [row for row in rows if row["event"] == "expected-variables-stale"]
    the_match = match.Match.create(check, "test", Path(sys.argv[1]).parent / "test" / "graphs" / "human.json", {})
    the_match.note_stale_expected_variables()
    the_match.note_stale_expected_variables()
    meta = the_match.meta()
    if check == "version-current":
        assert match.stale_expected_variables() is None and not stale and "expected_variables_stale" not in meta, (stale, meta)
    else:
        expected = "9.9.9" if check == "version-stale" else "does not answer"
        line = match.stale_expected_variables()
        assert expected in line and "test/probe-environment.py settings" in line, line
        assert len(stale) == 1 and stale[0]["detail"] == line, stale
        assert meta["expected_variables_stale"] == line, meta
PY
}

echo "launch environment"
verify "only PASSED_VARIABLES reach claude --bg, not the launcher's other variables" launch_environment_check passed
verify "a process born with a variable of its own → its name, never its value" launch_environment_check foreign
verify "a process born with the launch environment → empty list" launch_environment_check clean
verify "a process that no longer exists → None" launch_environment_check gone
verify "claude --version on the measured version → no line, in the loop log or in meta.json" launch_environment_check version-current
verify "claude --version on another version → one stale row in the loop log and the field in meta.json" launch_environment_check version-stale
verify "claude --version that does not answer → the same line, once" launch_environment_check version-mute

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

echo "loop: delivery of an escalation"
new_state deliver script
start_loop
"$LOOP" switch lead on > /dev/null
"$LOOP" switch worker on > /dev/null
"$LOOP" spawn lead "spawn worker escalate uno|exit" > /dev/null
verify "launcher alive → delivered to it (lead-1), rule launcher" \
  wait_for 10 rows_are 1 event=deliver position=lead session=lead-1 reason=launcher
verify "… lead-1 reads it" wait_for 5 rows_are 2 event=received session=lead-1
"$LOOP" spawn lead "spawn worker sleep 2|escalate due|exit
exit" > /dev/null
verify "… the launcher lead-2 exits before the escalation" wait_for 10 rows_are 1 event=closed session=lead-2
verify "launcher dead, another live session → delivered to lead-1, rule alive-session" \
  wait_for 10 rows_are 1 event=deliver position=lead session=lead-1 reason=alive-session
verify "… lead-1 reads it" wait_for 5 rows_are 3 event=received session=lead-1
kill_session lead-1
verify "… lead-1 dies" wait_for 5 rows_are 1 event=closed session=lead-1
"$LOOP" spawn worker "escalate tre|exit" > /dev/null
verify "no live session → the loop spawns lead-3, reason escalation" \
  wait_for 10 rows_are 1 event=spawn position=lead session=lead-3 reason=escalation
verify "… delivered to lead-3, rule spawn" rows_are 1 event=deliver position=lead session=lead-3 reason=spawn
verify "… lead-3 reads it" wait_for 5 rows_are 1 event=received session=lead-3
message_for lead lead-2 "per una sessione chiusa"
verify "a message for a closed session → undelivered, then delivered to a live one" \
  wait_for 5 rows_are 1 event=undelivered session=lead-2 reason=session-closed
verify "… to lead-3, rule alive-session" wait_for 5 rows_are 1 event=deliver session=lead-3 reason=alive-session
"$LOOP" switch lead off > /dev/null
"$LOOP" spawn worker "escalate quattro|exit" > /dev/null
verify "escalation toward a position switched off → held in the mailbox" \
  wait_for 10 rows_are 1 event=held position=lead reason=switched-off
"$LOOP" switch lead on > /dev/null
verify "… delivered when it is switched on" wait_for 5 rows_are 2 event=deliver session=lead-3 reason=alive-session
forge human escalate '{"text": "dall'"'"'umano"}' > /dev/null
verify "an escalation from the human → rejected, not-a-session" wait_for 5 rows_are 1 event=rejected reason=not-a-session
stop_loop

echo "loop: cap"
new_state cap script
start_loop
"$LOOP" switch lead on > /dev/null
for _ in 1 2 3; do "$LOOP" spawn lead > /dev/null; done
verify "three spawns on lead with cap 2 → two spawn rows" wait_for 5 rows_are 2 event=spawn position=lead
verify "… the third queued, reason cap" rows_are 1 event=queued position=lead reason=cap
verify "… two live sessions" bash -c "session_alive() { :; }; [[ \$(python3 -c \"import json; print(sum(1 for e in json.load(open('$STATE/loop/sessions.json'))['sessions'].values() if e['position'] == 'lead' and e['status'] == 'alive'))\") -eq 2 ]]"
kill_session lead-1
verify "a session closes → the queued spawn runs" wait_for 10 rows_are 3 event=spawn position=lead
stop_loop

echo "loop: budget"
new_state budget script
start_loop
"$LOOP" switch lead on > /dev/null
"$LOOP" spawn lead "spend 600" > /dev/null
"$LOOP" spawn lead > /dev/null
verify "lead-1 spends 600" wait_for 5 grep -qx 600 "$STATE/loop/work/lead-1/tokens.txt"
verify "… lead-2 alive, waiting in its mailbox" wait_for 5 session_alive lead-2
# The loop paused between two cycles: lead-2 reads its message and spends while the loop does not
# look. At the next cycle the halt hands the stops to the workers and the loop notices the read; the
# closed rows come when the stops are applied, and read_ts says that the read came before them.
kill -STOP "$LOOP_PID"
message_for lead lead-2 "spend 600"
verify "with the loop paused, lead-2 reads its message and spends 600" \
  wait_for 5 grep -qx 600 "$STATE/loop/work/lead-2/tokens.txt"
kill -CONT "$LOOP_PID"
verify "1200 tokens over a budget of 1000 → halt row, reason budget" \
  wait_for 10 rows_are 1 event=halt position=lead reason=budget
verify "… the switch of lead is off" switch_is lead False
verify "… both sessions closed at once, reason halt-budget" \
  wait_for 3 rows_are 2 event=closed position=lead reason=halt-budget
verify "… and their processes are gone" bash -c "! kill -0 $(session_field lead-1 pid) 2>/dev/null && ! kill -0 $(session_field lead-2 pid) 2>/dev/null"
verify "the read of lead-2 has read_ts earlier than the ts of its closed row" \
  wait_for 3 read_before_close lead-2
verify "every received row carries read_ts, not later than its ts; ts in write order" log_times_consistent
"$LOOP" switch lead on > /dev/null
verify "switched on again with the same budget → halted again" rows_are 2 event=halt position=lead reason=budget
verify "… the switch stays off" switch_is lead False
python3 - "$STATE/grants/lead.json" <<'PY2'
import json, sys
grant = json.load(open(sys.argv[1])); grant["budget_tokens"] = 5000
open(sys.argv[1], "w").write(json.dumps(grant))
PY2
verify "the budget raised in the grant → grants-loaded" wait_for 10 rows_are 2 event=grants-loaded
"$LOOP" switch lead on > /dev/null
sleep 1
verify "… switched on, it stays on" switch_is lead True
verify "… no new halt" rows_are 2 event=halt position=lead
# The loop paused again: lead-3 reads a message and exits while the loop does not look. At the next
# cycle the loop finds its process gone before it notices the read, so the received row comes after
# the closed row, and only read_ts says that the read came first.
"$LOOP" spawn lead > /dev/null
verify "lead-3 alive" wait_for 5 session_alive lead-3
LEAD3_PID=$(session_field lead-3 pid)
kill -STOP "$LOOP_PID"
message_for lead lead-3 "exit"
verify "with the loop paused, lead-3 reads its message and exits" wait_for 5 process_ended "$LEAD3_PID"
kill -CONT "$LOOP_PID"
verify "the read of lead-3 gets its received row after the closed row of lead-3" \
  wait_for 3 read_logged_after_close lead-3
verify "… with read_ts earlier than the ts of the closed row" read_before_close lead-3
stop_loop

echo "loop: the loop dies, the sessions do not"
new_state restart script
start_loop
"$LOOP" switch root on > /dev/null
"$LOOP" switch lead on > /dev/null
"$LOOP" spawn lead > /dev/null
"$LOOP" spawn lead > /dev/null
verify "three live sessions before the crash" wait_for 5 rows_are 3 event=spawn
ROOT_BEFORE=$(identity root-1)
LEAD_BEFORE=$(identity lead-1)
"$LOOP" --cycle "$CYCLE" --no-notify > "$STATE/second.out" 2>&1
verify "a second loop on the same state root → exit 2" test $? -eq 2
verify "… it says that another loop runs" grep -q "another loop already runs" "$STATE/second.out"
kill -9 "$LOOP_PID"
wait "$LOOP_PID" 2>/dev/null
LOOP_PID=
verify "kill -9 of the loop: root-1 alive, same pid and starttime" test "$(identity root-1)" == "$ROOT_BEFORE"
verify "… lead-1 alive, same pid and starttime" test "$(identity lead-1)" == "$LEAD_BEFORE"
kill_session lead-2
forge human switch '{"position": "root", "on": true}' > /dev/null
message_for root root-1 "sleep 0"
verify "while the loop is down, root-1 reads a message" \
  wait_for 5 bash -c "ls '$STATE/loop/mailboxes/root/received/' | grep -q json"
SPAWNS=$(rows event=spawn)
start_loop
sleep 3
verify "restart: a recovered row for root-1" rows_are 1 event=recovered session=root-1
verify "… and for lead-1" rows_are 1 event=recovered session=lead-1
verify "… lead-2, dead meanwhile, closed with reason dead-at-restart" \
  rows_are 1 event=closed session=lead-2 reason=dead-at-restart
verify "… 0 new spawns" rows_are "$SPAWNS" event=spawn
verify "… the switch request deposited while it was down → rejected, stale" rows_are 1 event=rejected reason=stale
verify "… every switch off" bash -c "! grep -q '\"on\": true' '$STATE/loop/switches.json'"
verify "… the read made while it was down gets its row, reason before-start" \
  rows_are 1 event=received session=root-1 reason=before-start
verify "… its read_ts earlier than the start row of the new loop" log_check before-start
verify "… once, and the reads logged before the crash are not repeated" \
  python3 -c "import json, sys, collections; c = collections.Counter(json.loads(l)['detail'] for l in open(sys.argv[1]) if json.loads(l)['event'] == 'received'); assert max(c.values()) == 1, c" \
  "$STATE/registry/loop.jsonl"
verify "… the sessions untouched: same pid and starttime" \
  bash -c "[[ '$(identity root-1)' == '$ROOT_BEFORE' && '$(identity lead-1)' == '$LEAD_BEFORE' ]]"
message_for root root-1 "sleep 0"
verify "an adopted session still reads what the new loop delivers" \
  wait_for 5 rows_are 1 event=received session=root-1 reason=None
kill -TERM "$LOOP_PID"
wait "$LOOP_PID" 2>/dev/null
verify "SIGTERM: the loop exits with a stop row" rows_are 1 event=stop reason=sigterm
verify "… and the sessions stay alive" bash -c "[[ '$(identity root-1)' == '$ROOT_BEFORE' && '$(identity lead-1)' == '$LEAD_BEFORE' ]]"
verify "two loops, one log: read_ts on every received row, ts in write order" log_times_consistent
LOOP_PID=
kill_state "$STATE"

echo "loop: from the root to the human"
new_state root script
start_loop
"$LOOP" switch root on > /dev/null
verify "root-1 spawned by the switch" wait_for 5 rows_are 1 event=spawn session=root-1
message_for root root-1 "escalate senza pacchetto
escalate
escalate --blocked 'manca una scelta' --alternative A --alternative B --recommendation A --default A --deadline domani scegliere fra A e B"
verify "escalation from the root without the package → rejected, incomplete-package" \
  wait_for 10 rows_are 1 event=rejected from=root-1 reason=incomplete-package
verify "an empty escalation → rejected, empty" wait_for 10 rows_are 1 event=rejected from=root-1 reason=empty
verify "with alternatives, recommendation and default → an interpellation row" \
  wait_for 10 rows_are 1 event=interpellation position=root session=root-1
verify "… the file carries the package and stays open" python3 - "$STATE/loop/interpellations" <<'PY2'
import json, sys
from pathlib import Path
[path] = Path(sys.argv[1]).glob("[0-9]*.json")
data = json.loads(path.read_text())
assert data["alternatives"] == ["A", "B"] and data["recommendation"] == "A" and data["default"] == "A", data
assert data["deadline"] == "domani" and data["blocked"] == "manca una scelta" and data["status"] == "open", data
PY2
verify "… no session opens for it: one spawn row in all" rows_are 1 event=spawn
verify "… the status counts it" python3 -c \
  "import json, sys; assert json.load(open(sys.argv[1]))['interpellations_open'] == 1" "$STATE/loop/status.json"
stop_loop

# <editor-fold desc="Concurrency with a fake claude">
fake_claude() { # <launch_s> <resume_s> <stop_s> <agents_s> [tokens]: test/fake-claude.py as claude in $STATE/fake-bin
  mkdir -p "$STATE/fake-bin" "$STATE/home"
  cp "$ENGINE/test/fake-claude.py" "$STATE/fake-bin/claude"
  chmod +x "$STATE/fake-bin/claude"
  python3 - "$ENGINE/bin" "$STATE/fake-bin/config.json" "$@" <<'PY'
import json, sys
sys.path.insert(0, sys.argv[1])
import match
launch_s, resume_s, stop_s, agents_s = (float(value) for value in sys.argv[3:7])
tokens = int(sys.argv[7]) if len(sys.argv) > 7 else 0
json.dump({"launch_s": launch_s, "resume_s": resume_s, "stop_s": stop_s, "agents_s": agents_s, "tokens": tokens,
           "version": match.EXPECTED_VARIABLES_CLAUDE_VERSION, "request": f"{sys.argv[1]}/request"},
          open(sys.argv[2], "w"))
PY
}

fake_set() { # <key> <value>: one setting of the fake claude, read again at its next command
  python3 - "$STATE/fake-bin/config.json" "$1" "$2" <<'PY'
import json, sys
config = json.load(open(sys.argv[1])); config[sys.argv[2]] = float(sys.argv[3])
json.dump(config, open(sys.argv[1], "w"))
PY
}

grant_budget() { # <position> <tokens>
  python3 - "$STATE/grants/$1.json" "$2" <<'PY'
import json, sys
grant = json.load(open(sys.argv[1])); grant["budget_tokens"] = int(sys.argv[2])
open(sys.argv[1], "w").write(json.dumps(grant))
PY
}

deposit_spawns() { # <count> <position>: spawns of the human deposited with the loop paused, so in one cycle
  kill -STOP "$LOOP_PID"
  for _ in $(seq "$1"); do forge human spawn "{\"position\": \"$2\"}" > /dev/null; done
  kill -CONT "$LOOP_PID"
}

fake_ready() { # <session>: its fake process has run its first `request recv`, so a message now waits for a wake
  local short_id
  short_id=$(session_field "$1" short_id) && [[ $(cat "$STATE/fake-bin/sessions/$short_id.ready" 2>/dev/null) == \
    "$(session_field "$1" pid)" ]]
}

request_id() { # <file>: the request id that a command of the loop printed
  grep -o 'r-[0-9a-f]*' "$1" | head -1
}

heartbeat_gap() { # <seconds>: the longest time, in seconds, the heartbeat of status.json stays the same
  python3 - "$STATE/loop/status.json" "$1" <<'PY'
import json, sys, time
path, seconds = sys.argv[1], float(sys.argv[2])
end = time.monotonic() + seconds
last, changed, longest = None, time.monotonic(), 0.0
while time.monotonic() < end:
    try:
        beat = json.load(open(path))["heartbeat"]
    except (OSError, ValueError, KeyError):
        beat = last
    now = time.monotonic()
    if beat != last:
        longest = max(longest, now - changed) if last is not None else longest
        last, changed = beat, now
    time.sleep(0.02)
print(f"{max(longest, time.monotonic() - changed):.3f}")
PY
}

concurrency_check() { # <check> [arguments]: the checks on the files of the loop, written below in Python
  python3 - "$STATE" "$CYCLE" "$@" <<'PY'
import json, sys
from datetime import datetime
from pathlib import Path
state, cycle, check, arguments = Path(sys.argv[1]), float(sys.argv[2]), sys.argv[3], sys.argv[4:]
at = lambda text: datetime.fromisoformat(text).timestamp()
rows = [json.loads(line) for line in (state / "registry" / "loop.jsonl").read_text().splitlines() if line.strip()]
sessions = json.loads((state / "loop" / "sessions.json").read_text())["sessions"]
fake = [json.loads(path.read_text()) for path in sorted((state / "fake-bin" / "sessions").glob("*.json"))] \
    if (state / "fake-bin" / "sessions").is_dir() else []
done = {json.loads(path.read_text())["id"]: json.loads(path.read_text())
        for path in (state / "loop" / "requests" / "done").glob("[0-9]*.json")}
pending = {json.loads(path.read_text())["id"]: json.loads(path.read_text())
           for path in (state / "loop" / "requests").glob("[0-9]*.json")}

def spawns(position):
    found = [row for row in rows if row["event"] == "spawn" and row["position"] == position]
    assert all(row.get("started_ts") for row in found), found
    return found

if check == "together":
    # The launches deposited in one cycle end within twice the duration of one launch, from the first
    # decision to the last spawn row; they also start together.
    position = arguments[0]
    found = spawns(position)
    longest = max(at(row["ts"]) - at(row["started_ts"]) for row in found)
    first = min(at(row["ts"]) for row in rows if row["event"] == "launching" and row["position"] == position)
    span = max(at(row["ts"]) for row in found) - first
    starts = [at(row["started_ts"]) for row in found]
    assert span <= 2 * longest, (span, longest)
    assert max(starts) - min(starts) < longest / 2, starts
    print(f"span {span:.2f} s, one launch {longest:.2f} s")
elif check == "in-a-row":
    # With one worker the launches run one after the other: each starts at least `delay` after the
    # previous one, and all of them end within four durations of one plus two cycles.
    position, delay = arguments[0], float(arguments[1])
    found = spawns(position)
    longest = max(at(row["ts"]) - at(row["started_ts"]) for row in found)
    first = min(at(row["ts"]) for row in rows if row["event"] == "launching" and row["position"] == position)
    span = max(at(row["ts"]) for row in found) - first
    starts = sorted(at(row["started_ts"]) for row in found)
    assert all(later - earlier >= 0.9 * delay for earlier, later in zip(starts, starts[1:])), starts
    assert span <= len(found) * longest + 2 * cycle, (span, longest)
    print(f"span {span:.2f} s, one launch {longest:.2f} s")
elif check == "decided-within":
    request = done[arguments[0]]
    elapsed = at(request["decided"]) - at(request["ts"])
    assert elapsed <= float(arguments[1]), elapsed
    print(f"decided after {elapsed:.3f} s")
elif check == "no-lost-writes":
    # Every session the fake claude launched is alive in the registry; every spawn request is decided
    # as spawned, with one outcome row in the log.
    position, count = arguments[0], int(arguments[1])
    alive = {entry["short_id"]: entry for entry in sessions.values()
             if entry["position"] == position and entry["status"] == "alive"}
    assert len(fake) == count and sorted(state["id"] for state in fake) == sorted(alive), (len(fake), len(alive))
    asked = [request for request in done.values() if request["kind"] == "spawn" and request.get("position") == position]
    assert len(asked) == count and not pending, (len(asked), list(pending))
    assert all(request["outcome"] == "spawned" and request["session"] in sessions for request in asked), asked
    outcomes = ("spawn", "spawn-failed", "rejected")
    for request in asked:
        found = [row for row in rows if row["event"] in outcomes and row["request"] == request["id"]]
        assert len(found) == 1, (request["id"], found)
    print(f"{count} sessions, {count} requests, one outcome row each")
elif check == "one-session":
    # One session for the request: in the registry, and among the sessions of the fake claude.
    request_id = arguments[0]
    mine = [entry for entry in sessions.values() if entry["request"] == request_id]
    names = {entry.get("name") for entry in mine}
    assert len(mine) == 1 and mine[0]["status"] == "alive", mine
    assert [state for state in fake if state["name"] in names] and len(fake) == 1, fake
    assert done[request_id]["outcome"] == "spawned" and done[request_id]["session"] == mine[0]["id"], done[request_id]
elif check == "session":
    # <session> <status> [operation kind]
    entry = sessions[arguments[0]]
    assert entry["status"] == arguments[1], entry
    assert (entry.get("operation") or {}).get("kind") == (arguments[2] if len(arguments) > 2 else None), entry
elif check == "queued":
    assert pending[arguments[0]]["queued"]["reason"] == arguments[1] and "launching" not in pending[arguments[0]], pending
elif check == "unmarked":
    assert not any(mark in pending[arguments[0]] for mark in ("queued", "launching")), pending
elif check == "fake-pid":
    # The registry carries the pid that the fake claude gives the session now, and it is alive.
    entry = sessions[arguments[0]]
    [mine] = [state for state in fake if state["id"] == entry["short_id"]]
    assert entry["pid"] == mine["pid"] and entry["pid"] != int(arguments[1]), (entry["pid"], mine["pid"])
    assert Path(f"/proc/{entry['pid']}").exists()
elif check == "after":
    # <event> <session> <event> <session>: the first row comes before the second
    first = next(index for index, row in enumerate(rows) if row["event"] == arguments[0] and str(row["session"]) == arguments[1])
    second = next(index for index, row in enumerate(rows) if row["event"] == arguments[2] and str(row["session"]) == arguments[3])
    assert first < second, (first, second)
else:
    raise SystemExit(f"unknown check {check}")
PY
}

if [[ ${#CONCURRENCY_ARGS[@]} -eq 0 ]]; then

echo "loop: concurrency — the cycle does not stop (fake claude, 5 s for every command, N = 1)"
new_state still fake
fake_claude 5 5 5 0
start_loop 1
verify "the concurrency in use is in status.json" python3 -c \
  "import json, sys; assert json.load(open(sys.argv[1]))['concurrency'] == 1" "$STATE/loop/status.json"
"$LOOP" switch lead on > /dev/null
"$LOOP" spawn lead > "$STATE/spawn.out"
verify "a spawn is accepted at once: the command says lead-1 is launching" grep -q "lead-1 in lancio" "$STATE/spawn.out"
verify "… lead-1 in the registry, status launching, operation launch" concurrency_check session lead-1 launching launch
verify "… the request pending with the launching mark" bash -c "grep -q '\"launching\"' '$STATE'/loop/requests/[0-9]*.json"
heartbeat_gap 4 > "$STATE/gap-launch.txt" &
SAMPLER=$!
sleep 1
"$LOOP" switch worker on > "$STATE/switch.out"
wait "$SAMPLER"
verify "during the launch the heartbeat never stays the same more than 2 cycles ($(cat "$STATE/gap-launch.txt") s)" \
  python3 -c "import sys; assert float(open(sys.argv[1]).read()) <= 2 * $CYCLE" "$STATE/gap-launch.txt"
verify "… a request deposited during the launch is decided within 2 cycles" \
  concurrency_check decided-within "$(request_id "$STATE/switch.out")" "$(python3 -c "print(2 * $CYCLE)")"
verify "the launch ends: a spawn row with started_ts" wait_for 10 rows_are 1 event=spawn session=lead-1
verify "… lead-1 has run its first recv" wait_for 25 fake_ready lead-1
fake_set agents_s 5
fake_set stop_s 1
fake_set resume_s 1
message_for lead lead-1 "sveglia"
verify "a message for lead-1 → the read of the session states runs as an operation" \
  wait_for 5 python3 -c "import json, sys; assert json.load(open(sys.argv[1]))['operations']['running'] == 1" "$STATE/loop/status.json"
heartbeat_gap 3 > "$STATE/gap-agents.txt" &
SAMPLER=$!
sleep 0.5
"$LOOP" switch worker off > "$STATE/switch.out"
wait "$SAMPLER"
verify "during a claude agents --json of 5 s the heartbeat never stays the same more than 2 cycles ($(cat "$STATE/gap-agents.txt") s)" \
  python3 -c "import sys; assert float(open(sys.argv[1]).read()) <= 2 * $CYCLE" "$STATE/gap-agents.txt"
verify "… a request deposited meanwhile is decided within 2 cycles" \
  concurrency_check decided-within "$(request_id "$STATE/switch.out")" "$(python3 -c "print(2 * $CYCLE)")"
fake_set agents_s 0
verify "… then lead-1 is woken: waking row, then wake row" wait_for 20 rows_are 1 event=wake session=lead-1
verify "… and it reads its message" wait_for 10 rows_are 1 event=received session=lead-1
stop_loop

echo "loop: concurrency — N launches together"
new_state together fake
fake_claude 5 5 5 0
start_loop 4
"$LOOP" switch lead on > /dev/null
deposit_spawns 4 lead
verify "N = 4: four spawns deposited in one cycle → four spawn rows" wait_for 20 rows_are 4 event=spawn position=lead
verify "… within twice the duration of one launch, started together" concurrency_check together lead
stop_loop
new_state in-a-row fake
fake_claude 5 5 5 0
start_loop 1
"$LOOP" switch lead on > /dev/null
deposit_spawns 4 lead
verify "N = 1: four spawns deposited in one cycle → four spawn rows" wait_for 40 rows_are 4 event=spawn position=lead
verify "… one at a time, within four durations of one launch plus 2 cycles" concurrency_check in-a-row lead 5
stop_loop

echo "loop: concurrency — no write lost"
new_state many fake
fake_claude 1 1 1 0
start_loop 4
"$LOOP" switch lead on > /dev/null
deposit_spawns 20 lead
verify "N = 4, 20 spawns on a position with cap 20 → 20 spawn rows" wait_for 60 rows_are 20 event=spawn position=lead
verify "… every session launched in sessions.json, every request with one outcome row" \
  concurrency_check no-lost-writes lead 20
verify "… the log in write order" log_times_consistent
stop_loop

echo "loop: concurrency — withdrawn on a switch off, and on SIGTERM"
new_state withdraw fake
fake_claude 5 5 5 0
start_loop 1
"$LOOP" switch lead on > /dev/null
deposit_spawns 2 lead
verify "N = 1, two spawns: lead-1 launching, lead-2 waiting for a worker" wait_for 5 rows_are 2 event=launching position=lead
"$LOOP" switch lead off > /dev/null
verify "switch lead off → the launch not started is withdrawn, reason switched-off" \
  wait_for 5 rows_are 1 event=withdrawn session=lead-2 reason=switched-off
verify "… its request queued again, reason switched-off" wait_for 5 rows_are 1 event=queued position=lead reason=switched-off
verify "… the launch already running reaches its end" wait_for 10 rows_are 1 event=spawn session=lead-1
"$LOOP" switch lead on > /dev/null
verify "switched on again, the withdrawn request launches" wait_for 5 rows_are 3 event=launching position=lead
sleep 1
kill -TERM "$LOOP_PID"
wait "$LOOP_PID"
verify "SIGTERM during a launch: exit 0" test $? -eq 0
LOOP_PID=
verify "… the loop waits for the running launch: spawn row, then stop row" concurrency_check after spawn lead-2 stop None
verify "… lead-2 alive in the registry" concurrency_check session lead-2 alive
kill_state "$STATE"

echo "loop: concurrency — kill -9 during a launch"
new_state kill-born fake
fake_claude 5 5 5 0
start_loop 2
"$LOOP" switch lead on > /dev/null
"$LOOP" spawn lead > "$STATE/spawn.out"
sleep 1
kill -9 "$LOOP_PID"
wait "$LOOP_PID" 2>/dev/null
verify "kill -9 while lead-1 launches: the registry keeps it launching" concurrency_check session lead-1 launching launch
start_loop 2
verify "restart: lead-1, born from the launch the loop left, adopted with a recovered row" \
  wait_for 20 rows_are 1 event=recovered session=lead-1 reason=launch-interrupted
verify "… its request decided as spawned, one session for it" wait_for 5 concurrency_check one-session "$(request_id "$STATE/spawn.out")"
verify "… no spawn row: the launch was the previous loop's" rows_are 0 event=spawn
stop_loop
new_state kill-unborn fake
fake_claude 5 5 5 0
start_loop 2
"$LOOP" switch lead on > /dev/null
"$LOOP" spawn lead > "$STATE/spawn.out"
sleep 1
kill -9 "$LOOP_PID"
pkill -9 -f "antfarm loop · lead · lead-1 · "
wait "$LOOP_PID" 2>/dev/null
start_loop 2
verify "kill -9 of the loop and of the launch: at restart lead-1 is not born → withdrawn, reason not-born" \
  wait_for 30 rows_are 1 event=withdrawn session=lead-1 reason=not-born
verify "… its request pending again; every switch is off, so it waits in the queue" \
  wait_for 5 concurrency_check queued "$(request_id "$STATE/spawn.out")" switched-off
"$LOOP" switch lead on > /dev/null
verify "switched on, the request launches once more" \
  wait_for 15 rows_are 1 event=spawn request="$(request_id "$STATE/spawn.out")"
verify "… one session for it" wait_for 5 concurrency_check one-session "$(request_id "$STATE/spawn.out")"
stop_loop

echo "loop: concurrency — kill -9 during a wake, and during a stop"
new_state kill-wake fake
fake_claude 1 5 5 0
start_loop 2
"$LOOP" switch lead on > /dev/null
"$LOOP" spawn lead > /dev/null
verify "lead-1 alive" wait_for 10 rows_are 1 event=spawn session=lead-1
verify "… and has run its first recv" wait_for 25 fake_ready lead-1
PID_BEFORE=$(session_field lead-1 pid)
message_for lead lead-1 "sveglia"
verify "a message for lead-1 → waking row" wait_for 10 rows_are 1 event=waking session=lead-1
sleep 6
kill -9 "$LOOP_PID"
wait "$LOOP_PID" 2>/dev/null
verify "kill -9 while the resume runs: the registry keeps lead-1 alive in its wake" concurrency_check session lead-1 alive wake
start_loop 2
verify "restart: lead-1 recovered, reason wake-interrupted" wait_for 20 rows_are 1 event=recovered session=lead-1 reason=wake-interrupted
verify "… with the new pid of the resume, alive" concurrency_check fake-pid lead-1 "$PID_BEFORE"
verify "… and it reads its message" wait_for 25 rows_are 1 event=received session=lead-1
stop_loop
new_state kill-stop fake
fake_claude 1 1 5 0 600
grant_budget lead 1000
start_loop 2
"$LOOP" switch lead on > /dev/null
"$LOOP" spawn lead > /dev/null
"$LOOP" spawn lead > /dev/null
verify "two fake sessions spend 1200 tokens over a budget of 1000 → halt, two stops handed over" \
  wait_for 15 rows_are 2 event=stopping position=lead
sleep 1
kill -9 "$LOOP_PID"
wait "$LOOP_PID" 2>/dev/null
verify "… lead-1 alive, operation stop" concurrency_check session lead-1 alive stop
verify "… lead-2 alive, operation stop" concurrency_check session lead-2 alive stop
start_loop 2
verify "restart: the two stops repeated" wait_for 10 rows_are 4 event=stopping position=lead
verify "… the two sessions closed with reason halt-budget" wait_for 15 rows_are 2 event=closed position=lead reason=halt-budget
stop_loop

echo "loop: concurrency — a stop during a wake"
new_state stop-wake fake
fake_claude 1 5 5 0 600
grant_budget lead 1000
start_loop 4
"$LOOP" switch lead on > /dev/null
"$LOOP" spawn lead > /dev/null
verify "lead-1 alive, 600 tokens" wait_for 10 rows_are 1 event=spawn session=lead-1
verify "… and has run its first recv" wait_for 25 fake_ready lead-1
message_for lead lead-1 "sveglia"
verify "a message for lead-1 → waking row" wait_for 10 rows_are 1 event=waking session=lead-1
"$LOOP" spawn lead > /dev/null
verify "lead-2 spends 600 more during the wake → halt" wait_for 10 rows_are 1 event=halt position=lead reason=budget
verify "… the stop of lead-1 waits for its wake" rows_are 1 event=stopping session=lead-1 "detail=after the wake in progress"
verify "… lead-1 closed with reason halt-budget, never resumed" \
  wait_for 15 rows_are 1 event=closed session=lead-1 reason=halt-budget
verify "… no wake row for lead-1" rows_are 0 event=wake session=lead-1
verify "… lead-2 closed too" wait_for 10 rows_are 1 event=closed session=lead-2 reason=halt-budget
stop_loop

fi
# </editor-fold>

echo
if [[ $failed -gt 0 ]]; then
  echo "loop tests: $failed FAILED"
  exit 1
fi
echo "loop tests: all ok"
