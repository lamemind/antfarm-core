#!/usr/bin/env bash
# Deterministic negative tests of the engine: graph and event validators, view, bus, off-bus actions. No LLM.
#
#   antfarm-core/test/negative-tests.sh
#
# It runs on a copy of bin/, schema/ and games/ in a temporary folder, with the state root
# (ANTFARM_STATE_DIR) pointed at a sibling folder of the copy: mailboxes, matches and message log
# are its own, and the real registry of a project receives no test rows, whatever the environment
# of whoever launches it. It reads no recorded match: it plays here the two dry-run corpse matches
# on which it composes the report, so it exits 0 on any clone of the engine.
# A node is a bash subprocess that registers itself in nodes.json with its own pair
# (pid, starttime), as the director does with the sessions.
# Exits 1 if a negative test fails.
set -uo pipefail

ANTFARM=$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)
TELEPHONE_GRAPH="$ANTFARM/games/telephone/graph.json"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
ENGINE="$TMP/engine"
STATE="$TMP/state"
mkdir -p "$ENGINE" "$STATE"
cp -r "$ANTFARM/bin" "$ANTFARM/schema" "$ANTFARM/games" "$ENGINE/"
export ANTFARM_STATE_DIR="$STATE"
BUS="$ENGINE/bin/bus"
MATCH="$STATE/matches/probe"
LOG="$STATE/registry/messages.jsonl"
mkdir -p "$MATCH"
cp "$TELEPHONE_GRAPH" "$MATCH/graph.json"
printf '{"match": "probe", "status": "open", "nodes": {}}\n' > "$MATCH/nodes.json"

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

register() { # <node> <pid>: writes in nodes.json the pair (pid, starttime) of the process
  local node=$1 pid=$2 stat start_time
  stat=$(< "/proc/$pid/stat")
  start_time=$(awk '{print $20}' <<< "${stat##*) }")
  python3 - "$MATCH/nodes.json" "$node" "$pid" "$start_time" <<'PY'
import json, sys
path, node, pid, start_time = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
data = json.load(open(path))
data["nodes"][node] = {"pid": pid, "starttime": start_time}
json.dump(data, open(path, "w"))
PY
}

as_node() { # <node> <command...>: runs the command as a child of a process registered as <node>
  local node=$1; shift
  (
    register "$node" "$BASHPID"
    "$@"
    code=$?
    exit "$code"
  )
}

echo "validator"
verify "telephone graph valid → 0" "$ENGINE/bin/validate-graph.py" "$TELEPHONE_GRAPH"
verify "edge towards a missing node → ≠0" \
  bash -c "! '$ENGINE/bin/validate-graph.py' '$ANTFARM/test/graphs/edge-to-missing-node.json'"
verify "right not in the list → ≠0" \
  bash -c "! '$ENGINE/bin/validate-graph.py' '$ANTFARM/test/graphs/right-not-listed.json'"

echo "event validator"
verify "message event built from a row of the log → 0" \
  "$ENGINE/bin/validate-events.py" "$ANTFARM/test/events/valid-message.jsonl"
verify "class not in the list → ≠0" \
  bash -c "! '$ENGINE/bin/validate-events.py' '$ANTFARM/test/events/class-not-listed.jsonl'"
verify "message event without the match id → ≠0" \
  bash -c "! '$ENGINE/bin/validate-events.py' '$ANTFARM/test/events/message-without-match.jsonl'"
verify "synthetic spawn stream → 0" "$ENGINE/bin/validate-events.py" "$ANTFARM/test/streams/spawn.jsonl"
jq -c 'if .class == "spawn" then .delta.edges |= map(select(.from != "editor")) else . end' \
  "$ANTFARM/test/streams/spawn.jsonl" > "$TMP/spawn-without-edge.jsonl"
verify "spawn without the parent edge → child in the delta → ≠0" \
  bash -c "! '$ENGINE/bin/validate-events.py' '$TMP/spawn-without-edge.jsonl'"

verify "synthetic stream of the changed graph → 0" "$ENGINE/bin/validate-events.py" "$ANTFARM/test/streams/graph-changed.jsonl"
CHANGE_STREAM="$ANTFARM/test/streams/graph-changed.jsonl"
jq -c 'if .type == "graph-changed" then del(.delta) else . end' "$CHANGE_STREAM" > "$TMP/change-without-delta.jsonl"
verify "graph-changed without delta → ≠0" bash -c "! '$ENGINE/bin/validate-events.py' '$TMP/change-without-delta.jsonl'"
jq -c 'if .type == "graph-changed" then .delta.version = 3 else . end' "$CHANGE_STREAM" > "$TMP/change-other-version.jsonl"
verify "delta with a version different from the graph's → ≠0" bash -c "! '$ENGINE/bin/validate-events.py' '$TMP/change-other-version.jsonl'"
jq -c 'if .type == "graph-changed" then .ts = "2026-11-08T10:00:11.000+01:00" else . end' "$CHANGE_STREAM" > "$TMP/change-other-time.jsonl"
verify "ts of the event different from effective → ≠0" bash -c "! '$ENGINE/bin/validate-events.py' '$TMP/change-other-time.jsonl'"
jq -c 'if .type == "graph-changed" then .delta.edges_removed = [{"from": "A", "to": "B", "rights": ["write"]}] else . end' "$CHANGE_STREAM" > "$TMP/change-removed-present.jsonl"
verify "a removed edge still present in the graph → ≠0" bash -c "! '$ENGINE/bin/validate-events.py' '$TMP/change-removed-present.jsonl'"
jq -c 'if .type == "match-opened" then .delta = {"version": 2, "nodes_added": [], "nodes_removed": [], "edges_added": [], "edges_removed": []} else . end' "$CHANGE_STREAM" > "$TMP/delta-out-of-place.jsonl"
verify "delta on an event that is not graph-changed → ≠0" bash -c "! '$ENGINE/bin/validate-events.py' '$TMP/delta-out-of-place.jsonl'"

