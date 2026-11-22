#!/usr/bin/env python3
"""Script node of Leavitt's game: exchanges cards with its neighbors and declares the common symbol.

It serves the dry-run match, the acceptance test of director, bus and oracle without LLMs. It
receives and writes only with the bus, like the sessions: the director registers it in nodes.json
with its (pid, starttime), and it reads its neighbors from `bus view`.

The protocol is flooding. The node keeps the cards it knows, node by node; the first one is its
own, from the director. Every time it learns a new card it sends each neighbor all the cards it
knows. When the intersection of the known cards has a single symbol, the known cards are five: the
node declares and, since it has already sent its neighbors all it knows, exits. On a connected
graph every card reaches every node, so every node declares.

Usage:  node-exchange.py [--fault wrong|mute]
  wrong — declares a symbol of its own card that is not the common one
  mute — exchanges like the others, but does not declare
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

import cards as libcards

BUS = Path(__file__).resolve().parents[2] / "bin" / "bus"
RECV_WAIT_S = 5


def bus(*args, text=None):
    return subprocess.run([str(BUS), *args], input=text, capture_output=True, text=True)


def who_am_i_and_neighbors():
    """Its own id and the nodes it can write to, director excluded, from the view that the bus prints."""
    output = bus("view")
    if output.returncode != 0:
        raise SystemExit(f"bus view: exit {output.returncode}: {output.stderr}")
    me = re.search(r"^# Vista di (\S+) ·", output.stdout, re.MULTILINE).group(1)
    recipients = re.search(r"^Puoi scrivere a: (.*)$", output.stdout, re.MULTILINE).group(1)
    return me, [node for node in recipients.split(", ") if node not in ("director", "(nessuno)")]


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
    print(output.stdout + output.stderr, end="")
    if output.returncode != 0:
        raise SystemExit(f"bus send {recipient}: exit {output.returncode}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--fault", choices=("wrong", "mute"))
    args = parser.parse_args()
    sys.stdin.readline()  # the director writes «via» after registering it in nodes.json
    me, neighbors = who_am_i_and_neighbors()
    known = {}
    while True:
        sender, text = receive()
        if sender == "director":
            new_cards = {me: libcards.codes_in(text)}
        else:
            new_cards = json.loads(text)["carte"]
        learned = {node: card for node, card in new_cards.items() if node not in known}
        if not learned:
            continue
        known.update(learned)
        message = json.dumps({"carte": known}, sort_keys=True)
        for neighbor in neighbors:
            send(neighbor, message)
        common_symbols = set.intersection(*(set(card) for card in known.values()))
        if len(common_symbols) == 1:
            break
    common = common_symbols.pop()
    if args.fault == "mute":
        print(f"{me}: common symbol {common}, no declaration (fault mute)")
        return 0
    symbol = common
    if args.fault == "wrong":
        symbol = next(code for code in known[me] if code != common)
    send("director", symbol)
    print(f"{me}: declared {symbol} · common symbol {common} · known cards {len(known)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
