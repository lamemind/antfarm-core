#!/usr/bin/env bash
# Acceptance test of the observability harness with the test client. No LLM.
#
#   antfarm-core/test/acceptance-harness.sh
#
# replay — replays every T42 match at speed 0 and compares the events the client receives with the
#   sources: one message event for every row of the match in the message log ($M), with the same
#   indices and in ts order; one launch event for every node that nodes.json marks as launched; every
#   event valid for validate-events.py
# off-bus actions — in the replay of the match that has some, the node-closed of every node carries
#   the list written by the director in nodes.json, and the harness never names the transcripts
# changing graph — in the replay of the dry-run corpse, match-opened carries version 1, a single
#   graph-changed carries the delta and the row of graphs/v2.json intact, and arrives before the
#   launch of D and before the first message with version 2
# read only — sha256 of the match registry ($P) and of $M equal before and after the replays
# local port — with the harness on, ss -ltn shows the port bound only to 127.0.0.1, and a
#   connection with a foreign origin is rejected
# live dry run — on a copy of the engine in a temporary folder, with the state root in a sibling
#   folder of the copy, like negative-tests.sh: harness on, client listening, director --dry-run;
#   the client receives every row the match adds to the copy's $M, one node-closed for every node
#   after its last send, and prints the delay between the ts of the row and the arrival (median,
#   max); then a dry-run corpse match, with graph-changed before the first message with version 2
#
# It reads the reference matches, whose ids are written below, the registry and the log from the
# state root of whoever launches it (ANTFARM_STATE_DIR): the test passes only where that state exists.
#
#   ANTFARM_STATE_DIR=<project> antfarm-core/test/acceptance-harness.sh
#
# Exits 1 if a check fails, 2 without ANTFARM_STATE_DIR.
set -uo pipefail

ANTFARM=$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)
if [[ -z ${ANTFARM_STATE_DIR:-} ]]; then
  echo "ANTFARM_STATE_DIR is not set: the test replays the reference matches from the state root of a project" >&2
  exit 2
fi
STATE=$(cd "$ANTFARM_STATE_DIR" && pwd) || exit 2
export ANTFARM_STATE_DIR="$STATE"
P="$STATE/registry/matches.tsv"
M="$STATE/registry/messages.jsonl"
T42_MATCHES=(
  2026-10-26-120840-contro-prova-bus
  2026-10-26-121310-telefono-a-secco
  2026-10-26-121427-telefono-copia
  2026-10-26-121519-telefono-riformula
)
ACTIONS_MATCH=2026-10-27-115351-telefono-riformula
CORPSE_MATCH=2026-11-08-114717-cadavere-a-secco
TMP=$(mktemp -d)
HARNESS_PID=""
trap '[[ -n $HARNESS_PID ]] && kill "$HARNESS_PID" 2>/dev/null; rm -rf "$TMP"' EXIT

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

start() { # <output file> <harness command...>: starts it in the background, reads the port
  local output=$1; shift
  "$@" --port 0 > "$output" 2> "$output.err" &
  HARNESS_PID=$!
  for _ in $(seq 200); do grep -q 'http://' "$output" 2>/dev/null && break; sleep 0.1; done
  PORT=$(grep -oP '127\.0\.0\.1:\K[0-9]+' "$output")
}

stop() {
  kill "$HARNESS_PID" 2>/dev/null
  wait "$HARNESS_PID" 2>/dev/null
  HARNESS_PID=""
}

equal() { [[ "$1" == "$2" ]]; }

echo "read only: fingerprints before the replays"
FINGERPRINT_BEFORE=$(sha256sum "$P" "$M")
printf '%s\n' "$FINGERPRINT_BEFORE" | sed 's/^/  /'