echo "graph delta"
DELTA=$(python3 - "$ENGINE" <<'PY'
import json, sys
sys.path.insert(0, f"{sys.argv[1]}/bin")
import graph as libgraph

edge = lambda from_node, to_node, *rights: {"from": from_node, "to": to_node, "rights": list(rights)}
node = lambda identity, role="x": {"id": identity, "type": "llm", "role": role}
before = {"version": 1, "nodes": [node("A"), node("B"), node("X")],
          "edges": [edge("A", "B", "know", "write"), edge("B", "A", "write"), edge("A", "X", "write")]}
after = {"version": 2, "nodes": [node("A"), node("B", "new role"), node("C")],
         "edges": [edge("A", "B", "write", "know"), edge("B", "A", "know", "write"), edge("B", "C", "write")]}
print(json.dumps(libgraph.delta(before, after)))
PY
)
verify "delta: the version is the new one" jq -e '.version == 2' <<< "$DELTA"
verify "delta: new node among the added, vanished node among the removed" \
  jq -e '([.nodes_added[].id] | index("C")) and ([.nodes_removed[].id] | index("X"))' <<< "$DELTA"
verify "delta: a node that changes a field is removed and added" \
  jq -e '([.nodes_added[].id] | index("B")) and ([.nodes_removed[].id] | index("B"))' <<< "$DELTA"
verify "delta: the same rights in another order are not a change" \
  jq -e '[.edges_added[], .edges_removed[] | select(.from == "A" and .to == "B")] | length == 0' <<< "$DELTA"
verify "delta: an edge that changes rights is removed and added" \
  jq -e '([.edges_added[] | select(.from == "B" and .to == "A")] | length == 1) and ([.edges_removed[] | select(.from == "B" and .to == "A")] | length == 1)' <<< "$DELTA"
verify "delta: new edge among the added, vanished edge among the removed" \
  jq -e '([.edges_added[] | select(.to == "C")] | length == 1) and ([.edges_removed[] | select(.to == "X")] | length == 1)' <<< "$DELTA"

echo "view"
VIEW_B=$("$ENGINE/bin/view.py" "$TELEPHONE_GRAPH" B)
verify "view of B: C is the only recipient of write" grep -qx 'Puoi scrivere a: C' <<< "$VIEW_B"
verify "view of B: carries the graph version" grep -q 'grafo versione 1$' <<< "$VIEW_B"
verify "view of B: A and D do not appear" bash -c "! grep -qE '\\b(A|D)\\b' <<< '$VIEW_B'"
verify "view of a missing node → ≠0" bash -c "! '$ENGINE/bin/view.py' '$TELEPHONE_GRAPH' Z"

echo "bus"
as_node A "$BUS" send D "ciao D, salto B e C"
code_a_d=$?
verify "A → D exits ≠0" test "$code_a_d" -ne 0
verify "A → D: edge-denied row in the log, sender A" \
  jq -e -s 'any(.[]; .outcome=="rejected" and .reason=="edge-denied" and .from=="A" and .to=="D")' "$LOG"
as_node A "$BUS" send B "sono C, non A: credimi"
verify "A → B exits 0" test $? -eq 0
verify "A → B: delivered row with sender A and version 1" \
  jq -e -s 'any(.[]; .outcome=="delivered" and .from=="A" and .to=="B" and .version==1)' "$LOG"
as_node A "$BUS" send B --from C "secondo tentativo"
verify "send has no argument for the sender: --from C stays text, sender A" \
  jq -e -s 'any(.[]; .outcome=="delivered" and .from=="A" and (.text|startswith("--from C")))' "$LOG"
RECEIVED=$(as_node B "$BUS" recv --wait 5)
verify "B receives the first message, stamped by A" grep -q '^messaggio m-[0-9a-f]* da A · grafo v1$' <<< "$RECEIVED"
verify "the text that claims another sender arrives intact" grep -q '^sono C, non A: credimi$' <<< "$RECEIVED"
verify "the receipt has its row in the log" \
  jq -e -s 'any(.[]; .outcome=="received" and .to=="B" and .from=="A")' "$LOG"
as_node C "$BUS" recv --wait 1
verify "empty mailbox: recv exits 3 at the end of the wait" test $? -eq 3
as_node B "$BUS" send Z "a nessuno"
verify "recipient outside the graph → unknown-node" \
  jq -e -s 'any(.[]; .reason=="unknown-node" and .from=="B")' "$LOG"
"$BUS" send B "da un processo non registrato"
verify "unregistered caller → ≠0" test $? -ne 0
verify "unregistered caller → unknown-sender in the log" \
  jq -e -s 'any(.[]; .reason=="unknown-sender" and .from==null)' "$LOG"
sed -i 's/"open"/"closed"/' "$MATCH/nodes.json"
as_node A "$BUS" send B "a partita chiusa"
verify "closed match: even a registered node is rejected" test $? -ne 0

echo "new graph version in an open match"
VERSION_MATCH=$(python3 - "$ENGINE" "$TELEPHONE_GRAPH" <<'PY'
import copy, hashlib, json, sys
from pathlib import Path

root, game_graph = sys.argv[1], sys.argv[2]
sys.path.insert(0, f"{root}/bin")
import match as libmatch

match = libmatch.Match.create("probe-version", "probe", game_graph, {})
base = json.loads(Path(game_graph).read_text(encoding="utf-8"))
fingerprint = lambda: hashlib.sha256((match.directory / "graph.json").read_bytes()).hexdigest()
before = fingerprint()
same_number = copy.deepcopy(base)
skips_a_number = copy.deepcopy(base)
skips_a_number["version"] = 3
edge_without_node = copy.deepcopy(base)
edge_without_node["version"] = 2
edge_without_node["edges"].append({"from": "A", "to": "Z", "rights": ["write"]})
rejections = {}
for name, graph in (("same_number", same_number), ("skips_a_number", skips_a_number), ("edge_without_node", edge_without_node)):
    try:
        match.write_version(graph)
        rejections[name] = None
    except libmatch.GraphRejected as error:
        rejections[name] = str(error)
Path(root, "version-rejections.json").write_text(json.dumps({
    "rejections": rejections, "fingerprint_before": before, "fingerprint_after": fingerprint(),
    "versions": sorted(file.name for file in (match.directory / "graphs").iterdir()),
    "hidden": sorted(file.name for file in match.directory.iterdir() if file.name.startswith(".") and file.name != libmatch.libgraph.GRAPH_LOCK),
}), encoding="utf-8")
print(match.directory)
PY
)
VERSION_REJECTIONS=$(cat "$ENGINE/version-rejections.json")
MATCH=$VERSION_MATCH
verify "version that did not grow → rejected" jq -e '.rejections.same_number | contains("version 1 instead of 2")' <<< "$VERSION_REJECTIONS"
verify "version that skips a number → rejected" jq -e '.rejections.skips_a_number | contains("version 3 instead of 2")' <<< "$VERSION_REJECTIONS"
verify "edge towards a node that does not exist → rejected by validate-graph.py" jq -e '.rejections.edge_without_node | contains("missing node")' <<< "$VERSION_REJECTIONS"
verify "after the rejections the sha256 of graph.json is the one from before" jq -e '.fingerprint_before == .fingerprint_after' <<< "$VERSION_REJECTIONS"
verify "after the rejections only version 1 is left, no hidden candidate" jq -e '.versions == ["v1.json"] and .hidden == []' <<< "$VERSION_REJECTIONS"
verify "the first version sits in graphs/v1.json with its time" jq -e '.graph.version == 1 and (.effective | test("T"))' "$MATCH/graphs/v1.json"
VERSION_ID=$(basename "$MATCH")
as_node A "$BUS" send B "prima del cambio"
verify "before the change A → B carries version 1" \
  jq -e -s --arg p "$VERSION_ID" 'any(.[]; .match==$p and .outcome=="delivered" and .from=="A" and .to=="B" and .version==1)' "$LOG"
