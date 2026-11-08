#!/usr/bin/env python3
"""Nodo script del cadavere squisito: riceve un turno, scrive una riga, la passa al successore.

Serve alla partita a secco, che collauda regia, bus e oracolo senza LLM. Riceve e scrive solo col
bus, come le sessioni: la regia lo registra in nodi.json, e il successore lo legge da `bus vista`,
che dice sempre il grafo corrente.

Chi riceve dalla regia un messaggio che non è un turno rilegge la vista, come dice l'avviso. Chi
riceve il turno 16 manda la sua riga alla regia e chiude. Se un invio è rifiutato il nodo rilegge
la vista e riprova una volta: la partita arriva in fondo, e il rifiuto resta nel log.

Prima di scrivere il nodo aspetta un secondo, come una sessione che pensa: dopo il turno 6 la regia
scrive il grafo e avvisa C, e l'avviso deve arrivare a C prima del turno 8.

Uso:  nodo-riga.py [--ignora-avviso]
  --ignora-avviso — non rilegge la vista quando la regia avvisa: la contro-prova dell'arco tolto
"""

import argparse
import re
import subprocess
import sys
import time
from pathlib import Path

import gioco

BUS = Path(__file__).resolve().parents[2] / "bin" / "bus"
ATTESA_RECV_S = 5
PAUSA_PRIMA_DI_SCRIVERE_S = 1.0


def bus(*argomenti, testo=None):
    return subprocess.run([str(BUS), *argomenti], input=testo, capture_output=True, text=True)


def chi_sono_e_successore():
    """Il proprio id e il primo nodo, regia esclusa, a cui la vista corrente permette di scrivere."""
    uscita = bus("vista")
    if uscita.returncode != 0:
        raise SystemExit(f"bus vista: exit {uscita.returncode}: {uscita.stderr}")
    io = re.search(r"^# Vista di (\S+) ·", uscita.stdout, re.MULTILINE).group(1)
    destinatari = re.search(r"^Puoi scrivere a: (.*)$", uscita.stdout, re.MULTILINE).group(1)
    return io, next(nodo for nodo in destinatari.split(", ") if nodo != "regia")


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
    print(uscita.stdout + uscita.stderr, end="", flush=True)
    return uscita.returncode == 0


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ignora-avviso", action="store_true")
    argomenti = parser.parse_args()
    sys.stdin.readline()  # la regia scrive «via» dopo averlo registrato in nodi.json
    io, successore = chi_sono_e_successore()
    while True:
        mittente, testo = ricevi()
        turno = gioco.turno_dichiarato(testo)
        if mittente == "regia" and turno is None:
            if not argomenti.ignora_avviso:
                _, successore = chi_sono_e_successore()
            print(f"{io}: avviso della regia, successore {successore}", flush=True)
            continue
        time.sleep(PAUSA_PRIMA_DI_SCRIVERE_S)
        riga = f"turno {turno + 1}: riga di {io} dopo la {turno}"
        if turno == gioco.ULTIMO_TURNO:
            invia("regia", riga)
            return 0
        if not invia(successore, riga):
            _, successore = chi_sono_e_successore()
            if not invia(successore, riga):
                raise SystemExit(f"{io}: invio rifiutato anche dopo aver riletto la vista")


if __name__ == "__main__":
    sys.exit(main())
