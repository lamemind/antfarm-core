#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["websockets==17.3"]
# ///
"""Client di prova del canale dell'harness: riceve gli eventi, li scrive, li conta, ne controlla l'ordine, misura il ritardo.

    orchestra/bin/ascolta.py [--porta N] [--eventi <file.jsonl>] [--fino-a-chiusura] [--scadenza S]
                             [--ritardo] [--origine URL]

Si collega a ws://127.0.0.1:N/eventi e riceve finché l'harness chiude il canale — il replay lo
chiude da sé —, oppure, con --fino-a-chiusura, fino al primo evento partita-chiusa, oppure fino
alla scadenza (default 120 s). Con --eventi scrive ogni evento così com'è arrivato, uno per riga.

Stampa su stdout un riepilogo JSON: eventi per classe/tipo, partite, indici dei messaggi in ordine
di arrivo, se l'arrivo dei messaggi rispetta l'ordine dei loro ts, nodi lanciati e chiusi, come si
è chiuso l'ascolto. Con --ritardo aggiunge, per i messaggi, il ritardo fra il ts della riga del log
e l'arrivo al client: n, mediana, massimo, in millisecondi. --origine manda l'intestazione Origin,
per provare il rifiuto delle pagine d'altra origine.

Esce 0 se l'ascolto si chiude come atteso — canale chiuso dall'harness, o partita-chiusa con
--fino-a-chiusura —, 1 alla scadenza o se la connessione è rifiutata, 2 su uso sbagliato.
"""

import argparse
import asyncio
import json
import statistics
import sys
import time
from datetime import datetime

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus


def istante(ts):
    return datetime.fromisoformat(ts).timestamp()


async def ricevi(argomenti, uscita):
    arrivi, fine, chiusura = [], "scadenza", None
    uri = f"ws://127.0.0.1:{argomenti.porta}/eventi"
    try:
        async with connect(uri, origin=argomenti.origine, max_size=None) as connessione:
            limite = time.monotonic() + argomenti.scadenza
            try:
                while True:
                    restante = limite - time.monotonic()
                    if restante <= 0:
                        break
                    try:
                        testo = await asyncio.wait_for(connessione.recv(), timeout=restante)
                    except TimeoutError:
                        break
                    arrivi.append((time.time(), testo))
                    if uscita is not None:
                        uscita.write(testo + "\n")
                        uscita.flush()
                    if argomenti.fino_a_chiusura and json.loads(testo).get("tipo") == "partita-chiusa":
                        fine = "partita-chiusa"
                        break
            except ConnectionClosed as chiuso:
                fine = "canale-chiuso"
                chiusura = {"codice": chiuso.rcvd.code if chiuso.rcvd else None,
                            "motivo": chiuso.rcvd.reason if chiuso.rcvd else None}
    except InvalidStatus as rifiuto:
        fine, chiusura = "rifiutato", {"codice": rifiuto.response.status_code, "motivo": rifiuto.response.body.decode(errors="replace").strip()}
    except OSError as errore:
        fine, chiusura = "irraggiungibile", {"codice": None, "motivo": str(errore)}
    return arrivi, fine, chiusura


def riepilogo(arrivi, fine, chiusura, con_ritardo):
    eventi = [(arrivo, json.loads(testo)) for arrivo, testo in arrivi]
    per_tipo, partite = {}, []
    for _, evento in eventi:
        chiave = f"{evento['classe']}/{evento['tipo']}"
        per_tipo[chiave] = per_tipo.get(chiave, 0) + 1
        if evento["partita"] not in partite:
            partite.append(evento["partita"])
    messaggi = [(arrivo, evento) for arrivo, evento in eventi if evento["classe"] == "messaggio"]
    istanti = [istante(evento["ts"]) for _, evento in messaggi]
    dati = {
        "fine": fine,
        "chiusura": chiusura,
        "eventi": len(eventi),
        "per_tipo": dict(sorted(per_tipo.items())),
        "partite": partite,
        "messaggi": len(messaggi),
        "indici_messaggi": [[evento["partita"], evento["indice"]] for _, evento in messaggi],
        "messaggi_in_ordine_di_ts": all(primo <= secondo for primo, secondo in zip(istanti, istanti[1:])),
        "nodi_lanciati": [[evento["partita"], evento["nodo"]] for _, evento in eventi if evento["tipo"] == "nodo-lanciato"],
        "nodi_chiusi": [[evento["partita"], evento["nodo"]] for _, evento in eventi if evento["tipo"] == "nodo-chiuso"],
    }
    if con_ritardo:
        ritardi = [(arrivo - istante(evento["ts"])) * 1000 for arrivo, evento in messaggi]
        dati["ritardo_ms"] = {
            "n": len(ritardi),
            "mediana": round(statistics.median(ritardi), 1) if ritardi else None,
            "massimo": round(max(ritardi), 1) if ritardi else None,
        }
    return dati


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--porta", type=int, default=8740)
    parser.add_argument("--eventi")
    parser.add_argument("--fino-a-chiusura", action="store_true")
    parser.add_argument("--scadenza", type=float, default=120.0)
    parser.add_argument("--ritardo", action="store_true")
    parser.add_argument("--origine")
    argomenti = parser.parse_args()
    uscita = open(argomenti.eventi, "w", encoding="utf-8") if argomenti.eventi else None
    try:
        arrivi, fine, chiusura = asyncio.run(ricevi(argomenti, uscita))
    finally:
        if uscita is not None:
            uscita.close()
    print(json.dumps(riepilogo(arrivi, fine, chiusura, argomenti.ritardo), ensure_ascii=False, indent=2))
    atteso = "partita-chiusa" if argomenti.fino_a_chiusura else "canale-chiuso"
    return 0 if fine == atteso else 1


if __name__ == "__main__":
    sys.exit(main())
