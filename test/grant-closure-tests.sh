#!/usr/bin/env bash
# Deterministic tests of bin/grant-closure.py, the closure of the grants of the loop: what every
# position can come to do, its own permission rules and those of its subtree. No LLM, no loop.
#
#   antfarm-core/test/grant-closure-tests.sh
#
# The main fixture is test/grants/closure: an llm chain of depth 2, root → middle → leaf, where root
# has a rule with a setup placeholder ([[file]]), middle has no allowed_tools and so the default rule
# of the loop, leaf has the parameter repo with three values; beside leaf, under middle, the script
# position tally. The variants — a permission mode other than dontAsk, a prefix rule — are copies of
# it in a temporary folder. The command only reads the grants: the tests run on the files of the
# engine. Exits 1 if a test fails.
set -uo pipefail

ANTFARM=$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)
CLOSURE="$ANTFARM/bin/grant-closure.py"
VALIDATOR="$ANTFARM/bin/validate-grants.py"
GRANTS="$ANTFARM/test/grants"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

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

holds() { # <json file> <python expression>: the expression is true on the --json output. In the
  # expression `p` is the positions; own(x) the rules of x, rules(x) the rules of its closure,
  # origins(x) the positions its closure rules come from, outside(x) the positions its closure
  # marks not analyzable
  python3 - "$1" "$2" <<'PY'
import json, sys
p = json.load(open(sys.argv[1], encoding="utf-8"))["positions"]
own = lambda position: [rule["rule"] for rule in p[position]["rules"]]
rules = lambda position: [rule["rule"] for rule in p[position]["closure"]["rules"]]
origins = lambda position: {rule["position"] for rule in p[position]["closure"]["rules"]}
outside = lambda position: [entry["position"] for entry in p[position]["closure"]["not_analyzable"]]
sys.exit(0 if eval(sys.argv[2]) else 1)
PY
}

exits() { # <code> <arguments...>: the command exits with that code
  local code=$1; shift
  "$CLOSURE" "$@" > /dev/null 2>&1
  [[ $? -eq $code ]]
}

prints() { # <count> <text> <arguments...>: that many lines of stdout hold the text; the exit code
  # is checked apart, with exits
  local count=$1 text=$2; shift 2
  local output
  output=$("$CLOSURE" "$@")
  [[ $(grep -cF -- "$text" <<< "$output") -eq $count ]]
}

forbids() { # <grants directory> <glob> <positions>: exit 1, and the gate names exactly those
  # positions, in the order of the tree
  local output
  output=$("$CLOSURE" "$1" --forbid "$2")
  [[ $? -eq 1 && $(grep -F "FORBIDDEN '$2'" <<< "$output") == *" — $3" ]]
}

rejects() { # <grants directory> [arguments...]: exit 2, nothing on stdout, and on stderr the
  # reason of the validator, the same text
  local directory=$1; shift
  local output code expected
  output=$("$CLOSURE" "$directory" "$@" 2> "$TMP/stderr")
  code=$?
  expected=$("$VALIDATOR" "$directory" 2>&1 > /dev/null)
  [[ $code -eq 2 && -z $output && -n $expected && $(< "$TMP/stderr") == "$expected" ]]
}

