#!/usr/bin/env python3
"""Vista per agente: dal grafo di accesso il testo che un nodo riceve, e nient'altro.

Porta la versione del grafo, gli archi uscenti del nodo coi loro diritti e, per ogni diritto,
i nodi verso cui il nodo lo ha. Una sorgente — un file, un nodo che non parla — verso cui il nodo
ha `scrive` sta su una riga sua e non fra i nodi a cui scrivere, perché non ha una casella: la si
scrive con `bus scrivi`, non con `bus send`. Gli archi entranti non ci sono: sapere chi ti scrive
non è un diritto, e il grafo intero non arriva a nessun agente.

Uso:  orchestra/bin/vista.py <grafo.json> <nodo> [--json]
Esce 1 se il nodo non è nel grafo, 2 su uso sbagliato.
"""

import json
import sys

import grafo as libgrafo


def vista(grafo, id_nodo):
    archi = libgrafo.archi_uscenti(grafo, id_nodo)
    return {
        "nodo": id_nodo,
        "versione": grafo["versione"],
        "archi": [{"a": arco["a"], "diritti": arco["diritti"]} for arco in archi],
        "diritti": {diritto: libgrafo.destinatari(grafo, id_nodo, diritto) for diritto in libgrafo.DIRITTI},
        "sorgenti": [arco["a"] for arco in archi if (libgrafo.nodo(grafo, arco["a"]) or {}).get("tipo") == "sorgente"],
    }


def come_testo(dati):
    righe = [f"# Vista di {dati['nodo']} · grafo versione {dati['versione']}", "", "Archi uscenti:"]
    if dati["archi"]:
        righe += [f"- {dati['nodo']} → {arco['a']}: {', '.join(arco['diritti'])}" for arco in dati["archi"]]
    else:
        righe.append("- (nessuno)")
    righe.append("")
    etichette = {
        "conosce": "Conosci",
        "legge": "Puoi leggere la traccia di",
        "scrive": "Puoi scrivere a",
        "modifica": "Puoi modificare gli archi intorno a",
        "spawn": "Puoi spawnare da",
    }
    sorgenti = dati.get("sorgenti", [])
    for diritto in libgrafo.DIRITTI:
        nodi = dati["diritti"][diritto]
        if diritto == "scrive":
            nodi = [nodo for nodo in nodi if nodo not in sorgenti]
        righe.append(f"{etichette[diritto]}: {', '.join(nodi) if nodi else '(nessuno)'}")
    scrivibili = [sorgente for sorgente in sorgenti if sorgente in dati["diritti"]["scrive"]]
    if scrivibili:
        righe.append(f"Puoi scrivere i file (sorgenti, con bus scrivi): {', '.join(scrivibili)}")
    return "\n".join(righe)


def main():
    argomenti = [argomento for argomento in sys.argv[1:] if argomento != "--json"]
    if len(argomenti) != 2:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    percorso, id_nodo = argomenti
    grafo = libgrafo.carica(percorso)
    if libgrafo.nodo(grafo, id_nodo) is None:
        print(f"nodo assente dal grafo: {id_nodo}", file=sys.stderr)
        return 1
    dati = vista(grafo, id_nodo)
    print(json.dumps(dati, ensure_ascii=False, indent=2) if "--json" in sys.argv else come_testo(dati))
    return 0


if __name__ == "__main__":
    sys.exit(main())
