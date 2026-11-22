#!/usr/bin/env python3
"""Script node of the exquisite corpse: it receives a turn, writes a line, passes it to the successor.

It serves the dry-run match, which tests director, bus and oracle without an LLM. It receives and
writes only with the bus, like the sessions: the director registers it in nodes.json, and the node
reads its successor from `bus view`, which always tells the current graph.

A node that receives from the director a message that is not a turn rereads the view, as the
notice says. A node that receives turn 16 sends its line to the director and closes. If a send is
rejected the node rereads the view and retries once: the match reaches the end, and the rejection
stays in the log.

Before writing the node waits one second, like a session that thinks: after turn 6 the director
writes the graph and notifies C, and the notice must reach C before turn 8.

Usage:  node-line.py [--ignore-notice]
  --ignore-notice — does not reread the view when the director sends the notice: the negative test of the removed edge
"""

import argparse
import re
import subprocess
import sys
import time
from pathlib import Path

import game

BUS = Path(__file__).resolve().parents[2] / "bin" / "bus"
RECV_WAIT_S = 5
PAUSE_BEFORE_WRITING_S = 1.0


def bus(*args, text=None):
    return subprocess.run([str(BUS), *args], input=text, capture_output=True, text=True)


def who_am_i_and_successor():
    """Its own id and the first node, director excluded, that the current view lets it write to."""
    output = bus("view")
    if output.returncode != 0:
        raise SystemExit(f"bus view: exit {output.returncode}: {output.stderr}")
    me = re.search(r"^# Vista di (\S+) ·", output.stdout, re.MULTILINE).group(1)
    recipients = re.search(r"^Puoi scrivere a: (.*)$", output.stdout, re.MULTILINE).group(1)
    return me, next(node for node in recipients.split(", ") if node != "director")


def receive():
    """(sender, text) of the next message; calls the bus again until one arrives."""
    while True:
        output = bus("recv", "--wait", str(RECV_WAIT_S))
        if output.returncode == 0:
            header, _, text = output.stdout.partition("\n---\n")
            sender = re.match(r"messaggio \S+ da (\S+) ·", header).group(1)
            return sender, text.rstrip("\n")
        if output.returncode != 3:
            raise SystemExit(f"bus recv: exit {output.returncode}: {output.stderr}")


def send(recipient, text):
    output = bus("send", recipient, text=text)
    print(output.stdout + output.stderr, end="", flush=True)
    return output.returncode == 0


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ignore-notice", action="store_true")
    args = parser.parse_args()
    sys.stdin.readline()  # the director writes «via» after registering it in nodes.json
    me, successor = who_am_i_and_successor()
    while True:
        sender, text = receive()
        turn = game.declared_turn(text)
        if sender == "director" and turn is None:
            if not args.ignore_notice:
                _, successor = who_am_i_and_successor()
            print(f"{me}: notice from director, successor {successor}", flush=True)
            continue
        time.sleep(PAUSE_BEFORE_WRITING_S)
        line = f"turno {turn + 1}: riga di {me} dopo la {turn}"
        if turn == game.LAST_TURN:
            send("director", line)
            return 0
        if not send(successor, line):
            _, successor = who_am_i_and_successor()
            if not send(successor, line):
                raise SystemExit(f"{me}: send rejected even after rereading the view")


if __name__ == "__main__":
    sys.exit(main())
