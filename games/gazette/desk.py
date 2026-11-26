#!/usr/bin/env python3
"""The desk of the gazette: the script session at the root of the org chart of a match.

It asks the spawn of the editor, then waits in its mailbox with `request recv`. The first escalation
from an editor session is the gazette: the desk writes its text, as it arrived, in gazette.md of the
state root — the directory of the match — and ends. Every other message it prints and ignores.

The desk does not judge the gazette: the oracle of games/gazette/report.py reads gazette.md.
Everything it does goes to stdout, which the loop keeps in loop/records/<session>/output.txt.
"""

import os
import re
import subprocess
import sys
from pathlib import Path

REQUEST = Path(__file__).resolve().parents[2] / "bin" / "request"
GAZETTE = "gazette.md"
RECV_WAIT_S = "840"
HEADER = re.compile(r"^messaggio (\S+) da (\S+)( · escalation)? · posizione \S+$")


def request(*arguments):
    outcome = subprocess.run([str(REQUEST), *arguments], capture_output=True, text=True, stdin=subprocess.DEVNULL)
    print(f"$ request {' '.join(arguments)} → exit {outcome.returncode}: "
          f"{outcome.stdout.strip()[:300]} {outcome.stderr.strip()}", flush=True)
    return outcome


def write_gazette(state, text):
    """gazette.md through a temporary file and a rename: whoever waits for it never reads half."""
    target = state / GAZETTE
    temporary = state / f".{GAZETTE}.tmp"
    temporary.write_text(text.rstrip("\n") + "\n", encoding="utf-8")
    os.replace(temporary, target)
    return target


def main():
    state = Path(os.environ["ANTFARM_STATE_DIR"])
    print(f"desk started, state {state}", flush=True)
    if request("spawn", "editor").returncode != 0:
        return 1
    while True:
        outcome = request("recv", "--wait", RECV_WAIT_S)
        if outcome.returncode == 3:
            continue
        if outcome.returncode != 0:
            return 1
        header, _, text = outcome.stdout.partition("\n---\n")
        hit = HEADER.match(header.strip())
        if hit and hit.group(3) and hit.group(2).startswith("editor-"):
            print(f"gazette from {hit.group(2)}, message {hit.group(1)}: {write_gazette(state, text)}", flush=True)
            return 0
        print(f"not a gazette, ignored: {header.strip()}", flush=True)


if __name__ == "__main__":
    sys.exit(main())
