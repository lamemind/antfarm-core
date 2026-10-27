#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["jsonschema>=4.18"]
# ///
"""Gate degli eventi dell'harness: esce 0 se ogni riga del file è un evento valido, 1 se una non lo è.

Controlla ogni evento contro schema/evento.schema.json — che rimanda a schema/grafo.schema.json
per nodi, archi e grafo — e contro la coerenza fra involucro e riga che lo schema non esprime:
stessa partita, stesso esito, stesso ts nell'involucro e nella riga sorgente; per lo spawn, il
figlio fra i nodi del delta e un arco dal genitore al figlio fra gli archi. Sul flusso intero:
nessun messaggio emesso due volte, cioè nessuna coppia (partita, indice) ripetuta.

Uso:  orchestra/bin/valida-eventi.py <eventi.jsonl>     un evento JSON per riga, come lo emette l'harness
Esce 2 su uso sbagliato o file illeggibile.
"""

import json
import sys
from pathlib import Path

import jsonschema
from referencing import Registry, Resource

CARTELLA_SCHEMI = Path(__file__).resolve().parent.parent / "schema"
SCHEMA_EVENTO = CARTELLA_SCHEMI / "evento.schema.json"
SCHEMA_GRAFO = CARTELLA_SCHEMI / "grafo.schema.json"


def validatore():
    schema_evento = json.loads(SCHEMA_EVENTO.read_text(encoding="utf-8"))
    schema_grafo = json.loads(SCHEMA_GRAFO.read_text(encoding="utf-8"))
    registro = Registry().with_resources([
        (schema_evento["$id"], Resource.from_contents(schema_evento)),
        (schema_grafo["$id"], Resource.from_contents(schema_grafo)),
    ])
    return jsonschema.Draft202012Validator(schema_evento, registry=registro)


def errori_di_forma(validatore_eventi, evento):
    for errore in sorted(validatore_eventi.iter_errors(evento), key=lambda e: list(e.absolute_path)):
        posizione = "/".join(str(parte) for parte in errore.absolute_path) or "(radice)"
        yield f"forma: {posizione}: {errore.message}"


def errori_di_coerenza(evento):
    riga = evento["riga"]
    classe, tipo = evento["classe"], evento["tipo"]
    if classe == "messaggio":
        if riga["partita"] != evento["partita"]:
            yield f"partita dell'involucro {evento['partita']} ≠ partita della riga {riga['partita']}"
        if riga["esito"] != tipo:
            yield f"tipo {tipo} ≠ esito della riga {riga['esito']}"
        if riga["ts"] != evento["ts"]:
            yield f"ts dell'involucro {evento['ts']} ≠ ts della riga {riga['ts']}"
    elif tipo == "partita-aperta":
        if riga["meta"]["partita"] != evento["partita"]:
            yield f"partita dell'involucro {evento['partita']} ≠ partita di meta.json {riga['meta']['partita']}"
        if riga["meta"]["inizio"] != evento["ts"]:
            yield f"ts dell'involucro {evento['ts']} ≠ inizio di meta.json {riga['meta']['inizio']}"
    elif tipo == "partita-chiusa":
        if riga["partita"] != evento["partita"]:
            yield f"partita dell'involucro {evento['partita']} ≠ partita di meta.json {riga['partita']}"
        if riga["fine"] != evento["ts"]:
            yield f"ts dell'involucro {evento['ts']} ≠ fine di meta.json {riga['fine']}"
    elif tipo in ("nodo-lanciato", "nodo-chiuso"):
        campo = "lanciato" if tipo == "nodo-lanciato" else "chiuso"
        if riga.get(campo) != evento["ts"]:
            yield f"ts dell'involucro {evento['ts']} ≠ {campo} della voce del nodo {riga.get(campo)}"
    elif classe == "spawn":
        figlio, genitore, delta = evento["figlio"]["id"], evento["genitore"], evento["delta"]
        if figlio not in [nodo["id"] for nodo in delta["nodi"]]:
            yield f"il figlio {figlio} non è fra i nodi del delta"
        if not any(arco["da"] == genitore and arco["a"] == figlio for arco in delta["archi"]):
            yield f"nessun arco {genitore} → {figlio} fra gli archi del delta"


def main():
    if len(sys.argv) != 2:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    try:
        righe = Path(sys.argv[1]).read_text(encoding="utf-8").splitlines()
    except OSError as errore:
        print(f"file illeggibile: {errore}", file=sys.stderr)
        return 2
    validatore_eventi = validatore()
    errori, conteggio, messaggi_visti = [], {}, set()
    for numero, testo in enumerate(righe, start=1):
        if not testo.strip():
            continue
        try:
            evento = json.loads(testo)
        except json.JSONDecodeError as errore:
            errori.append(f"riga {numero}: JSON illeggibile: {errore}")
            continue
        errori_evento = list(errori_di_forma(validatore_eventi, evento))
        if not errori_evento:
            errori_evento = list(errori_di_coerenza(evento))
        if not errori_evento and evento["classe"] == "messaggio":
            chiave = (evento["partita"], evento["indice"])
            if chiave in messaggi_visti:
                errori_evento.append(f"messaggio emesso due volte: partita {chiave[0]}, indice {chiave[1]}")
            messaggi_visti.add(chiave)
        errori += [f"riga {numero}: {errore}" for errore in errori_evento]
        if not errori_evento:
            classe_tipo = f"{evento['classe']}/{evento['tipo']}"
            conteggio[classe_tipo] = conteggio.get(classe_tipo, 0) + 1
    if not errori and not conteggio:
        errori.append("nessun evento nel file: un flusso vuoto non passa il gate")
    for errore in errori:
        print(errore, file=sys.stderr)
    if errori:
        print(f"EVENTI NON VALIDI: {len(errori)} errori — {sys.argv[1]}", file=sys.stderr)
        return 1
    totale = sum(conteggio.values())
    print(f"eventi validi: {totale} · " + " · ".join(f"{chiave} {numero}" for chiave, numero in sorted(conteggio.items())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