EFFECTIVE=$(python3 - "$ENGINE" "$MATCH" "$TELEPHONE_GRAPH" <<'PY'
import copy, json, sys
from pathlib import Path

root, directory, game_graph = sys.argv[1], sys.argv[2], sys.argv[3]
sys.path.insert(0, f"{root}/bin")
import match as libmatch

graph = json.loads(Path(game_graph).read_text(encoding="utf-8"))
graph["version"] = 2
graph["nodes"].append({"id": "E", "type": "script", "role": "joins an open match"})
graph["edges"] = [edge for edge in graph["edges"] if (edge["from"], edge["to"]) != ("A", "B")]
graph["edges"].append({"from": "A", "to": "E", "rights": ["know", "write"]})
print(libmatch.Match(directory).write_version(graph))
PY
)
verify "the valid version takes effect: graph.json carries 2" jq -e '.version == 2' "$MATCH/graph.json"
verify "version 2 sits in graphs/v2.json, equal to graph.json, with its time" \
  bash -c "jq -e --arg time '$EFFECTIVE' '.effective == \$time' '$MATCH/graphs/v2.json' && test \"\$(jq -cS .graph '$MATCH/graphs/v2.json')\" = \"\$(jq -cS . '$MATCH/graph.json')\""
NEW_VIEW=$(as_node A "$BUS" view)
verify "bus view reads the new version at the next command" grep -q 'grafo versione 2$' <<< "$NEW_VIEW"
verify "bus view: A writes to E and no longer to B" grep -qx 'Puoi scrivere a: E' <<< "$NEW_VIEW"
as_node A "$BUS" send B "dopo il cambio"
code_removed=$?
verify "an edge removed by the new version exits ≠0" test "$code_removed" -ne 0
verify "A → B after the change: edge-denied with version 2" \
  jq -e -s --arg p "$VERSION_ID" 'any(.[]; .match==$p and .outcome=="rejected" and .reason=="edge-denied" and .from=="A" and .to=="B" and .version==2)' "$LOG"
as_node A "$BUS" send E "sull'arco nuovo"
verify "A → E after the change: delivered with version 2" \
  jq -e -s --arg p "$VERSION_ID" 'any(.[]; .match==$p and .outcome=="delivered" and .from=="A" and .to=="E" and .version==2)' "$LOG"
RECEIVED_AFTER=$(as_node B "$BUS" recv --wait 5)
verify "the message sent before the change, received after it, keeps version 1" grep -q '^messaggio m-[0-9a-f]* da A · grafo v1$' <<< "$RECEIVED_AFTER"
verify "time and version agree: v1 before effective, v2 after" \
  jq -e -s --arg p "$VERSION_ID" --arg time "$EFFECTIVE" '
    [.[] | select(.match==$p and .outcome=="delivered")] as $rows
    | ($rows | length) == 2 and all($rows[]; if .version == 1 then .ts <= $time else .ts >= $time end)' "$LOG"
sed -i 's/"open"/"closed"/' "$MATCH/nodes.json"

echo "source: validator"
GRAPHS="$ANTFARM/test/graphs"
verify "graph with the source bottone valid → 0" "$ENGINE/bin/validate-graph.py" "$GRAPHS/source.json"
for invalid in source-absolute-path source-path-outside source-without-path source-that-speaks path-on-llm-node; do
  verify "$invalid → ≠0" bash -c "! '$ENGINE/bin/validate-graph.py' '$GRAPHS/$invalid.json'"
done

echo "source: view"
VIEW_A=$("$ENGINE/bin/view.py" "$GRAPHS/source.json" A)
VIEW_SB=$("$ENGINE/bin/view.py" "$GRAPHS/source.json" B)
verify "view of A: the source is not among the nodes to write to" grep -qx 'Puoi scrivere a: B' <<< "$VIEW_A"
verify "view of A: bottone on the sources row" \
  grep -qx 'Puoi scrivere i file (sorgenti, con bus write): bottone' <<< "$VIEW_A"
verify "view of B: no sources row" bash -c "! grep -q 'sorgenti' <<< '$VIEW_SB'"

echo "source: bus write"
MATCH="$STATE/matches/probe-source"
mkdir -p "$MATCH/bottone"
cp "$GRAPHS/source.json" "$MATCH/graph.json"
printf '{"match": "probe-source", "status": "open", "nodes": {}}\n' > "$MATCH/nodes.json"
BUTTON="$MATCH/bottone/bottone.txt"
: > "$BUTTON"
SHA_BEFORE=$(sha256sum "$BUTTON" | cut -d' ' -f1)
as_node B "$BUS" write bottone BOOM
code_b=$?
verify "B write bottone exits ≠0" test "$code_b" -ne 0
verify "B write bottone: edge-denied row in the log, from B to bottone" \
  jq -e -s 'any(.[]; .match=="probe-source" and .outcome=="rejected" and .reason=="edge-denied" and .from=="B" and .to=="bottone")' "$LOG"
