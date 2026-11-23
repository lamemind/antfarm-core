#!/usr/bin/env bash
# Deterministic tests of install.sh, and of the loop launched from an installed version. No LLM.
#
#   antfarm-core/test/install-tests.sh
#
# It builds a repository of its own in a temporary folder from the files of this engine, with two
# tags and a branch, and installs from it into a prefix of its own: the installed versions of the
# user and the tags of the public repository are never touched. Then it starts the loop from the
# link of the prefix, on a state root of its own with the script grants, and reads in /proc which
# file the loop and its sessions run.
# INSTALL_TESTS_KEEP=1 keeps the temporary folder. Exits 1 if a test fails.
set -uo pipefail

ANTFARM=$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)
INSTALL="$ANTFARM/install.sh"
TMP=$(mktemp -d)
SOURCE="$TMP/source"
PREFIX="$TMP/prefix"
STATE="$TMP/state"
CYCLE=0.2

cleanup() {
  [[ -n ${LOOP_PID:-} ]] && kill -9 "$LOOP_PID" 2>/dev/null
  [[ -n ${SESSION_PID:-} ]] && kill -9 -- "-$SESSION_PID" 2>/dev/null
  if [[ -n ${INSTALL_TESTS_KEEP:-} ]]; then
    echo "INSTALL_TESTS_KEEP: the temporary folder stays in $TMP"
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

wait_for() { # <seconds> <command...>
  local limit=$((SECONDS + $1)); shift
  until "$@" > /dev/null 2>&1; do
    [[ $SECONDS -ge $limit ]] && return 1
    sleep 0.2
  done
}

install() { # <arguments...>: install.sh on the test repository and prefix; output in $TMP/install.out
  "$INSTALL" "$@" --from "$SOURCE" --prefix "$PREFIX" > "$TMP/install.out" 2>&1
}

current_is() { # <tag>: the link points at the directory of that version
  [[ $(readlink "$PREFIX/current") == "versions/$1" && -x $PREFIX/current/bin/loop.py ]]
}

installed_and_current() { # <tag>: install.sh installs it and the link points at it
  install "$1" && current_is "$1"
}

exits() { # <code> <arguments...>: install.sh exits with that code
  local expected=$1; shift
  install "$@"
  [[ $? -eq $expected ]]
}

no_partial() { # no temporary clone left next to the versions
  [[ -z $(find "$PREFIX/versions" -maxdepth 1 -name '.*partial*') ]]
}

not_installed() { # <name>: no directory for it and no partial clone
  [[ ! -e $PREFIX/versions/$1 ]] && no_partial
}

says() { # <text>: the output of the last install.sh carries it
  grep -qF "$1" "$TMP/install.out"
}

clean_version() { # <tag>: a clone at the tag, with no local change
  git -C "$PREFIX/versions/$1" tag --points-at HEAD | grep -qx "$1" \
    && [[ -z $(git -C "$PREFIX/versions/$1" status --porcelain) ]]
}

session_alive() { # <session>: the registry of the state root marks it alive
  [[ $(json_field "$STATE/loop/sessions.json" "d['sessions'].get('$1', {}).get('status')") == alive ]]
}

script_path() { # <pid>: the resolved path of the script the process runs, from /proc/<pid>/cmdline
  local script
  script=$(tr '\0' '\n' < "/proc/$1/cmdline" | sed -n 2p)
  readlink -f "$script"
}

not_under() { # <path> <directory>
  [[ $1 != "$2"/* ]]
}

json_field() { # <file> <python expression on d>
  python3 -c "import json, sys; d = json.load(open(sys.argv[1])); print($2)" "$1"
}

# The repository to install from: the files of this engine, committed, with the tags t1 and t2 and
# the branch b1. The working tree of the engine can carry changes not yet committed.
mkdir -p "$SOURCE"
(cd "$ANTFARM" && git ls-files -z --cached --others --exclude-standard | xargs -0 cp --parents -t "$SOURCE")
git -C "$SOURCE" init --quiet --initial-branch main
git -C "$SOURCE" -c user.name=test -c user.email=test@localhost add -A
git -C "$SOURCE" -c user.name=test -c user.email=test@localhost commit --quiet -m t1
git -C "$SOURCE" tag t1
echo "second version" >> "$SOURCE/README.md"
git -C "$SOURCE" -c user.name=test -c user.email=test@localhost commit --quiet -am t2
git -C "$SOURCE" tag t2
git -C "$SOURCE" branch b1

echo "install"
verify "install t1 → exit 0" exits 0 t1
verify "… current → versions/t1" current_is t1
verify "… the version is a clone at the tag t1, with no local change" clean_version t1
verify "install t2 → exit 0, current → versions/t2" installed_and_current t2
verify "… t1 stays installed" test -x "$PREFIX/versions/t1/bin/loop.py"
verify "… the two versions differ" bash -c "! cmp -s '$PREFIX/versions/t1/README.md' '$PREFIX/versions/t2/README.md'"
verify "install t1 again → exit 0, no new clone" exits 0 t1
verify "… it says that t1 is already installed" says "t1 already installed"
verify "… current → versions/t1 again: going back moves the link" current_is t1

echo "install: what does not install"
verify "a tag the repository does not have → exit 1" exits 1 t9
verify "… no directory for it, no partial clone" not_installed t9
verify "… current unchanged" current_is t1
verify "a branch → exit 1: only a tag installs" exits 1 b1
verify "… it says so" says "only a tag installs"
verify "… no directory for it" not_installed b1
verify "… current unchanged" current_is t1
verify "a name with a slash → exit 2" exits 2 ../t1
verify "no partial clone left after the failures" no_partial
echo "local change" >> "$PREFIX/versions/t2/README.md"
verify "an installed version with a local change → exit 1" exits 1 t2
verify "… it says so" says "local changes"
verify "… current unchanged" current_is t1
git -C "$PREFIX/versions/t2" checkout --quiet -- README.md

echo "the loop from the installed version"
mkdir -p "$STATE/grants"
cp "$ANTFARM/test/grants/script"/*.json "$STATE/grants/"
(cd "$STATE" && ANTFARM_STATE_DIR=. "$PREFIX/current/bin/loop.py" --cycle "$CYCLE" --no-notify >> "$STATE/loop.out" 2>&1) &
wait_for 15 test -s "$STATE/loop/status.json"
LOOP_PID=$(json_field "$STATE/loop/status.json" "d['pid']")
LOOP_SCRIPT=$(script_path "$LOOP_PID")
verify "the loop launched from the link runs versions/t1/bin/loop.py" test "$LOOP_SCRIPT" == "$PREFIX/versions/t1/bin/loop.py"
verify "… not a file under the working tree of the engine" not_under "$LOOP_SCRIPT" "$ANTFARM"
verify "… and its state stays in the state root" test -s "$STATE/registry/loop.jsonl"
verify "install t2 while the loop runs → current → versions/t2" installed_and_current t2
ANTFARM_STATE_DIR="$STATE" "$PREFIX/current/bin/loop.py" switch root on > "$STATE/switch.out" 2>&1
verify "switch root on → root-1 alive" wait_for 15 session_alive root-1
SESSION_PID=$(json_field "$STATE/loop/sessions.json" "d['sessions']['root-1']['pid']")
SESSION_SCRIPT=$(script_path "$SESSION_PID")
verify "… its session runs the script of t1, the version of the loop, not of the link" \
  test "$SESSION_SCRIPT" == "$PREFIX/versions/t1/test/loop-node.py"
kill -TERM "$LOOP_PID" 2>/dev/null
verify "SIGTERM: the loop exits" wait_for 15 bash -c "! kill -0 $LOOP_PID"
kill -9 -- "-$SESSION_PID" 2>/dev/null
LOOP_PID=
SESSION_PID=

echo
if [[ $failed -gt 0 ]]; then
  echo "INSTALL TESTS FAILED: $failed"
  exit 1
fi
echo "install tests: all ok"
