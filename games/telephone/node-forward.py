#!/usr/bin/env python3
"""Script node of the telephone game: receives a message from the bus and forwards it unchanged to the successor.

It serves the dry-run match, the acceptance test of director, bus and oracle without LLMs. It receives like
every node, with `bus recv`: the director registers it in nodes.json with its (pid, starttime), as it does with sessions.

Usage:  node-forward.py <successor>
"""

import subprocess
import sys
from pathlib import Path

BUS = Path(__file__).resolve().parents[2] / "bin" / "bus"


def main():
    if len(sys.argv) != 2:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    sys.stdin.readline()  # the director writes «via» after registering it in nodes.json
    while True:
        received_output = subprocess.run([str(BUS), "recv"], capture_output=True, text=True)
        if received_output.returncode == 0:
            break
        if received_output.returncode != 3:
            print(received_output.stderr, file=sys.stderr)
            return 1
    _, _, text = received_output.stdout.partition("\n---\n")
    sent = subprocess.run([str(BUS), "send", sys.argv[1]], input=text, capture_output=True, text=True)
    print(sent.stdout + sent.stderr, end="")
    return sent.returncode


if __name__ == "__main__":
    sys.exit(main())