verify "B write bottone: sha256 of the file equal before and after" test "$(sha256sum "$BUTTON" | cut -d' ' -f1)" = "$SHA_BEFORE"
as_node A "$BUS" write altro ciao
verify "A write towards a node outside the graph → unknown-node" \
  jq -e -s 'any(.[]; .match=="probe-source" and .reason=="unknown-node" and .from=="A" and .to=="altro")' "$LOG"
verify "A write altro: no new file in the match" \
  test -z "$(find "$MATCH" -name 'altro*' -print -quit)"
as_node A "$BUS" write B "a un nodo"
verify "A write towards a node that is not a source → wrong-type" \
  jq -e -s 'any(.[]; .match=="probe-source" and .reason=="wrong-type" and .from=="A" and .to=="B")' "$LOG"
as_node A "$BUS" send bottone "un messaggio al file"
verify "A send towards the source → wrong-type, no mailbox" \
  bash -c "jq -e -s 'any(.[]; .match==\"probe-source\" and .reason==\"wrong-type\" and .from==\"A\" and .to==\"bottone\")' '$LOG' && test ! -e '$MATCH/mailboxes/bottone'"
verify "after the rejections the file is still intact" test "$(sha256sum "$BUTTON" | cut -d' ' -f1)" = "$SHA_BEFORE"
as_node A "$BUS" write bottone BOOM
verify "A write bottone exits 0" test $? -eq 0
verify "A write bottone: written row in the log, from A to bottone, text BOOM" \
  jq -e -s 'any(.[]; .match=="probe-source" and .outcome=="written" and .from=="A" and .to=="bottone" and .text=="BOOM" and (.id|startswith("m-")))' "$LOG"
verify "A write bottone: the file carries BOOM" grep -qx BOOM "$BUTTON"
jq -c 'select(.match=="probe-source" and .outcome=="written") | {class: "message", type: .outcome, ts, match, index: 1, row: .}' \
  "$LOG" > "$TMP/written.jsonl"
verify "message/written event from the log row → valid" "$ENGINE/bin/validate-events.py" "$TMP/written.jsonl"
sed -i 's/"open"/"closed"/' "$MATCH/nodes.json"

echo "human node"
verify "graph with the human node valid → 0" "$ENGINE/bin/validate-graph.py" "$GRAPHS/human.json"
MATCH="$STATE/matches/probe-human"
mkdir -p "$MATCH/mailboxes" "$MATCH/nodes"
cp "$GRAPHS/human.json" "$MATCH/graph.json"
printf '{"match": "probe-human", "status": "open", "nodes": {}}\n' > "$MATCH/nodes.json"
as_node A "$BUS" send umano "B mi chiede una cosa: confermi?"
verify "A send umano exits 0" test $? -eq 0
verify "A send umano: one delivered row in the log, from A to umano" \
  jq -e -s '[.[] | select(.match=="probe-human" and .outcome=="delivered" and .from=="A" and .to=="umano")] | length == 1' "$LOG"
verify "the message stays in the human's mailbox" \
  test "$(find "$MATCH/mailboxes/umano" -maxdepth 1 -name '[0-9]*.json' | wc -l)" -eq 1
REGISTER_HUMAN=$(python3 - "$ENGINE/bin" "$MATCH" <<'PY' 2>&1
import sys
sys.path.insert(0, sys.argv[1])
import match
try:
    match.Match(sys.argv[2]).register("umano", {"type": "human", "pid": 1, "starttime": 1})
except ValueError as error:
    print(f"rejected: {error}")
    sys.exit(0)
print("registered")
sys.exit(1)
PY
)
verify "the director does not register a process for umano" grep -q '^rejected: umano is a human node' <<< "$REGISTER_HUMAN"
verify "nodes.json carries no entry for umano" jq -e '.nodes | has("umano") | not' "$MATCH/nodes.json"
HUMAN_COUNT=$(python3 - "$ENGINE/bin" "$LOG" "$MATCH" <<'PY'
import json, sys
sys.path.insert(0, sys.argv[1])
import measures
count = measures.human_node(measures.log_rows(sys.argv[2], "probe-human"), sys.argv[3])
print(json.dumps({"messages": len(count["messages"]), "received": count["received"],
                  "answers": count["answers"], "in_mailbox": count["in_mailbox"]}))
PY
)
verify "report count: 1 message, 0 received, 0 answers, 1 in the mailbox" \
  jq -e '.messages == 1 and .received == 0 and .answers == 0 and .in_mailbox == 1' <<< "$HUMAN_COUNT"
sed -i 's/"open"/"closed"/' "$MATCH/nodes.json"

