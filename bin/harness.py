#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["websockets==17.3"]
# ///
"""Harness di osservabilità del motore: legge le sorgenti del motore, ne fa eventi, li pubblica su un WebSocket.

    orchestra/bin/harness.py vivo [--porta N]
        segue dal vivo le partite aperte all'accensione e quelle che nascono dopo. Legge il log
        dei messaggi dall'inizio e poi ne segue la coda, e le cartelle delle partite per
        apertura, lancio e chiusura dei nodi, chiusura della partita: parte prima o dopo la regia
        senza perdere righe. Ogni client riceve prima gli eventi già emessi, poi quelli nuovi.

    orchestra/bin/harness.py replay <partita>... [--velocita V] [--porta N]
        rigioca partite già giocate, una dopo l'altra, in ordine di ts e coi loro tempi divisi
        per V (default 1); V = 0 emette senza attesa. Ogni client riceve il replay intero
        dall'inizio, poi il canale si chiude.

    orchestra/bin/harness.py flusso <eventi.jsonl> [--velocita V] [--porta N]
        come replay, ma da un file di eventi già fatti, inviati così come sono scritti: il
        collaudo di ciò che il motore non produce ancora, come lo spawn.

Tre classi di evento, schema in schema/evento.schema.json: sessione (partita-aperta,
partita-chiusa, nodo-lanciato, nodo-chiuso, da meta.json e nodi.json), messaggio (consegnato,
rifiutato, letto: le tre specie di riga del log dei messaggi), spawn. Ogni evento avvolge la riga
sorgente intatta nel campo `riga`; partita-aperta porta meta.json e grafo.json della partita, e la
vista disegna da lì. Le righe del log senza partita (mittente-ignoto) non diventano eventi: si
contano, e il conteggio esce su stderr alla chiusura.

Una porta sola, legata a 127.0.0.1: `/` serve la vista, `/canale.json` il modo e la velocità,
`/eventi` il WebSocket. Il WebSocket rifiuta le pagine d'altra origine, perché il log porta il
testo integrale dei messaggi. L'harness non scrive in registro/ né in partite/: legge soltanto.

Porta di default 8740; --porta 0 la fa scegliere al sistema. La prima riga su stdout porta
l'indirizzo. Si ferma con SIGINT o SIGTERM.
"""

import argparse
import asyncio
import json
import signal
import sys
from datetime import datetime
from http import HTTPStatus
from pathlib import Path

from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

sys.path.insert(0, str(Path(__file__).resolve().parent))
import grafo as libgrafo  # noqa: E402

RADICE = libgrafo.radice_orchestra()
PARTITE = RADICE / "partite"
LOG_MESSAGGI = RADICE / "registro" / "messaggi.jsonl"
CARTELLA_VISTA = RADICE / "vista"
INDIRIZZO = "127.0.0.1"
PORTA_DEFAULT = 8740
PASSO_DIRETTA_S = 0.1
PERCORSO_EVENTI = "/eventi"
FILE_VISTA = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/vista.js": ("vista.js", "text/javascript; charset=utf-8"),
    "/vista.css": ("vista.css", "text/css; charset=utf-8"),
}
# A parità di ts, l'ordine del ciclo di vita: lancio prima dei messaggi, chiusura dopo.
PRIORITA_A_PARI_TS = {"nodo-lanciato": 0, "consegnato": 1, "rifiutato": 1, "letto": 1, "spawn": 1, "nodo-chiuso": 2}


