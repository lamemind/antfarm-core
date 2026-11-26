#!/usr/bin/env python3
"""Script session of the loop tests: it runs the instructions of the messages it receives.

It reads its mailbox with `request recv`, waiting; every line of a message is an instruction:

    spawn <position> [name=value...] [text]
                                 request spawn; the leading `name=value` words are its parameters,
                                 each one argument; in the text `|` stands for a new line, so a
                                 spawn can carry the instructions of the session it asks for
    escalate <arguments>         request escalate with the arguments split as a shell does, so
                                 that `--alternative 'a b'` stays one argument
    spend <tokens>               adds the tokens to tokens.txt in its working directory: the
                                 consumption that the loop reads for a script session
    sleep <seconds>
    exit                         ends the session

After the instructions of a message it reads the next one; without messages it waits for ever.
Every instruction and its outcome go to stdout, which the loop keeps in loop/records/<session>/.
"""

import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

REQUEST = Path(__file__).resolve().parent.parent / "bin" / "request"
TOKENS = Path("tokens.txt")
RECV_WAIT_S = "60"
PARAMETER = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,31}=\S*")


def request(*args):
    outcome = subprocess.run([str(REQUEST), *args], capture_output=True, text=True, stdin=subprocess.DEVNULL)
    print(f"$ request {' '.join(args)} → exit {outcome.returncode}: {outcome.stdout.strip()} {outcome.stderr.strip()}",
          flush=True)
    return outcome


def spend(tokens):
    spent = int(TOKENS.read_text(encoding="utf-8")) if TOKENS.exists() else 0
    temporary = TOKENS.with_name(".tokens.txt.tmp")
    temporary.write_text(str(spent + tokens), encoding="utf-8")
    temporary.replace(TOKENS)
    print(f"spent {tokens}, total {spent + tokens}", flush=True)


def run(line):
    word, _, rest = line.strip().partition(" ")
    if word == "spawn":
        position, _, text = rest.partition(" ")
        words = text.split(" ") if text else []
        parameters = []
        while words and PARAMETER.fullmatch(words[0]):
            parameters.append(words.pop(0))
        text = " ".join(words)
        request("spawn", position, *parameters, *([text.replace("|", "\n")] if text else []))
    elif word == "escalate":
        request("escalate", *shlex.split(rest))
    elif word == "spend":
        spend(int(rest))
    elif word == "sleep":
        time.sleep(float(rest))
    elif word == "exit":
        print("exit", flush=True)
        sys.exit(0)
    elif word:
        print(f"unknown instruction: {line}", flush=True)


def main():
    print(f"started in {Path.cwd()}", flush=True)
    while True:
        outcome = request("recv", "--wait", RECV_WAIT_S)
        if outcome.returncode == 0:
            _, _, text = outcome.stdout.partition("\n---\n")
            for line in text.splitlines():
                run(line)
        elif outcome.returncode != 3:
            time.sleep(1)


if __name__ == "__main__":
    main()
