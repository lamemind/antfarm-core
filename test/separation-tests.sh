#!/usr/bin/env bash
# The engine does not name the tools it is developed with: no file of antfarm-core, and no path,
# carries the name of the task-workflow plugin of the projects that build it. The engine is a public
# product that other projects install, and the name of a private tool in its source is a coupling
# that nobody declared — a variable read from the environment, a path, a command.
#
#   antfarm-core/test/separation-tests.sh
#
# The name is written below in two pieces, so this file does not carry it. The scan reads every
# file of the engine that git tracks or would track (ignored files excluded), as text, without case,
# and finds the name when no letter precedes it: an environment variable or a plugin name counts, a
# longer word that ends with the same letters does not. First the scan is tried on a temporary copy
# of the engine with the name planted in a file and in a path, where it must fail; then it runs on
# the engine. Exits 1 if a test fails.
set -uo pipefail

ANTFARM=$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)
NAME="lo""om"
PATTERN="(^|[^[:alpha:]])$NAME"
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

files() { # <engine directory>: the files of the engine, one per line, relative to it
  if git -C "$1" rev-parse --show-toplevel > /dev/null 2>&1; then
    git -C "$1" ls-files --cached --others --exclude-standard
  else
    (cd "$1" && find . -type f -not -path './.git/*' -not -path '*/__pycache__/*' -not -path './.ruff_cache/*' | sed 's|^\./||')
  fi
}

hits() { # <engine directory>: every file whose content or path names it, one per line
  local engine=$1
  files "$engine" | while IFS= read -r file; do
    [[ -f $engine/$file ]] || continue
    if grep -qiE "$PATTERN" <<< "$file" || grep -qIiE "$PATTERN" "$engine/$file"; then
      echo "$file"
    fi
  done
}

clean() { # <engine directory>: no file names it
  [[ -z $(hits "$1") ]]
}

not_clean() { # <engine directory>: at least one file names it
  ! clean "$1"
}

names() { # <engine directory> <file>: the scan finds that file. Without -q: under pipefail, a grep
  # that stops at the first line would make hits fail on a closed pipe
  hits "$1" | grep -xF "$2" > /dev/null
}

# A copy of the engine, without git: the scan falls back on the files of the directory.
COPY="$TMP/engine"
mkdir -p "$COPY"
(cd "$ANTFARM" && files . | while IFS= read -r file; do [[ -f $file ]] && cp --parents -t "$COPY" "$file"; done)

echo "the scan, on a copy of the engine"
verify "the copy as it is → no file names it" clean "$COPY"
printf 'VARIABLES = ["HOME", "PATH", "%s_TASK"]\n' "${NAME^^}" > "$COPY/bin/planted.py"
verify "an environment variable planted in bin/ → the scan names the file" names "$COPY" bin/planted.py
verify "… and the copy is no longer clean" not_clean "$COPY"
rm "$COPY/bin/planted.py"
printf 'plugin: %s-works\n' "$NAME" > "$COPY/README-planted.md"
verify "a plugin name in a README → named" names "$COPY" README-planted.md
rm "$COPY/README-planted.md"
mkdir -p "$COPY/test/${NAME}"
echo "neutral text" > "$COPY/test/${NAME}/file.txt"
verify "a path that names it, with neutral content → named" names "$COPY" "test/${NAME}/file.txt"
rm -r "${COPY:?}/test/${NAME}"
printf 'the b%s of a flower\n' "$NAME" > "$COPY/bin/flower.txt"
verify "a longer word that ends with it → not named" clean "$COPY"
rm "$COPY/bin/flower.txt"

echo "the engine"
HITS=$(hits "$ANTFARM")
verify "no file of antfarm-core names it${HITS:+: $(tr '\n' ' ' <<< "$HITS")}" test -z "$HITS"

echo
if [[ $failed -gt 0 ]]; then
  echo "SEPARATION TESTS FAILED: $failed"
  exit 1
fi
echo "separation tests: all ok"
