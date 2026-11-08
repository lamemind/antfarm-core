"""Lettura del grafo di accesso: il file dati che dice chi conosce, legge, scrive, modifica, spawna chi.

Modulo condiviso da vista.py, bus e regia.py. Non valida: la forma la controlla valida-grafo.py,
che la regia lancia prima di aprire la partita. Solo libreria standard, perché il bus lo importa
a ogni messaggio.
"""

import json
from pathlib import Path

DIRITTI = ("conosce", "legge", "scrive", "modifica", "spawn")
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


def radice_orchestra():
    return Path(__file__).resolve().parent.parent
