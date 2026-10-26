#!/usr/bin/env python3
"""Nodo script del telefono senza fili: riceve un messaggio dal bus e lo inoltra identico al successore.

Serve alla partita a secco, che collauda regia, bus e oracolo senza LLM. Riceve come ogni nodo,
con `bus recv`: la regia lo registra in nodi.json col suo (pid, starttime) come fa con le sessioni.

Uso:  nodo-inoltro.py <successore>
"""

import subprocess
import sys
from pathlib import Path

BUS = Path(__file__).resolve().parents[2] / "bin" / "bus"


def main():
    if len(sys.argv) != 2:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    sys.stdin.readline()  # la regia scrive «via» dopo averlo registrato in nodi.json
    while True:
        ricevuto = subprocess.run([str(BUS), "recv"], capture_output=True, text=True)
        if ricevuto.returncode == 0:
            break
        if ricevuto.returncode != 3:
            print(ricevuto.stderr, file=sys.stderr)
            return 1
    _, _, testo = ricevuto.stdout.partition("\n---\n")
    inviato = subprocess.run([str(BUS), "send", sys.argv[1]], input=testo, capture_output=True, text=True)
    print(inviato.stdout + inviato.stderr, end="")
    return inviato.returncode


if __name__ == "__main__":
    sys.exit(main())