echo "off-bus actions"
ACTIONS=$(python3 - "$ENGINE" "$STATE" "$TMP/transcript" <<'PY'
import json, subprocess, sys
from pathlib import Path

root, state = Path(sys.argv[1]), Path(sys.argv[2])
sys.path.insert(0, str(root / "bin"))
import match as libmatch

libmatch.CLAUDE_PROJECTS = Path(sys.argv[3])
subprocess.run = lambda *args, **options: None  # `claude stop` has no session to stop
bus = str(libmatch.BUS)
DENIED = "Permission to use Bash has been denied."


def use(index, command, name="Bash"):
    return {"type": "tool_use", "id": f"u{index}", "name": name, "input": {"command": command} if command else {}}


def outcome(index, text, error=False):
    return {"type": "tool_result", "tool_use_id": f"u{index}", "content": text, "is_error": error}


def transcript(session, blocks):
    directory = libmatch.CLAUDE_PROJECTS / "project"
    directory.mkdir(parents=True, exist_ok=True)
    with open(directory / f"{session}.jsonl", "w", encoding="utf-8") as transcript_file:
        for kind, block in blocks:
            transcript_file.write(json.dumps({"type": kind, "message": {"content": [block]}}) + "\n")


directory = state / "matches" / "probe-actions"
directory.mkdir(parents=True)
match = libmatch.Match(directory)
entries = {
    "director": {"type": "script"},
    "script": {"type": "script", "closed": "yesterday", "off_bus_actions": None},
    "A": {"type": "llm", "short_id": "aaaa", "sessionId": "sess-a"},
    "B": {"type": "llm", "short_id": "bbbb", "sessionId": "sess-b"},
    "S": {"type": "llm", "short_id": "ssss", "sessionId": "sess-gone"},
    "C": {"type": "llm", "short_id": "cccc", "sessionId": "sess-c"},
}
libmatch.write_json(match.nodes_file, {"match": "probe-actions", "status": "closed", "nodes": entries})
TWO_LINE_COMMAND = "echo 'ciao   mondo' | wc -w\n# seconda riga"
transcript("sess-a", [
    ("assistant", use(1, f"{bus} send B ciao")), ("user", outcome(1, "consegnato")),
    ("assistant", use(2, "cat /etc/hostname")), ("user", outcome(2, DENIED, True)),
    ("assistant", use(3, TWO_LINE_COMMAND)), ("user", outcome(3, "x" * 5000)),
    ("assistant", use(4, f"  {bus} recv")), ("user", outcome(4, "messaggio")),
    ("assistant", use(5, "sleep 900")),
])
transcript("sess-b", [("assistant", use(1, f"{bus} recv")), ("user", outcome(1, "messaggio"))])
# The filter looks at the whole command: one off-bus piece lets it in, a chain of bus calls only does not.
GET_IN = [
    f"{bus} send A x && cat /etc/hostname",
    f"{bus} send A x; ls",
    f"{bus} send A x | tee f",
    f"{bus} send A x\ncat /etc/hostname",
    f"{bus} send A \"$(cat /etc/hostname)\"",
    f"{bus} send A x & cat /etc/hostname",
    f"{bus} send A `cat /etc/hostname`",
    f"{bus} send A <<EOF\n$(cat /etc/hostname)\nEOF",
    f"{bus} send A <(ls)",
]
STAY_OUT = [
    f"{bus} send A x && {bus} send B y",
    f"{bus} send A \"testo con && dentro\"",
    f"{bus} send A <<'EOF'\ntesto con && dentro\n$(non eseguito)\nEOF",
    f"{bus} send A <<EOF\ntesto con && dentro; ls | wc\nEOF",
    f"{bus} send A x 2>&1",
    f"{bus} send A x &> /dev/null",
    f"{bus} send A 'con `backtick` letterale e $(x)'",
    f"{bus} send A x &&\n  {bus} send B y",
    f"{bus} send A \"$((1+2))\"",
]
blocks_c = []
for index, command in enumerate(sum(zip(GET_IN, STAY_OUT), ()), start=1):
    blocks_c += [("assistant", use(index, command)), ("user", outcome(index, "esito"))]
transcript("sess-c", blocks_c)
match.stop_nodes()
after_close = match.nodes()["nodes"]
a, b, s, c = after_close["A"], after_close["B"], after_close["S"], after_close["C"]
got_in_c = [action["command"] for action in c["actions"]]

actions_list = a["actions"]
without_transcript = {"actions": s["actions"], "count": s["off_bus_actions"]}

# Matches already played: the entry carries the count, not the list.
a.pop("actions")
b.pop("actions")
s.pop("actions")
c.pop("actions")
s["off_bus_actions"] = 3
after_close["script"].pop("actions", None)
after_close["N"] = {"type": "llm", "sessionId": "sess-b"}  # entry without count
after_close["D"] = {"type": "llm", "sessionId": "sess-a", "off_bus_actions": 99}  # count different from the list
libmatch.write_json(match.nodes_file, {"match": "probe-actions", "status": "closed", "nodes": after_close})
written, problems = match.rewrite_actions()
rewritten = match.nodes()["nodes"]
fingerprint = match.nodes_file.read_bytes()
match.rewrite_actions()
print(json.dumps({
    "list": actions_list,
    "count_a": a["off_bus_actions"],
    "c_got_in": got_in_c,
    "c_expected": GET_IN,
    "c_missing": [command for command in GET_IN if command not in got_in_c],
    "c_out_got_in": [command for command in STAY_OUT if command in got_in_c],
    "expected_command": TWO_LINE_COMMAND,
    "without_transcript": without_transcript,
    "written": written,
    "problems": problems,
    "rewritten_a": rewritten["A"].get("actions"),
    "script_has_actions": "actions" in rewritten["script"],
    "without_count_has_actions": "actions" in rewritten["N"],
    "different": {"actions": "actions" in rewritten["D"], "count": rewritten["D"]["off_bus_actions"]},
    "idempotent": fingerprint == match.nodes_file.read_bytes(),
}, ensure_ascii=False))
PY
)
verify "at the end of the match: 3 actions, the bus calls excluded" jq -e '.list | length == 3' <<< "$ACTIONS"
verify "the count is the length of the list" jq -e '.count_a == (.list | length)' <<< "$ACTIONS"
verify "a node without transcript at closing: list and count null" \
  jq -e '.without_transcript.actions == null and .without_transcript.count == null' <<< "$ACTIONS"
verify "progressive order from 1" jq -e '[.list[].order] == [1,2,3]' <<< "$ACTIONS"
verify "the command stays whole, line break included" jq -e '.list[1].command == .expected_command' <<< "$ACTIONS"
verify "a denial carries error yes and the text of the denial" \
  jq -e '.list[0].error == true and (.list[0].outcome | startswith("Permission to use Bash"))' <<< "$ACTIONS"
verify "the outcome is cut at 1000 characters, error no" \
  jq -e '.list[1].error == false and (.list[1].outcome | length) == 1000' <<< "$ACTIONS"
verify "a call without answer: empty outcome, error null" \
  jq -e '.list[2].outcome == "" and .list[2].error == null' <<< "$ACTIONS"
verify "inwards: every command with an off-bus piece gets in, whole and in order — &&, ;, |, line break, \$(…), &, backtick, unquoted heredoc, <(…)" \
  jq -e '.c_missing == [] and .c_got_in == .c_expected' <<< "$ACTIONS"
