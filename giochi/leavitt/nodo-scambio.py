#!/usr/bin/env python3
"""Nodo script del gioco di Leavitt: scambia le carte coi vicini e dichiara il simbolo comune.

Serve alla partita a secco, che collauda regia, bus e oracolo senza LLM. Riceve e scrive solo col
bus, come le sessioni: la regia lo registra in nodi.json col suo (pid, starttime), e i vicini li
legge da `bus vista`.

Il protocollo è l'inondazione. Il nodo tiene le carte che conosce, nodo per nodo; la prima è la
sua, dalla regia. Ogni volta che impara una carta nuova manda a ogni vicino tutte quelle che
conosce. Quando l'intersezione delle carte note ha un simbolo solo, le carte note sono cinque: il
nodo dichiara e, perché ha già mandato ai vicini tutto ciò che sa, esce. Su un grafo connesso ogni
carta arriva a ogni nodo, quindi ogni nodo dichiara.

Uso:  nodo-scambio.py [--guasto sbagliata|muta]
  sbagliata — dichiara un simbolo della propria carta che non è quello comune
  muta — scambia come gli altri, ma non dichiara
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

import carte as libcarte

BUS = Path(__file__).resolve().parents[2] / "bin" / "bus"
ATTESA_RECV_S = 5


def bus(*argomenti, testo=None):
    return subprocess.run([str(BUS), *argomenti], input=testo, capture_output=True, text=True)


def chi_sono_e_vicini():
    """Il proprio id e i nodi a cui si può scrivere, regia esclusa, dalla vista che il bus stampa."""
    uscita = bus("vista")
    if uscita.returncode != 0:
        raise SystemExit(f"bus vista: exit {uscita.returncode}: {uscita.stderr}")
    io = re.search(r"^# Vista di (\S+) ·", uscita.stdout, re.MULTILINE).group(1)
    destinatari = re.search(r"^Puoi scrivere a: (.*)$", uscita.stdout, re.MULTILINE).group(1)
    return io, [nodo for nodo in destinatari.split(", ") if nodo not in ("regia", "(nessuno)")]


def ricevi():
    """(mittente, testo) del prossimo messaggio; richiama il bus finché non arriva."""
    while True:
        uscita = bus("recv", "--attesa", str(ATTESA_RECV_S))
        if uscita.returncode == 0:
            intestazione, _, testo = uscita.stdout.partition("\n---\n")
            mittente = re.match(r"messaggio \S+ da (\S+) ·", intestazione).group(1)
            return mittente, testo.rstrip("\n")
        if uscita.returncode != 3:
            raise SystemExit(f"bus recv: exit {uscita.returncode}: {uscita.stderr}")


def invia(destinatario, testo):
    uscita = bus("send", destinatario, testo=testo)
    print(uscita.stdout + uscita.stderr, end="")
    if uscita.returncode != 0:
        raise SystemExit(f"bus send {destinatario}: exit {uscita.returncode}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--guasto", choices=("sbagliata", "muta"))
    argomenti = parser.parse_args()
    sys.stdin.readline()  # la regia scrive «via» dopo averlo registrato in nodi.json
    io, vicini = chi_sono_e_vicini()
    note = {}
    while True:
        mittente, testo = ricevi()
        if mittente == "regia":
            nuove = {io: libcarte.codici_in(testo)}
        else:
            nuove = json.loads(testo)["carte"]
        imparate = {nodo: carta for nodo, carta in nuove.items() if nodo not in note}
        if not imparate:
            continue
        note.update(imparate)
        messaggio = json.dumps({"carte": note}, sort_keys=True)
        for vicino in vicini:
            invia(vicino, messaggio)
        comuni = set.intersection(*(set(carta) for carta in note.values()))
        if len(comuni) == 1:
            break
    comune = comuni.pop()
    if argomenti.guasto == "muta":
        print(f"{io}: simbolo comune {comune}, nessuna dichiarazione (guasto muta)")
        return 0
    simbolo = comune
    if argomenti.guasto == "sbagliata":
        simbolo = next(codice for codice in note[io] if codice != comune)
    invia("regia", simbolo)
    print(f"{io}: dichiarato {simbolo} · simbolo comune {comune} · carte note {len(note)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