# <editor-fold defaultstate="collapsed" desc="Sorgenti: file del motore → eventi">
def leggi_json(percorso):
    """Il contenuto di un file JSON; None se manca o è a metà scrittura."""
    try:
        return json.loads(percorso.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def righe_del_log(contenuto):
    """(indice, riga) per ogni riga del log, con l'indice da 1 come numero di riga del file.
    Il taglio è sul solo a capo: il bus scrive senza escape ASCII, e un testo può portare
    separatori di riga Unicode che splitlines() prenderebbe per a capo."""
    righe = []
    for indice, blocco in enumerate(contenuto.split(b"\n"), start=1):
        if not blocco.strip():
            continue
        try:
            righe.append((indice, json.loads(blocco)))
        except json.JSONDecodeError:
            continue
    return righe


def istante(ts):
    return datetime.fromisoformat(ts).timestamp()


def evento_messaggio(indice, riga):
    return {"classe": "messaggio", "tipo": riga["esito"], "ts": riga["ts"], "partita": riga["partita"],
            "indice": indice, "riga": riga}


def evento_partita_aperta(cartella, meta, grafo):
    return {"classe": "sessione", "tipo": "partita-aperta", "ts": meta["inizio"], "partita": cartella.name,
            "riga": {"meta": meta, "grafo": grafo}}


def evento_partita_chiusa(cartella, meta):
    return {"classe": "sessione", "tipo": "partita-chiusa", "ts": meta["fine"], "partita": cartella.name, "riga": meta}


def evento_nodo(tipo, cartella, id_nodo, voce):
    campo = "lanciato" if tipo == "nodo-lanciato" else "chiuso"
    return {"classe": "sessione", "tipo": tipo, "ts": voce[campo], "partita": cartella.name, "nodo": id_nodo,
            "riga": voce}


def eventi_dei_nodi(cartella, nodi):
    """nodo-lanciato per ogni voce con `lanciato`, nodo-chiuso per ogni voce con `chiuso`. La regia
    non porta `lanciato`: è lei che apre la partita."""
    eventi = []
    for id_nodo, voce in (nodi or {}).get("nodi", {}).items():
        if voce.get("lanciato"):
            eventi.append(evento_nodo("nodo-lanciato", cartella, id_nodo, voce))
        if voce.get("chiuso"):
            eventi.append(evento_nodo("nodo-chiuso", cartella, id_nodo, voce))
    return eventi
# </editor-fold>


# <editor-fold defaultstate="collapsed" desc="Replay: una partita già giocata, rigiocata coi suoi tempi">
def cartella_partita(argomento):
    for candidata in (PARTITE / argomento, Path(argomento)):
        if (candidata / "meta.json").is_file() and (candidata / "grafo.json").is_file():
            return candidata.resolve()
    raise SystemExit(f"partita non trovata: {argomento} (né sotto {PARTITE} né come cartella)")


def eventi_partita(cartella, righe_partita):
    """Gli eventi di una partita chiusa nell'ordine del replay: apertura, poi lanci, messaggi e
    chiusure dei nodi in ordine di ts, poi la chiusura della partita. Apertura e chiusura stanno
    agli estremi perché meta.json porta i secondi e il log i millesimi: la chiusura troncata al
    secondo cadrebbe prima dell'ultima lettura."""
    meta, grafo = leggi_json(cartella / "meta.json"), leggi_json(cartella / "grafo.json")
    centro = eventi_dei_nodi(cartella, leggi_json(cartella / "nodi.json"))
    centro += [evento_messaggio(indice, riga) for indice, riga in righe_partita]
    centro.sort(key=lambda evento: (istante(evento["ts"]), PRIORITA_A_PARI_TS[evento["tipo"]]))
    chiusura = [evento_partita_chiusa(cartella, meta)] if meta.get("fine") else []
    return [evento_partita_aperta(cartella, meta, grafo)] + centro + chiusura


def programma(gruppi, velocita):
    """(secondi dall'inizio del replay, testo dell'evento) per ogni evento. Dentro un gruppo i tempi
    sono quelli dei ts divisi per la velocità, mai all'indietro; fra un gruppo e il successivo
    nessuna pausa."""
    risultato, scostamento = [], 0.0
    for eventi in gruppi:
        if not eventi:
            continue
        origine, ultimo = istante(eventi[0]["ts"]), scostamento
        for evento in eventi:
            quando = scostamento + ((istante(evento["ts"]) - origine) / velocita if velocita > 0 else 0.0)
            ultimo = max(ultimo, quando)
            risultato.append((ultimo, json.dumps(evento, ensure_ascii=False)))
        scostamento = ultimo
    return risultato


def programma_da_flusso(percorso, velocita):
    """Il programma di un file di eventi: ordine del file, tempi dai ts, testo inviato com'è scritto."""
    testi = [testo for testo in Path(percorso).read_text(encoding="utf-8").split("\n") if testo.strip()]
    eventi = [json.loads(testo) for testo in testi]
    risultato, ultimo = [], 0.0
    origine = istante(eventi[0]["ts"]) if eventi else 0.0
    for evento, testo in zip(eventi, testi):
        quando = (istante(evento["ts"]) - origine) / velocita if velocita > 0 else 0.0
        ultimo = max(ultimo, quando)
        risultato.append((ultimo, testo))
    return risultato


async def rigioca(connessione, eventi_programmati):
    ciclo = asyncio.get_running_loop()
    partenza = ciclo.time()
    try:
        for quando, testo in eventi_programmati:
            attesa = partenza + quando - ciclo.time()
            if attesa > 0:
                await asyncio.sleep(attesa)
            await connessione.send(testo)
        await connessione.close(1000, "fine replay")
    except ConnectionClosed:
        pass
# </editor-fold>


# <editor-fold defaultstate="collapsed" desc="Dal vivo: coda del log e cartelle delle partite">
class Seguita:
    def __init__(self, cartella):
        self.cartella = cartella
        self.lanciati = set()
        self.chiusi = set()
        self.chiusa = False


class Diretta:
    """Segue il motore mentre gioca. A ogni giro legge le righe nuove del log, poi guarda le
    cartelle delle partite. Prima di emettere una chiusura — di un nodo o della partita — rilegge
    il log: la regia scrive la chiusura dopo l'ultima riga di quella partita, quindi la rilettura
    porta ogni riga che la precede."""

    def __init__(self):
        self.posizione = 0
        self.resto = b""
        self.righe_lette = 0
        self.seguite = {}
        self.ignorate = set()
        self.senza_partita = 0
        self.senza_cartella = 0
        self.emessi = []
        self.iscritti = set()

    def accendi(self):
        """Le partite già chiuse all'accensione non si seguono: sono materia del replay."""
        if PARTITE.is_dir():
            for cartella in PARTITE.iterdir():
                if (leggi_json(cartella / "nodi.json") or {}).get("stato") == "chiusa":
                    self.ignorate.add(cartella.name)

    def pubblica(self, evento):
        testo = json.dumps(evento, ensure_ascii=False)
        self.emessi.append(testo)
        for coda in self.iscritti:
            coda.put_nowait(testo)

    def iscrivi(self):
        """Una coda che parte con gli eventi già emessi e riceve i nuovi: copia e iscrizione stanno
        nello stesso passo sincrono, quindi nessun evento cade fra le due."""
        coda = asyncio.Queue()
        for testo in self.emessi:
            coda.put_nowait(testo)
        self.iscritti.add(coda)
        return coda

    def righe_nuove(self):
        try:
            with open(LOG_MESSAGGI, "rb") as log:
                log.seek(self.posizione)
                dati = log.read()
        except FileNotFoundError:
            return []
        self.posizione += len(dati)
        blocchi = (self.resto + dati).split(b"\n")
        self.resto = blocchi.pop()
        primo = self.righe_lette + 1
        self.righe_lette += len(blocchi)
        return [(primo + indice - 1, riga) for indice, riga in righe_del_log(b"\n".join(blocchi))]

    def apri(self, partita):
        if partita in self.seguite:
            return self.seguite[partita]
        cartella = PARTITE / partita
        meta, grafo = leggi_json(cartella / "meta.json"), leggi_json(cartella / "grafo.json")
        if meta is None or grafo is None:
            return None
        seguita = self.seguite[partita] = Seguita(cartella)
        self.pubblica(evento_partita_aperta(cartella, meta, grafo))
        return seguita

    def aggiorna_lanci(self, seguita):
        nodi = leggi_json(seguita.cartella / "nodi.json")
        for evento in eventi_dei_nodi(seguita.cartella, nodi):
            if evento["tipo"] == "nodo-lanciato" and evento["nodo"] not in seguita.lanciati:
                seguita.lanciati.add(evento["nodo"])
                self.pubblica(evento)
        return nodi

    def leggi_log(self):
        for indice, riga in self.righe_nuove():
            partita = riga.get("partita")
            if partita is None:
                self.senza_partita += 1
                continue
            if partita in self.ignorate:
                continue
            seguita = self.apri(partita)
            if seguita is None:
                self.senza_cartella += 1
                continue
            self.aggiorna_lanci(seguita)
            self.pubblica(evento_messaggio(indice, riga))

    def aggiorna_partita(self, partita):
        seguita = self.apri(partita)
        if seguita is None or seguita.chiusa:
            return
        nodi = self.aggiorna_lanci(seguita)
        chiusure = [evento for evento in eventi_dei_nodi(seguita.cartella, nodi)
                    if evento["tipo"] == "nodo-chiuso" and evento["nodo"] not in seguita.chiusi]
        meta = leggi_json(seguita.cartella / "meta.json") or {}
        if not chiusure and not meta.get("fine"):
            return
        self.leggi_log()
        for evento in sorted(chiusure, key=lambda evento: istante(evento["ts"])):
            seguita.chiusi.add(evento["nodo"])
            self.pubblica(evento)
        if meta.get("fine"):
            seguita.chiusa = True
            self.pubblica(evento_partita_chiusa(seguita.cartella, meta))

    def giro(self):
        self.leggi_log()
        if PARTITE.is_dir():
            for cartella in sorted(PARTITE.iterdir()):
                if cartella.is_dir() and cartella.name not in self.ignorate:
                    self.aggiorna_partita(cartella.name)

    async def segui(self):
        while True:
            self.giro()
            await asyncio.sleep(PASSO_DIRETTA_S)


async def inoltra(connessione, coda):
    try:
        while True:
            await connessione.send(await coda.get())
    except ConnectionClosed:
        pass


async def servi_diretta(diretta, connessione):
    coda = diretta.iscrivi()
    invio = asyncio.create_task(inoltra(connessione, coda))
    try:
        await connessione.wait_closed()
    finally:
        diretta.iscritti.discard(coda)
        invio.cancel()
# </editor-fold>


# <editor-fold defaultstate="collapsed" desc="HTTP sulla stessa porta: vista, stato del canale, origine">
def risposta_http(canale):
    def rispondi(connessione, richiesta):
        percorso = richiesta.path.split("?", 1)[0]
        if percorso == PERCORSO_EVENTI:
            origine = richiesta.headers.get("Origin")
            ammesse = {f"http://{INDIRIZZO}:{canale['porta']}", f"http://localhost:{canale['porta']}"}
            if origine is not None and origine not in ammesse:
                return connessione.respond(HTTPStatus.FORBIDDEN, f"origine non ammessa: {origine}\n")
            return None
        if percorso == "/canale.json":
            return con_tipo(connessione.respond(HTTPStatus.OK, json.dumps(canale, ensure_ascii=False)),
                            "application/json; charset=utf-8")
        if percorso in FILE_VISTA:
            nome, tipo = FILE_VISTA[percorso]
            try:
                testo = (CARTELLA_VISTA / nome).read_text(encoding="utf-8")
            except OSError:
                return connessione.respond(HTTPStatus.NOT_FOUND, f"vista assente: {nome}\n")
            return con_tipo(connessione.respond(HTTPStatus.OK, testo), tipo)
        return connessione.respond(HTTPStatus.NOT_FOUND, "non trovato\n")
    return rispondi


def con_tipo(risposta, tipo):
    del risposta.headers["Content-Type"]
    risposta.headers["Content-Type"] = tipo
    risposta.headers["Cache-Control"] = "no-store"
    return risposta
# </editor-fold>


async def accendi(argomenti):
    canale = {"modo": argomenti.modo, "porta": argomenti.porta, "velocita": None, "partite": [], "flusso": None}
    diretta, eventi_programmati, inseguimento = None, None, None
    if argomenti.modo == "vivo":
        diretta = Diretta()
        diretta.accendi()
    elif argomenti.modo == "replay":
        cartelle = [cartella_partita(argomento) for argomento in argomenti.partite]
        righe = righe_del_log(LOG_MESSAGGI.read_bytes()) if LOG_MESSAGGI.exists() else []
        gruppi = [eventi_partita(cartella, [(indice, riga) for indice, riga in righe if riga.get("partita") == cartella.name])
                  for cartella in cartelle]
        eventi_programmati = programma(gruppi, argomenti.velocita)
        canale.update(velocita=argomenti.velocita, partite=[cartella.name for cartella in cartelle])
    else:
        eventi_programmati = programma_da_flusso(argomenti.file_eventi, argomenti.velocita)
        canale.update(velocita=argomenti.velocita, flusso=str(argomenti.file_eventi))

    async def gestisci(connessione):
        if diretta is not None:
            await servi_diretta(diretta, connessione)
        else:
            await rigioca(connessione, eventi_programmati)

    async with serve(gestisci, INDIRIZZO, argomenti.porta, process_request=risposta_http(canale)) as server:
        canale["porta"] = server.sockets[0].getsockname()[1]
        dettaglio = (f"{len(eventi_programmati)} eventi" if eventi_programmati is not None
                     else f"{len(diretta.ignorate)} partite già chiuse non seguite")
        print(f"harness · {argomenti.modo} · http://{INDIRIZZO}:{canale['porta']}/ · {dettaglio}", flush=True)
        if diretta is not None:
            inseguimento = asyncio.create_task(diretta.segui())
        fermo = asyncio.get_running_loop().create_future()
        for segnale in (signal.SIGINT, signal.SIGTERM):
            asyncio.get_running_loop().add_signal_handler(segnale, lambda: fermo.done() or fermo.set_result(None))
        await fermo
        if inseguimento is not None:
            inseguimento.cancel()
    if diretta is not None:
        print(f"harness: righe del log senza partita, non emesse: {diretta.senza_partita} · "
              f"righe di partite senza cartella leggibile: {diretta.senza_cartella} · "
              f"eventi emessi: {len(diretta.emessi)}", file=sys.stderr)
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sotto = parser.add_subparsers(dest="modo", required=True)
    vivo = sotto.add_parser("vivo")
    replay = sotto.add_parser("replay")
    replay.add_argument("partite", nargs="+")
    flusso = sotto.add_parser("flusso")
    flusso.add_argument("file_eventi")
    for comando in (vivo, replay, flusso):
        comando.add_argument("--porta", type=int, default=PORTA_DEFAULT)
    for comando in (replay, flusso):
        comando.add_argument("--velocita", type=float, default=1.0)
    argomenti = parser.parse_args()
    if getattr(argomenti, "velocita", 0) < 0:
        parser.error("--velocita non può essere negativa")
    return asyncio.run(accendi(argomenti))


if __name__ == "__main__":
    sys.exit(main())