verify "outwards: bus-only chain, && in quotes, heredoc, 2>&1, &>, backtick in single quotes, \$((…)) stay out" \
  jq -e '.c_out_got_in == []' <<< "$ACTIONS"
verify "rewrite: same list as at closing" jq -e '.rewritten_a == .list' <<< "$ACTIONS"
verify "rewrite: B with 0 actions carries the empty list, C its entries" \
  jq -e '.written.A == 3 and .written.B == 0 and .written.C == (.c_expected | length)' <<< "$ACTIONS"
verify "rewrite: neither a script node nor an entry without count receives the list" \
  jq -e '.script_has_actions == false and .without_count_has_actions == false' <<< "$ACTIONS"
verify "rewrite: count different from the list → problem, entry and count intact" \
  jq -e '(.problems.D | contains("99 in the count")) and .different.actions == false and .different.count == 99' <<< "$ACTIONS"
verify "rewrite: transcript gone → problem" jq -e '.problems.S | contains("missing")' <<< "$ACTIONS"
verify "rewrite: a second pass does not change the file" jq -e '.idempotent' <<< "$ACTIONS"

echo "class of an action"
CLASSES=$(python3 - "$ENGINE" <<'PY'
import json, sys
from pathlib import Path

root = Path(sys.argv[1])
sys.path.insert(0, str(root / "bin"))
import match as libmatch

bus = str(libmatch.BUS)
DENIED = "Permission to use Bash has been denied because Claude Code is running in dontAsk mode."


def action_class(command, outcome=DENIED):
    return libmatch.action_class({"order": 1, "tool": "Bash", "command": command, "outcome": outcome, "error": True})


print(json.dumps({
    "variable": action_class(f"B={bus}; $B send A x"),
    "quotes_and_braces": action_class(f"B={bus}; M=\"uno; due && tre\"; $B send A \"$M\" && ${{B}} send C \"$M\""),
    "read": action_class("cat /etc/hostname"),
    "write": action_class(f"echo BOOM > {root}/bottone.txt"),
    "reads_the_bus": action_class(f"cat {bus}"),
    "bus_then_other": action_class(f"{bus} send A x && cat /etc/hostname"),
    "telephone_form": action_class(f"T=\"a; b\"; echo \"$T\" | wc -w; {bus} send B \"$T\""),
    "bus_not_denied": action_class(f"B={bus}; $B send A x", "consegnato"),
    "assignments_only": action_class("X=1"),
    "assignment_before_a_command": action_class("X=1 cat /etc/hostname"),
    "variable_not_the_bus": action_class("B=/bin/cat; $B /etc/hostname"),
    "variable_reassigned": action_class(f"B={bus}; B=/bin/cat; $B /etc/hostname"),
    "unbalanced_quotes": action_class(f"{bus} send A \"x"),
    "substitution": action_class(f"{bus} send A \"$(cat /etc/hostname)\""),
    "literal_substitution": action_class(f"{bus} send A '$(x)'"),
    "quoted_heredoc": action_class(f"{bus} send A <<'EOF'\n$(x) && ls\nEOF"),
    "heredoc_with_substitution": action_class(f"{bus} send A <<EOF\n$(x)\nEOF"),
    "segments": libmatch.command_segments("a \"b; c\" && d || e | f\ng; 'h\ni';"),
    "segments_heredoc_and_background": libmatch.command_segments("cat <<EOF\na; b\nEOF\nls & wc 2>&1 &> f"),
}, ensure_ascii=False))
PY
)
DENIED_BUS="denied bus call"
OFF_CHANNEL="off-channel action"
verify_class() { # <description> <key> <expected class>
  verify "$1" jq -e --arg expected "$3" ".$2 == \$expected" <<< "$CLASSES"
}
verify_class "B=<bus>; \$B send A x denied → denied bus call" variable "$DENIED_BUS"
verify_class "; and && inside quotes do not split, \${B} is the bus → denied bus call" quotes_and_braces "$DENIED_BUS"
verify_class "cat /etc/hostname denied → off the channel" read "$OFF_CHANNEL"
verify_class "echo BOOM > <file> denied → off the channel" write "$OFF_CHANNEL"
verify_class "cat <bus> denied → off the channel" reads_the_bus "$OFF_CHANNEL"
verify_class "<bus> send A x && cat /etc/hostname denied → off the channel" bus_then_other "$OFF_CHANNEL"
verify_class "the telephone form, with echo | wc in the middle → off the channel" telephone_form "$OFF_CHANNEL"
verify_class "the same bus form, not denied → off the channel" bus_not_denied "$OFF_CHANNEL"
verify_class "assignments only, denied: no bus call → off the channel" assignments_only "$OFF_CHANNEL"
verify_class "X=1 in front of a command is not an assignment → off the channel" assignment_before_a_command "$OFF_CHANNEL"
verify_class "\$B assigned to another program → off the channel" variable_not_the_bus "$OFF_CHANNEL"
verify_class "\$B reassigned after the bus → off the channel" variable_reassigned "$OFF_CHANNEL"
verify_class "unbalanced quotes → off the channel" unbalanced_quotes "$OFF_CHANNEL"
verify_class "<bus> send A \"\$(cat …)\" denied: the substitution leaves the channel → off the channel" substitution "$OFF_CHANNEL"
verify_class "\$(x) in single quotes is text → denied bus call" literal_substitution "$DENIED_BUS"
verify_class "quoted heredoc with \$(x) && ls in the body → denied bus call" quoted_heredoc "$DENIED_BUS"
verify_class "unquoted heredoc with \$(x) in the body → off the channel" heredoc_with_substitution "$OFF_CHANNEL"
verify "the splitter splits outside quotes and drops the empty ones" \
  jq -e '.segments == ["a \"b; c\"", "d", "e", "f", "g", "'"'"'h\ni'"'"'"]' <<< "$CLASSES"
verify "the splitter skips the body of a heredoc, cuts on a lone & and keeps 2>&1 and &>" \
  jq -e '.segments_heredoc_and_background == ["cat <<EOF", "ls", "wc 2>&1 &> f"]' <<< "$CLASSES"

