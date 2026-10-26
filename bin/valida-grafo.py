#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["jsonschema>=4.18"]
# ///
"""Gate del grafo di accesso: esce 0 se il file è un grafo valido, 1 se non lo è.

Controlla la forma contro schema/grafo.schema.json e la coerenza che lo schema non esprime:
id dei nodi unici, estremi di ogni arco fra i nodi dichiarati, nessun arco da un nodo a sé
stesso, nessuna coppia (da, a) ripetuta — i diritti di una coppia stanno in un arco solo.

Uso:  orchestra/bin/valida-grafo.py <grafo.json>
Esce 2 su uso sbagliato o file illeggibile.
"""

import json
import sys
from pathlib import Path

import jsonschema

SCHEMA = Path(__file__).resolve().parent.parent / "schema" / "grafo.schema.json"


def errori_di_forma(grafo):
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    validatore = jsonschema.Draft202012Validator(schema)
    for errore in sorted(validatore.iter_errors(grafo), key=lambda e: list(e.absolute_path)):
        posizione = "/".join(str(parte) for parte in errore.absolute_path) or "(radice)"
        yield f"forma: {posizione}: {errore.message}"


def errori_di_coerenza(grafo):
    visti = set()
    for nodo in grafo["nodi"]:
        if nodo["id"] in visti:
            yield f"nodo ripetuto: {nodo['id']}"
        visti.add(nodo["id"])
    coppie = set()
    for indice, arco in enumerate(grafo["archi"]):
        for estremo in ("da", "a"):
            if arco[estremo] not in visti:
                yield f"archi/{indice}: '{estremo}' punta a un nodo inesistente: {arco[estremo]}"
        if arco["da"] == arco["a"]:
            yield f"archi/{indice}: arco da {arco['da']} a sé stesso"
        coppia = (arco["da"], arco["a"])
        if coppia in coppie:
            yield f"archi/{indice}: coppia ripetuta {arco['da']} → {arco['a']}"
        coppie.add(coppia)


def main():
    if len(sys.argv) != 2:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    try:
        grafo = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as errore:
        print(f"grafo illeggibile: {errore}", file=sys.stderr)
        return 2
    errori = list(errori_di_forma(grafo))
    if not errori:
        errori = list(errori_di_coerenza(grafo))
    for errore in errori:
        print(errore, file=sys.stderr)
    if errori:
        print(f"GRAFO NON VALIDO: {len(errori)} errori — {sys.argv[1]}", file=sys.stderr)
        return 1
    print(f"grafo valido: versione {grafo['versione']}, {len(grafo['nodi'])} nodi, {len(grafo['archi'])} archi")
    return 0


if __name__ == "__main__":
    sys.exit(main())
