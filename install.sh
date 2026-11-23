#!/usr/bin/env bash
# Installs a version of antfarm-core from a tag and makes it the current one.
#
#   antfarm-core/install.sh <tag> [--from <repository>] [--prefix <directory>]
#
# It clones the tag into <prefix>/versions/<tag>/ and points the link <prefix>/current at that
# directory. Whoever launches the engine launches it from the link, never from a working tree:
#
#   ANTFARM_STATE_DIR=. ~/.local/share/antfarm/current/bin/loop.py
#
# Prefix: ${XDG_DATA_HOME:-$HOME/.local/share}/antfarm. Repository: the public repository of the
# engine; --from takes any path or URL that `git clone` takes, for the tests.
#
# Only a tag installs: a branch moves, and the version under a name would no longer be one. A
# version already installed is not cloned again: the script checks that its directory is still that
# tag, with no local change, and only points the link. The old versions stay, so going back is
# installing the old tag again, which moves the link and nothing else.
#
# The clone goes to a temporary directory next to the versions and takes its name only when it is
# complete, and the link is replaced with a rename: a failure leaves the current version as it was.
# The engine derives its code root from the resolved path of its own files (graph.code_root), so a
# process started from the link keeps the version it started with when the link moves, and so do
# the sessions it launches, which receive the paths of that version.
#
# Exits 0 when the tag is installed and current, 1 when the tag cannot be installed, 2 on a usage
# error.
set -euo pipefail

REPOSITORY=https://github.com/lamemind/antfarm-core.git
PREFIX=${XDG_DATA_HOME:-$HOME/.local/share}/antfarm

usage() {
  echo "usage: $0 <tag> [--from <repository>] [--prefix <directory>]" >&2
  exit 2
}

fail() {
  echo "install: $*" >&2
  exit 1
}

[[ $# -ge 1 ]] || usage
TAG=$1
shift
while [[ $# -gt 0 ]]; do
  case $1 in
    --from) [[ $# -ge 2 ]] || usage; REPOSITORY=$2; shift 2 ;;
    --prefix) [[ $# -ge 2 ]] || usage; PREFIX=$2; shift 2 ;;
    *) usage ;;
  esac
done
# The tag becomes a directory name: no slash, no leading dot, no `..`.
[[ $TAG =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ && $TAG != *..* ]] || { echo "install: $TAG is not a tag name this script installs" >&2; exit 2; }

VERSIONS="$PREFIX/versions"
TARGET="$VERSIONS/$TAG"
mkdir -p "$VERSIONS"

# The tags that point at the HEAD of a clone, one per line.
tags_at_head() { # <directory>
  git -C "$1" tag --points-at HEAD
}

if [[ -e $TARGET ]]; then
  tags_at_head "$TARGET" 2>/dev/null | grep -qxF "$TAG" \
    || fail "$TARGET exists and is not a clone at the tag $TAG: remove it by hand"
  [[ -z $(git -C "$TARGET" status --porcelain) ]] \
    || fail "$TARGET carries local changes, so it is no longer the version $TAG: remove it by hand"
  echo "$TAG already installed in $TARGET"
else
  PARTIAL=$(mktemp -d "$VERSIONS/.$TAG.partial.XXXXXX")
  trap 'rm -rf "$PARTIAL"' EXIT
  git -c advice.detachedHead=false clone --quiet --depth 1 --branch "$TAG" "$REPOSITORY" "$PARTIAL/$TAG" \
    || fail "git clone of $TAG from $REPOSITORY failed"
  tags_at_head "$PARTIAL/$TAG" | grep -qxF "$TAG" \
    || fail "$TAG is not a tag of $REPOSITORY: only a tag installs"
  mv -T "$PARTIAL/$TAG" "$TARGET"
  rm -rf "$PARTIAL"
  trap - EXIT
  echo "$TAG installed in $TARGET, from $REPOSITORY"
fi

LINK="$PREFIX/current"
NEXT="$PREFIX/.current.$$"
ln -sfn "versions/$TAG" "$NEXT"
mv -T "$NEXT" "$LINK"
echo "current → $TAG: launch with $LINK/bin/loop.py"