echo "telephone oracle"
ORACLE=$(python3 - "$ANTFARM/games/telephone" <<'PY'
import json, sys
sys.path.insert(0, sys.argv[1])
import facts, game
fact_list = facts.generate(42)
departure = facts.start_message(fact_list)
first, second = fact_list[0]["number"], fact_list[1]["number"]
lossy = departure.replace(str(first), f"{first // 100}.{first % 100:02d}").replace(str(second), "molti")
rows = [{"outcome": "delivered", "from": from_node, "to": to_node, "id": f"m-{index}", "words": len(text.split()), "text": text}
        for index, (from_node, to_node, text) in enumerate([("director", "A", departure), ("A", "B", departure),
                                                            ("B", "C", lossy), ("C", "D", lossy), ("D", "director", lossy)])]
complete = game.oracle(rows, fact_list)
interrupted = game.oracle(rows[:3], fact_list)
print(json.dumps({"outcome": complete["outcome"], "lost_bc": complete["hops"][2]["lost"], "expected": [first, second],
                  "interrupted": interrupted["outcome"]}))
PY
)
verify "two numbers lost between B and C → 8/10" jq -e '.outcome == "8/10"' <<< "$ORACLE"
verify "the lost numbers are charged to the hop B → C, reformatted digit included" jq -e '.lost_bc == .expected' <<< "$ORACLE"
verify "chain without C → D: outcome interrupted" jq -e '.interrupted == "interrupted C→D"' <<< "$ORACLE"

echo "the prompt of Leavitt"
LEAVITT=$(python3 - "$ANTFARM" <<'PY'
import json, sys
sys.path.insert(0, sys.argv[1])
from games.leavitt import game
prompt = game.node_prompt("C", "v", "/bus", 8000)
print(json.dumps({"chain": "concatenare gli invii in un solo comando con &&" in prompt,
                  "cap_of_the_command": "Tutti i testi di un comando, insieme, sono di al massimo 8000 caratteri" in prompt,
                  "send_again": "accorcia il testo e rimandalo" in prompt}))
PY
)
verify "the prompt lets a node chain sends and caps all the texts of a command together" jq -e '.chain and .cap_of_the_command' <<< "$LEAVITT"
verify "the prompt says to shorten the text and send it again when a send is denied" jq -e '.send_again' <<< "$LEAVITT"

echo "the exquisite corpse: graphs, prompt, oracle"
V1="$ANTFARM/games/corpse/graph-v1.json"
V2="$ANTFARM/games/corpse/graph-v2.json"
verify "corpse graph v1 valid → 0" "$ENGINE/bin/validate-graph.py" "$V1"
verify "corpse graph v2 valid → 0" "$ENGINE/bin/validate-graph.py" "$V2"
VIEW_D=$("$ENGINE/bin/view.py" "$V2" D)
verify "view of D: carries version 2" grep -q 'grafo versione 2$' <<< "$VIEW_D"
verify "view of D: the edge D → A" grep -q '^- D → A: ' <<< "$VIEW_D"
verify "view of C after the change: writes to D and no longer to A" grep -qx 'Puoi scrivere a: D, director' <<< "$("$ENGINE/bin/view.py" "$V2" C)"
CORPSE=$(python3 - "$ENGINE" "$ANTFARM" "$V1" "$V2" <<'PY'
import json, sys
root, antfarm, v1, v2 = sys.argv[1:5]
sys.path.insert(0, f"{root}/bin")
sys.path.insert(1, antfarm)
import graph as libgraph
import view as libview
from games.corpse import game

effective = "2026-11-08T10:00:10.000+01:00"
view_d = libview.as_text(libview.view(libgraph.load(v2), "D"))
prompt_d = game.node_prompt("D", view_d, "/bus", 8000)


def row(from_node, to_node, ts, version, text="turno 1: una riga", outcome="delivered", identity="m-1"):
    return {"ts": ts, "id": identity, "outcome": outcome, "from": from_node, "to": to_node, "version": version, "text": text}


before = "2026-11-08T10:00:09.000+01:00"
after = "2026-11-08T10:00:11.000+01:00"
good = [row("A", "B", before, 1), row("C", "A", before, 1), row("C", "D", after, 2), row("D", "A", after, 2),
        row("director", "C", after, 2, "avviso\nsu due righe")]
outcome = lambda rows, change=effective, prompt=prompt_d: game.oracle(rows, change, prompt)
passed = lambda rows, number, **options: outcome(rows, **options)["checks"][number - 1]["passed"]
received = game.received_after_change([row("C", "A", before, 1, identity="m-a"), row("C", "A", before, 1, outcome="received", identity="m-a") | {"ts": after},
                                       row("A", "B", before, 1, identity="m-b"), row("A", "B", before, 1, outcome="received", identity="m-b")], effective)
delta = libgraph.delta(libgraph.load(v1), libgraph.load(v2))
print(json.dumps({
    "good": outcome(good)["outcome"],
    "v2_before_the_change": passed([row("A", "B", before, 2)], 1),
    "v1_after_the_change": passed([row("A", "B", after, 1)], 1),
    "same_millisecond": passed([row("A", "B", effective, 1), row("B", "C", effective, 2)], 1),
    "without_v2": outcome([row("A", "B", before, 1)], change=None, prompt=None)["outcome"],
    "c_a_with_v2": passed([row("C", "A", after, 2)], 2),
    "c_a_rejected_does_not_count": passed([row("C", "A", after, 2, outcome="rejected")], 2),
    "missing_prompt": passed([], 3, prompt=None),
    "prompt_v1": passed([], 3, prompt=prompt_d.replace("grafo versione 2", "grafo versione 1")),
    "prompt_without_edge": passed([], 3, prompt=prompt_d.replace("- D → A: ", "- D → B: ")),
    "two_lines": passed([row("A", "B", before, 1, "turno 1: uno\nturno 2: due")], 4),
    "received_after": [entry["id"] for entry in received],
    "turn": [game.declared_turn("turno 7: x"), game.declared_turn("  Turno 12 : y"), game.declared_turn("riga senza numero")],
    "prompt_single_line": "una riga sola" in prompt_d and "Non riportare mai le righe precedenti" in prompt_d and "`turno 17: <la tua riga>`" in prompt_d,
    "prompt_joining": "Entri a partita già cominciata" in prompt_d and "Entri a partita già cominciata" not in game.node_prompt("A", "v", "/bus", 8000),
    "prompt_text_cap": "Scrivi testi di al massimo 8000 caratteri" in prompt_d and "accorcia il testo e rimandalo" in prompt_d,
    "delta_nodes": [node["id"] for node in delta["nodes_added"]], "delta_removed": [(edge["from"], edge["to"]) for edge in delta["edges_removed"]],
    "delta_added": sorted((edge["from"], edge["to"]) for edge in delta["edges_added"]), "nodes_removed": delta["nodes_removed"],
}, ensure_ascii=False))
PY
)
verify "four checks passed → 4/4; the director's rows do not count" jq -e '.good == "4/4"' <<< "$CORPSE"
verify "check 1: a v2 sent before the change fails" jq -e '.v2_before_the_change == false' <<< "$CORPSE"
verify "check 1: a v1 sent after the change fails" jq -e '.v1_after_the_change == false' <<< "$CORPSE"
verify "check 1: in the same millisecond of the change both are valid" jq -e '.same_millisecond == true' <<< "$CORPSE"
verify "without v2 and without the prompt of D: only check 3 fails → 3/4" jq -e '.without_v2 == "3/4"' <<< "$CORPSE"
verify "check 2: a C → A delivery with v2 fails" jq -e '.c_a_with_v2 == false' <<< "$CORPSE"
verify "check 2: a C → A rejection with v2 does not count" jq -e '.c_a_rejected_does_not_count == true' <<< "$CORPSE"
verify "check 3: missing prompt, at v1 or without the edge D → A fails" \
  jq -e '.missing_prompt == false and .prompt_v1 == false and .prompt_without_edge == false' <<< "$CORPSE"
