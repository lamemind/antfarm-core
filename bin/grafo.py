"""Lettura del grafo di accesso: il file dati che dice chi conosce, legge, scrive, modifica, spawna chi.

Modulo condiviso da vista.py, bus e regia.py. Non valida: la forma la controlla valida-grafo.py,
che la regia lancia prima di aprire la partita. Solo libreria standard, perché il bus lo importa
a ogni messaggio.

Dice anche dove stanno le due radici del motore. La radice del codice è la cartella del motore:
`bin/`, `giochi/`, `schema/`, `vista/`, `test/`. La radice dello stato è la cartella di un progetto
che usa il motore: `partite/` e `registro/`. Le due sono distinte, perché un motore installato
serve molti progetti e lo stato di un progetto non entra mai nella storia del motore.
"""

import json
import os
import sys
from pathlib import Path

DIRITTI = ("conosce", "legge", "scrive", "modifica", "spawn")
# La variabile d'ambiente che porta la radice dello stato. Obbligatoria e senza default: un motore
# che la indovinasse scriverebbe nel posto sbagliato in silenzio.
VARIABILE_STATO = "ANTFARM_STATE_DIR"
# Il file di lock accanto a grafo.json: il bus lo prende condiviso per leggere versione e ora di
# partenza insieme, la regia esclusivo per scrivere una versione nuova.
LOCK_GRAFO = ".grafo.lock"


def carica(percorso):
    with open(percorso, encoding="utf-8") as file_grafo:
        return json.load(file_grafo)


def ids_nodi(grafo):
    return [nodo["id"] for nodo in grafo["nodi"]]


def nodo(grafo, id_nodo):
    for candidato in grafo["nodi"]:
        if candidato["id"] == id_nodo:
            return candidato
    return None


def diritti(grafo, da, a):
    """Insieme dei diritti dell'arco da → a; vuoto se l'arco non esiste."""
    for arco in grafo["archi"]:
        if arco["da"] == da and arco["a"] == a:
            return set(arco["diritti"])
    return set()


def archi_uscenti(grafo, da):
    return [arco for arco in grafo["archi"] if arco["da"] == da]


def destinatari(grafo, da, diritto):
    """I nodi verso cui `da` ha `diritto`, nell'ordine in cui il file li dichiara."""
    return [arco["a"] for arco in archi_uscenti(grafo, da) if diritto in arco["diritti"]]


def delta(prima, dopo):
    """Cosa cambia fra due versioni del grafo: nodi e archi aggiunti e tolti, voci intere, nell'ordine
    dei file, più la versione nuova. Un arco che cambia i diritti, o un nodo che cambia un campo,
    compare fra i tolti e fra gli aggiunti, così il delta non perde niente."""
    def chiave_nodo(voce):
        return json.dumps(voce, sort_keys=True, ensure_ascii=False)

    def chiave_arco(voce):
        return voce["da"], voce["a"], frozenset(voce["diritti"])

    def differenza(voci, altre, chiave):
        presenti = {chiave(voce) for voce in altre}
        return [voce for voce in voci if chiave(voce) not in presenti]

    return {
        "versione": dopo["versione"],
        "nodi_aggiunti": differenza(dopo["nodi"], prima["nodi"], chiave_nodo),
        "nodi_tolti": differenza(prima["nodi"], dopo["nodi"], chiave_nodo),
        "archi_aggiunti": differenza(dopo["archi"], prima["archi"], chiave_arco),
        "archi_tolti": differenza(prima["archi"], dopo["archi"], chiave_arco),
    }


def radice_orchestra():
    return Path(__file__).resolve().parent.parent


def radice_codice():
    """La cartella del motore, ricavata dal path di questo file: `bin/`, `giochi/`, `schema/`, `vista/`."""
    return Path(__file__).resolve().parent.parent


def radice_stato():
    """La cartella dello stato, da `ANTFARM_STATE_DIR`, in path assoluto: `partite/` e `registro/`
    stanno qui sotto e nascono al primo uso.

    Esce con errore, codice 2 e il path su stderr, se la variabile manca o è vuota, se il path non è
    una cartella esistente, o se è la cartella del motore o ci sta dentro. Una radice scritta male
    non nasce in silenzio: chi scrive lo stato crea l'albero con `mkdir(parents=True)`. E il motore
    ignora `partite/` e `registro/` in git, quindi una scrittura dentro il motore non si vedrebbe in
    `git status`."""
    valore = os.environ.get(VARIABILE_STATO, "")
    if not valore:
        esci_senza_radice(f"{VARIABILE_STATO} non è impostata: la radice dello stato la sceglie chi usa "
                          f"il motore, per esempio `{VARIABILE_STATO}=. <motore>/bin/regia.py …` dalla cartella del progetto")
    radice = Path(valore).resolve()
    if not radice.is_dir():
        esci_senza_radice(f"{VARIABILE_STATO}={valore}: {radice} non è una cartella esistente")
    codice = radice_codice()
    if radice == codice or codice in radice.parents:
        esci_senza_radice(f"{VARIABILE_STATO}={valore}: {radice} è la cartella del motore o sta dentro ({codice}); "
                          "lo stato di un progetto sta fuori dalla cartella del motore")
    return radice


def esci_senza_radice(motivo):
    print(f"radice dello stato non valida — {motivo}", file=sys.stderr)
    raise SystemExit(2)
