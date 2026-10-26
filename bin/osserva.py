#!/usr/bin/env python3
"""Osservabilità di una partita: i tre controlli su ogni sessione lanciata dalla regia, turni umani, token.

I tre controlli, per ogni nodo LLM di nodi.json:

1. transcript — il file della sessione esiste e porta almeno un turno dell'assistente
2. genitore — nodi.json, scritto dalla regia al lancio, porta genitore (la regia della partita,
   col suo pid) e compito, e il transcript porta lo stesso sessionId: il legame non viene da una
   dichiarazione dell'agente
3. token — ogni messaggio dell'assistente porta i quattro campi `usage` interi, quindi la somma
   è calcolabile

Turni umani: record `user` con `origin.kind` = `human` dopo il primo, che è il prompt di lancio.
Token: somma di input_tokens, output_tokens, cache_creation_input_tokens, cache_read_input_tokens
per messaggio dell'assistente, contando ogni `message.id` una volta sola — Claude Code scrive un
record per blocco (thinking, testo, tool_use) e ognuno ripete l'usage del messaggio.
Accanto ai controlli: messaggi nativi ricevuti (`origin.kind` = `peer`) e chiamate a SendMessage.

Uso:  orchestra/bin/osserva.py <cartella della partita> [--json]
Esce 1 se una sessione fallisce un controllo, 2 su uso sbagliato.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import grafo as libgrafo  # noqa: E402
import partita as libpartita  # noqa: E402

CAMPI_USAGE = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
REGISTRO_PARTITE = libgrafo.radice_orchestra() / "registro" / "partite.tsv"
COLONNE_REGISTRO = ("partita", "gioco", "esito", "turni_umani", "token", "cwd")


def leggi_records(percorso):
    with open(percorso, encoding="utf-8") as transcript:
        return [json.loads(riga) for riga in transcript if riga.strip()]


def misura_sessione(id_nodo, dati_nodo, pid_regia):
    percorso = libpartita.transcript(dati_nodo["sessionId"])
    misura = {"nodo": id_nodo, "sessionId": dati_nodo["sessionId"], "transcript": str(percorso) if percorso else None}
    records = leggi_records(percorso) if percorso else []

    messaggi = {}
    for record in records:
        if record.get("type") == "assistant" and (record.get("message") or {}).get("id"):
            messaggi[record["message"]["id"]] = record["message"]
    misura["turni_assistente"] = len(messaggi)

    genitore = dati_nodo.get("genitore") or {}
    id_sessione_nei_record = {record["sessionId"] for record in records if record.get("sessionId")}
    misura["genitore"] = genitore
    misura["compito"] = dati_nodo.get("compito")

    usage_validi = all(
        all(isinstance((messaggio.get("usage") or {}).get(campo), int) for campo in CAMPI_USAGE)
        for messaggio in messaggi.values()
    )
    misura["usage"] = {campo: sum((messaggio.get("usage") or {}).get(campo) or 0 for messaggio in messaggi.values())
                       for campo in CAMPI_USAGE}
    misura["token"] = sum(misura["usage"].values())

    utenti = [record for record in records if record.get("type") == "user"]
    umani = [record for record in utenti if (record.get("origin") or {}).get("kind") == "human"]
    misura["turni_umani"] = max(0, len(umani) - 1)
    misura["record_umani"] = len(umani)
    misura["messaggi_peer"] = sum(1 for record in utenti if (record.get("origin") or {}).get("kind") == "peer")
    usi = [blocco for record in records if record.get("type") == "assistant"
           for blocco in (record.get("message") or {}).get("content") or [] if blocco.get("type") == "tool_use"]
    misura["tool_usati"] = sorted({blocco["name"] for blocco in usi})
    misura["send_message_nativi"] = sum(1 for blocco in usi if blocco["name"] == "SendMessage")
    misura["modelli"] = sorted({messaggio.get("model") for messaggio in messaggi.values() if messaggio.get("model")})

    misura["controlli"] = {
        "transcript": percorso is not None and len(messaggi) >= 1,
        "genitore": (genitore.get("nodo") == "regia" and genitore.get("pid") == pid_regia
                     and bool(dati_nodo.get("compito")) and id_sessione_nei_record == {dati_nodo["sessionId"]}),
        "token": percorso is not None and len(messaggi) >= 1 and usage_validi,
    }
    return misura


def osserva(cartella_partita):
    partita = libpartita.Partita(cartella_partita)
    nodi = partita.nodi()["nodi"]
    pid_regia = nodi.get("regia", {}).get("pid")
    sessioni = [misura_sessione(id_nodo, dati, pid_regia) for id_nodo, dati in nodi.items() if dati.get("tipo") == "llm"]
    return {
        "partita": partita.id,
        "sessioni": sessioni,
        "turni_umani": sum(sessione["turni_umani"] for sessione in sessioni),
        "token": sum(sessione["token"] for sessione in sessioni),
        "usage": {campo: sum(sessione["usage"][campo] for sessione in sessioni) for campo in CAMPI_USAGE},
        "messaggi_peer": sum(sessione["messaggi_peer"] for sessione in sessioni),
        "send_message_nativi": sum(sessione["send_message_nativi"] for sessione in sessioni),
        "controlli_ok": bool(sessioni) and all(all(sessione["controlli"].values()) for sessione in sessioni),
    }


def registra_partita(riga):
    """Appende una riga al registro delle partite, con l'intestazione se il file non c'è."""
    REGISTRO_PARTITE.parent.mkdir(parents=True, exist_ok=True)
    nuovo = not REGISTRO_PARTITE.exists()
    with open(REGISTRO_PARTITE, "a", encoding="utf-8") as registro:
        if nuovo:
            registro.write("\t".join(COLONNE_REGISTRO) + "\n")
        registro.write("\t".join(str(riga[colonna]) for colonna in COLONNE_REGISTRO) + "\n")


def come_testo(osservazione):
    righe = [f"partita {osservazione['partita']}"]
    for sessione in osservazione["sessioni"]:
        controlli = " · ".join(f"{nome} {'ok' if ok else 'FAIL'}" for nome, ok in sessione["controlli"].items())
        righe.append(f"  {sessione['nodo']}: {controlli} · turni umani {sessione['turni_umani']}"
                     f" · token {sessione['token']} · peer {sessione['messaggi_peer']}"
                     f" · SendMessage {sessione['send_message_nativi']} · tool {','.join(sessione['tool_usati'])}")
    righe.append(f"  totale: turni umani {osservazione['turni_umani']} · token {osservazione['token']}"
                 f" · controlli {'ok' if osservazione['controlli_ok'] else 'FALLITI'}")
    return "\n".join(righe)


def main():
    argomenti = [argomento for argomento in sys.argv[1:] if argomento != "--json"]
    if len(argomenti) != 1 or not (Path(argomenti[0]) / "nodi.json").is_file():
        print(__doc__.strip(), file=sys.stderr)
        return 2
    osservazione = osserva(Path(argomenti[0]))
    print(json.dumps(osservazione, ensure_ascii=False, indent=2) if "--json" in sys.argv else come_testo(osservazione))
    return 0 if osservazione["controlli_ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