for match in "${T42_MATCHES[@]}"; do
  echo "replay $match"
  start "$TMP/harness-$match.out" "$ANTFARM/bin/harness.py" replay "$match" --speed 0
  if [[ $match == "${T42_MATCHES[0]}" ]]; then
    LISTENING=$(ss -ltnH "sport = :$PORT" | awk '{print $4}')
    printf '  ss -ltn: %s\n' "$LISTENING"
    verify "port $PORT listening" test -n "$LISTENING"
    verify "port bound only to 127.0.0.1, never to 0.0.0.0 or ::" \
      bash -c "! grep -qv '^127\\.0\\.0\\.1:$PORT\$' <<< '$LISTENING'"
    FOREIGN=$("$ANTFARM/bin/listen.py" --port "$PORT" --origin http://example.invalid --timeout 5)
    verify "page from another origin rejected with 403" \
      jq -e '.end == "rejected" and .closure.code == 403' <<< "$FOREIGN"
  fi
  EVENTS="$TMP/$match.jsonl"
  SUMMARY=$("$ANTFARM/bin/listen.py" --port "$PORT" --events "$EVENTS" --timeout 60)
  verify "the channel closes by itself at the end of the replay" jq -e '.end == "channel-closed"' <<< "$SUMMARY"
  stop
  verify "validate-events.py exits 0 on every event received" "$ANTFARM/bin/validate-events.py" "$EVENTS"
  EXPECTED_ROWS=$(jq -c --arg p "$match" 'select(.match == $p) | input_line_number' "$M" | jq -sc .)
  RECEIVED_INDICES=$(jq -c '[.message_indices[][1]]' <<< "$SUMMARY")
  printf '  rows in $M: %s · message events: %s\n' "$(jq length <<< "$EXPECTED_ROWS")" "$(jq length <<< "$RECEIVED_INDICES")"
  verify "one message event for every row of the match in \$M, same indices" \
    equal "$(jq -c sort <<< "$RECEIVED_INDICES")" "$EXPECTED_ROWS"
  verify "messages in ts order" jq -e '.messages_in_ts_order' <<< "$SUMMARY"
  EXPECTED_LAUNCHED=$(jq -c '[.nodes | to_entries[] | select(.value.launched) | .key] | sort' "$STATE/matches/$match/nodes.json")
  RECEIVED_LAUNCHED=$(jq -c '[.nodes_launched[][1]] | sort' <<< "$SUMMARY")
  verify "one launch event for every launched node of nodes.json: $EXPECTED_LAUNCHED" \
    equal "$RECEIVED_LAUNCHED" "$EXPECTED_LAUNCHED"
  verify "one match-opened and one match-closed" \
    jq -e '.per_type["lifecycle/match-opened"] == 1 and .per_type["lifecycle/match-closed"] == 1' <<< "$SUMMARY"
  if [[ $match == *-telefono-copia ]]; then
    verify "control match: 10 messages, 5 delivered and 5 received" \
      jq -e '.messages == 10 and .per_type["message/delivered"] == 5 and .per_type["message/received"] == 5' <<< "$SUMMARY"
    verify "control match: A, B, C, D launched" equal "$RECEIVED_LAUNCHED" '["A","B","C","D"]'
  fi
done

echo "replay: the four matches in a single stream"
cat "$TMP"/2026-*.jsonl > "$TMP/all.jsonl"
verify "validate-events.py exits 0 on the whole stream, no message emitted twice" \
  "$ANTFARM/bin/validate-events.py" "$TMP/all.jsonl"

echo "replay $ACTIONS_MATCH: the detail of the off-bus actions"
start "$TMP/harness-actions.out" "$ANTFARM/bin/harness.py" replay "$ACTIONS_MATCH" --speed 0
ACTIONS_EVENTS="$TMP/actions-events.jsonl"
"$ANTFARM/bin/listen.py" --port "$PORT" --events "$ACTIONS_EVENTS" --timeout 60 > /dev/null
stop
verify "validate-events.py exits 0 on every event received" "$ANTFARM/bin/validate-events.py" "$ACTIONS_EVENTS"
closed() { # <node> <jq filter on the list of actions>: the node-closed of the node is a single one and passes the filter
  jq -se --arg node "$1" "[.[] | select(.type == \"node-closed\" and .node == \$node)] | length == 1 and (.[0].row.actions | $2)" "$ACTIONS_EVENTS"
}
verify "node-closed of A: 1 action, error yes" closed A 'length == 1 and .[0].error == true and .[0].tool == "Bash"'
verify "node-closed of B: 1 action, error no" closed B 'length == 1 and .[0].error == false and .[0].outcome == "61"'
verify "node-closed of C and of D: no action" bash -c "$(declare -f closed); ACTIONS_EVENTS='$ACTIONS_EVENTS'; closed C 'type == \"array\" and length == 0' && closed D 'type == \"array\" and length == 0'"
verify "every node-closed carries as many actions as off_bus_actions counts" \
  jq -se '[.[] | select(.type == "node-closed")] | length == 4 and all(.row as $entry | ($entry.actions | type) == "array" and ($entry.actions | length) == $entry.off_bus_actions)' "$ACTIONS_EVENTS"
verify "the list in the event is the one of nodes.json, intact" \
  equal "$(jq -sc '[.[] | select(.type == "node-closed") | {(.node): .row.actions}] | add' "$ACTIONS_EVENTS")" \
  "$(jq -c '[.nodes | to_entries[] | select(.value.closed) | {(.key): .value.actions}] | add' "$STATE/matches/$ACTIONS_MATCH/nodes.json")"
verify "the harness never names the transcripts" equal "$(grep -c '.claude/projects' "$ANTFARM/bin/harness.py")" 0

graph_changed_in_order() { # <events file>: graph-changed before the launch of D and the first message with v2
  jq -se 'to_entries as $e
    | ([$e[] | select(.value.type == "graph-changed") | .key]) as $changes
    | ($changes | length) == 1
      and ([$e[] | select(.value.type == "node-launched" and .value.node == "D") | .key] | all(. > $changes[0]))
      and ([$e[] | select(.value.class == "message" and .value.row.version == 2) | .key] | length > 0 and all(. > $changes[0]))' "$1"
}

echo "replay $CORPSE_MATCH: the changing graph"
start "$TMP/harness-corpse.out" "$ANTFARM/bin/harness.py" replay "$CORPSE_MATCH" --speed 0
CORPSE_EVENTS="$TMP/corpse-events.jsonl"
"$ANTFARM/bin/listen.py" --port "$PORT" --events "$CORPSE_EVENTS" --timeout 60 > /dev/null
stop
verify "validate-events.py exits 0 on every event received" "$ANTFARM/bin/validate-events.py" "$CORPSE_EVENTS"
verify "match-opened carries the graph at version 1, not the last one" \
  jq -se '[.[] | select(.type == "match-opened")] | length == 1 and .[0].row.graph.version == 1 and (.[0].row.graph.nodes | length) == 4' "$CORPSE_EVENTS"
verify "the delta: D joins, C → A goes away, C → D, D → A, D → director appear, version 2" \
  jq -se '[.[] | select(.type == "graph-changed")][0].delta | .version == 2 and ([.nodes_added[].id] == ["D"]) and (.nodes_removed == [])
    and ([.edges_removed[] | [.from, .to]] == [["C","A"]]) and ([.edges_added[] | [.from, .to]] | sort == [["C","D"],["D","A"],["D","director"]])' "$CORPSE_EVENTS"
verify "the row is graphs/v2.json intact, and the ts is the time it took effect" \
  bash -c "test \"\$(jq -cS 'select(.type == \"graph-changed\") | .row' '$CORPSE_EVENTS')\" = \"\$(jq -cS . '$STATE/matches/$CORPSE_MATCH/graphs/v2.json')\" && jq -se '[.[] | select(.type == \"graph-changed\")][0] | .ts == .row.effective' '$CORPSE_EVENTS'"
verify "graph-changed before the launch of D and the first message with v2" graph_changed_in_order "$CORPSE_EVENTS"

echo "read only: fingerprints after the replays"
FINGERPRINT_AFTER=$(sha256sum "$P" "$M")
verify "sha256 of \$P and \$M equal before and after" equal "$FINGERPRINT_BEFORE" "$FINGERPRINT_AFTER"

echo "live, dry-run match on a copy of the engine"
COPY="$TMP/engine"
COPY_STATE="$TMP/state"
mkdir -p "$COPY" "$COPY_STATE"
export ANTFARM_STATE_DIR="$COPY_STATE"
cp -r "$ANTFARM/bin" "$ANTFARM/games" "$ANTFARM/schema" "$COPY/"
[[ -d "$ANTFARM/view" ]] && cp -r "$ANTFARM/view" "$COPY/"
start "$TMP/harness-live.out" "$COPY/bin/harness.py" live
"$COPY/bin/listen.py" --port "$PORT" --until-close --delay --events "$TMP/live.jsonl" --timeout 120 \
  > "$TMP/live-summary.json" &
LISTENER_PID=$!
"$COPY/bin/director.py" telephone --dry-run > "$TMP/director.out" 2>&1
verify "the dry-run director closes the match as succeeded" test $? -eq 0
wait "$LISTENER_PID"
verify "the client stops on match-closed" test $? -eq 0
stop
sed 's/^/  /' "$TMP/harness-live.out.err"
LIVE_MATCH=$(jq -r 'select(.type == "match-opened") | .match' "$TMP/live.jsonl")
LIVE_ROWS=$(jq -c --arg p "$LIVE_MATCH" 'select(.match == $p) | input_line_number' "$COPY_STATE/registry/messages.jsonl" | jq -sc .)
LIVE_INDICES=$(jq -c '[.message_indices[][1]] | sort' "$TMP/live-summary.json")
printf '  match %s · rows in $M: %s · message events: %s\n' "$LIVE_MATCH" \
  "$(jq length <<< "$LIVE_ROWS")" "$(jq length <<< "$LIVE_INDICES")"
verify "every row the match adds to \$M reaches the client, same indices" equal "$LIVE_INDICES" "$LIVE_ROWS"
verify "validate-events.py exits 0 on the live events" "$COPY/bin/validate-events.py" "$TMP/live.jsonl"
verify "A, B, C, D launched" jq -e '[.nodes_launched[][1]] | sort == ["A","B","C","D"]' "$TMP/live-summary.json"
verify "one node-closed for every launched node, written by the director in nodes.json" \
  jq -e '[.nodes_closed[][1]] | sort == ["A","B","C","D"]' "$TMP/live-summary.json"
verify "script node: off_bus_actions and actions null in the entry of the node-closed" \
  jq -se '[.[] | select(.type == "node-closed")] | length == 4 and all(.row.off_bus_actions == null and .row.actions == null and .row.type == "script")' "$TMP/live.jsonl"
verify "every node-closed arrives after the last message sent by that node" \
  jq -se 'to_entries as $e | [$e[] | select(.value.type == "node-closed")] | all(. as $c |
    ([$e[] | select(.value.class == "message" and .value.row.from == $c.value.node) | .key] | max // -1) < $c.key)' "$TMP/live.jsonl"
printf '  delay ts → client: %s\n' "$(jq -c '.delay_ms' "$TMP/live-summary.json")"

echo "live, dry-run corpse on the same copy of the engine"
start "$TMP/harness-corpse-live.out" "$COPY/bin/harness.py" live
"$COPY/bin/listen.py" --port "$PORT" --until-close --events "$TMP/corpse-live.jsonl" --timeout 180 \
  > "$TMP/corpse-live-summary.json" &
LISTENER_PID=$!
"$COPY/bin/director.py" corpse --dry-run > "$TMP/director-corpse.out" 2>&1
verify "the dry-run corpse director closes the match as succeeded" test $? -eq 0
wait "$LISTENER_PID"
verify "the client stops on match-closed" test $? -eq 0
stop
verify "validate-events.py exits 0 on the live events" "$COPY/bin/validate-events.py" "$TMP/corpse-live.jsonl"
verify "live, match-opened carries version 1" \
  jq -se '[.[] | select(.type == "match-opened")] | length == 1 and .[0].row.graph.version == 1' "$TMP/corpse-live.jsonl"
verify "live, graph-changed before the launch of D and the first message with v2" graph_changed_in_order "$TMP/corpse-live.jsonl"

echo
if [[ $failed -gt 0 ]]; then
  echo "HARNESS ACCEPTANCE TEST FAILED: $failed checks"
  exit 1
fi
echo "harness acceptance test: all ok"