variant() { # <name> <python statement on `g`, position → grant>: a copy of test/grants/closure,
  # changed by the statement; prints its directory
  local directory="$TMP/$1"
  mkdir -p "$directory"
  cp "$GRANTS/closure"/*.json "$directory/"
  python3 - "$directory" "$2" <<'PY'
import json, sys
from pathlib import Path
directory = Path(sys.argv[1])
g = {path.stem: json.loads(path.read_text(encoding="utf-8")) for path in directory.glob("*.json")}
exec(sys.argv[2])
for position, grant in g.items():
    (directory / f"{position}.json").write_text(json.dumps(grant, ensure_ascii=False, indent=2), encoding="utf-8")
PY
  echo "$directory"
}

CHAIN="$GRANTS/closure"
JSON="$TMP/closure.json"
"$CLOSURE" "$CHAIN" --json > "$JSON"
LOG='Bash(git -C {state}/repos/'

echo "the closure, on the llm chain root → middle → leaf"
verify "the fixture is a valid set of grants" "$VALIDATOR" "$CHAIN"
verify "the command exits 0" exits 0 "$CHAIN"
verify "one closure per position, in the order of the tree" holds "$JSON" "list(p) == ['root', 'middle', 'leaf', 'tally']"
verify "the closure of root holds every rule of leaf" holds "$JSON" "own('leaf') and all(rule in rules('root') for rule in own('leaf'))"
verify "… and of middle" holds "$JSON" "own('middle') and all(rule in rules('root') for rule in own('middle'))"
verify "… and is the rules of root, middle and leaf, in this order" \
  holds "$JSON" "rules('root') == own('root') + own('middle') + own('leaf')"
verify "the closure of middle: middle and leaf, not root" holds "$JSON" "rules('middle') == own('middle') + own('leaf')"
verify "the closure of leaf: only its own rules" holds "$JSON" "rules('leaf') == own('leaf') and origins('leaf') == {'leaf'}"
verify "every rule carries the position it comes from" \
  holds "$JSON" "origins('root') == {'root', 'middle', 'leaf'} and all(rule['position'] == x for x in p for rule in p[x]['rules'])"
verify "the text output writes the origin beside the rule, in the closures of root, middle and leaf" \
  prints 3 "    leaf    Bash(git -C {state}/repos/beta log -20)" "$CHAIN"

echo "the parameters"
verify "the rule of leaf that names {repo} becomes three rules, one per value" \
  holds "$JSON" "[rule for rule in own('leaf') if 'log -20' in rule] == ['$LOG' + value + ' log -20)' for value in ('alpha', 'beta', 'gamma')]"
verify "… each marked with its value" \
  holds "$JSON" "[rule['parameters'] for rule in p['leaf']['rules'] if 'log -20' in rule['rule']] == [{'repo': value} for value in ('alpha', 'beta', 'gamma')]"
verify "a rule that names no parameter stays one rule" \
  holds "$JSON" "own('leaf').count('Bash({request} *)') == 1 and p['leaf']['rules'][0]['parameters'] == {}"
verify "no rule of any closure keeps {repo}" holds "$JSON" "not any('{repo}' in rule for x in p for rule in rules(x))"
verify "the placeholders of the loop stay as they are, marked not expanded" \
  holds "$JSON" "p['leaf']['rules'][0]['not_expanded'] == ['{request}'] and all(rule['not_expanded'] == ['{state}'] for rule in p['leaf']['rules'][1:])"
verify "the setup placeholder [[file]] stays as it is, marked not expanded" \
  holds "$JSON" "p['root']['rules'][1]['rule'] == 'Bash(cat {state}/notes/[[file]])' and p['root']['rules'][1]['not_expanded'] == ['{state}', '[[file]]']"
verify "the text output writes them as not expanded, in the closure of root alone" prints 1 "not expanded {state} [[file]]" "$CHAIN"
verify "… and the value beside the expanded rule, in the closures of root, middle and leaf" \
  prints 3 "repo=gamma · not expanded {state}" "$CHAIN"
verify "middle, without allowed_tools, has the default rule of the loop, marked default" \
  holds "$JSON" "own('middle') == ['Bash({request} *)'] and p['middle']['rules'][0]['default'] and not p['leaf']['rules'][0]['default']"

echo "the script position"
verify "tally has no rules and is marked not analyzable, with its command" \
  holds "$JSON" "p['tally']['rules'] == [] and p['tally']['not_analyzable'] == 'script: {code}/test/loop-node.py'"
verify "the closures of middle and root say it" holds "$JSON" "outside('middle') == ['tally'] and outside('root') == ['tally']"
verify "the closure of leaf, which does not reach it, does not" holds "$JSON" "outside('leaf') == []"
verify "the text output carries it with its command in the closures of root, middle and tally" \
  prints 3 "tally   not analyzable — script: {code}/test/loop-node.py" "$CHAIN"
"$CLOSURE" "$GRANTS/parameters" --json > "$TMP/parameters.json"
verify "a set of script positions only (test/grants/parameters): every position not analyzable, no rule" \
  holds "$TMP/parameters.json" "all(entry['not_analyzable'] and not entry['closure']['rules'] for entry in p.values()) and outside('root') == ['root', 'lead', 'reporter', 'fact']"

echo "a permission mode other than dontAsk"
MODE=$(variant mode "g['leaf']['launch']['permission_mode'] = 'acceptEdits'")
"$CLOSURE" "$MODE" --json > "$TMP/mode.json"
verify "leaf with acceptEdits is not analyzable: a command outside its rules can run" \
  holds "$TMP/mode.json" "p['leaf']['not_analyzable'].startswith('permission_mode acceptEdits')"
verify "… its rules stay in its closure and in the closure of root" holds "$TMP/mode.json" "len(own('leaf')) == 4 and rules('root')[-4:] == own('leaf')"
verify "… and the closure of root names it" holds "$TMP/mode.json" "outside('root') == ['leaf', 'tally']"

echo "the gate"
verify "a glob that matches only rules of leaf → exit 1" exits 1 "$CHAIN" --forbid 'Bash(git -C * log -20)'
verify "… and it names root, middle and leaf" forbids "$CHAIN" 'Bash(git -C * log -20)' 'root, middle, leaf'
verify "… each position with the rules it reaches and the position they come from" \
  prints 3 "root    via leaf    Bash(git -C {state}/repos/" "$CHAIN" --forbid 'Bash(git -C * log -20)'
verify "… and the script position it does not see inside" \
  bash -c "'$CLOSURE' '$CHAIN' --forbid 'Bash(git -C * log -20)' | grep -qxF 'not analyzable, outside the gate: tally — script: {code}/test/loop-node.py'"
verify "a glob that matches nothing → exit 0" exits 0 "$CHAIN" --forbid 'Bash(rm *)'
verify "a glob that matches only the rule of root → exit 1, it names root alone" forbids "$CHAIN" 'Bash(cat *)' 'root'
verify "a glob on one value of the parameter → exit 1" forbids "$CHAIN" '*/repos/beta *' 'root, middle, leaf'
verify "two globs, the second matches → exit 1" exits 1 "$CHAIN" --forbid 'Bash(rm *)' --forbid 'Bash(cat *)'
verify "two globs, neither matches → exit 0" exits 0 "$CHAIN" --forbid 'Bash(rm *)' --forbid 'Edit*'
PREFIX=$(variant prefix "g['leaf']['launch']['allowed_tools'].append('Bash(git *)')")
verify "the glob is on the text: the prefix rule Bash(git *) and the glob Bash(git push*) do not match → exit 0" \
  exits 0 "$PREFIX" --forbid 'Bash(git push*)'
verify "… the glob Bash(git [*]), a literal star, matches it → exit 1" forbids "$PREFIX" 'Bash(git [*])' 'root, middle, leaf'
verify "--json and --forbid together → exit 2" exits 2 "$CHAIN" --json --forbid 'Bash(rm *)'

echo "invalid grants"
for set in "$GRANTS"/invalid-*; do
  verify "$(basename "$set") → exit 2, the reason of the validator, no closure" rejects "$set"
done
verify "the gate on invalid grants → exit 2, no gate" rejects "$GRANTS/invalid-cycle" --forbid 'Bash(*'
verify "--json on invalid grants → exit 2, no JSON" rejects "$GRANTS/invalid-two-roots" --json
verify "the templates of the gazette, with [[repos]] among the values → exit 2" rejects "$ANTFARM/games/gazette/grants"
verify "a directory that does not exist → exit 2" exits 2 "$TMP/absent"

echo
if [[ $failed -gt 0 ]]; then
  echo "grant closure tests: $failed FAILED"
  exit 1
fi
echo "grant closure tests: all ok"