verify "check 4: a message with two lines fails" jq -e '.two_lines == false' <<< "$CORPSE"
verify "received after the change: only the v1 message received after effective" jq -e '.received_after == ["m-a"]' <<< "$CORPSE"
verify "the turn number is read from the text, None if missing" jq -e '.turn == [7, 12, null]' <<< "$CORPSE"
verify "the prompt carries the single-line rule and the final turn" jq -e '.prompt_single_line' <<< "$CORPSE"
verify "only D receives the joining text for an open match" jq -e '.prompt_joining' <<< "$CORPSE"
verify "the prompt carries the cap of a text and the instruction to shorten and send again" jq -e '.prompt_text_cap' <<< "$CORPSE"
verify "delta v1 → v2: D joins, C → A goes away, C → D, D → A, D → director appear" \
  jq -e '.delta_nodes == ["D"] and .delta_removed == [["C","A"]] and .delta_added == [["C","D"],["D","A"],["D","director"]] and .nodes_removed == []' <<< "$CORPSE"

echo "the exquisite corpse: report on two dry-run matches played here"
# The two matches are played in the temporary state root, in parallel, about twenty
# seconds: a recorded match does not enter the repo, because it carries the machine's paths.
"$ENGINE/bin/director.py" corpse --dry-run > "$TMP/director-corpse.out" 2>&1 &
DIRECTOR_DRY=$!
"$ENGINE/bin/director.py" corpse --dry-run --negative-test > "$TMP/director-corpse-negative-test.out" 2>&1 &
DIRECTOR_NEGATIVE=$!
wait "$DIRECTOR_DRY"
verify "dry-run corpse match: the director exits 0" test $? -eq 0
wait "$DIRECTOR_NEGATIVE"
verify "dry-run corpse negative test: the director exits 0" test $? -eq 0
DRY_MATCH=$(find "$STATE/matches" -maxdepth 1 -name '*-corpse-dry-run' -printf '%f\n' | head -1)
NEGATIVE_MATCH=$(find "$STATE/matches" -maxdepth 1 -name '*-corpse-negative-test-dry-run' -printf '%f\n' | head -1)
REPORTS=$(python3 - "$ENGINE" "$STATE" "$DRY_MATCH" "$NEGATIVE_MATCH" <<'PY'
import json, sys
from pathlib import Path
engine, state = Path(sys.argv[1]), Path(sys.argv[2])
sys.path.insert(0, str(engine / "bin"))
sys.path.insert(1, str(engine))
from games.corpse import report

empty = {"sessions": [], "usage": {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}, "token": 0}
result = {}
for key, match in (("dry", sys.argv[3]), ("negative", sys.argv[4])):
    text = report.compose(report.measure(state / "matches" / match, state / "registry" / "matches.tsv",
                                         state / "registry" / "messages.jsonl", empty), empty, "registry", "log")
    section = text.split("## The recomposed text")[1].split("## Messages sent")[0]
    rows = [row for row in section.splitlines() if row.startswith("| ") and not row.startswith("| Position") and not row.startswith("| ---")]
    result[key] = {"text": text, "text_lines": len(rows), "first": rows[0], "last": rows[-1], "warnings": sum("⚠️" in row for row in rows)}
print(json.dumps(result, ensure_ascii=False))
PY
)
verify "one row per turn, from turn 0 to 17: 18 rows" jq -e '.dry.text_lines == 18 and .negative.text_lines == 18' <<< "$REPORTS"
verify "every written turn matches the position in the log" jq -e '.dry.warnings == 0 and .negative.warnings == 0' <<< "$REPORTS"
verify "the first row is turn 0 of the director, the last one turn 17 of C to the director" \
  jq -e '(.dry.first | contains("director → A")) and (.dry.last | contains("C → director"))' <<< "$REPORTS"
verify "the report carries the four checks, all passed" jq -e '(.dry.text | contains("**failed**") | not) and (.dry.text | contains("| 4 | one line per message | passed"))' <<< "$REPORTS"
verify "without a registry row it says so" jq -e '.dry.text | contains("Registry row missing")' <<< "$REPORTS"
verify "the report says how many times C wrote to A after the change: 0 in the dry run, 1 in the negative test" \
  jq -e '(.dry.text | contains("C wrote to A after the change: 0 times")) and (.negative.text | contains("C wrote to A after the change: 1 time"))' <<< "$REPORTS"

echo
if [[ $failed -gt 0 ]]; then
  echo "NEGATIVE TESTS FAILED: $failed"
  exit 1
fi
echo "negative tests: all ok"
