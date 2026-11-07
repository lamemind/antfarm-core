"""Le misure comuni ai rapporti di fine partita: dipendono da registro, log e transcript, non dal gioco.

Ogni gioco scrive il proprio rapporto con le proprie regole, il proprio oracolo e le proprie
tabelle; le sezioni qui — messaggi per arco, messaggi per nodo, token per nodo, turni umani e
canale nativo, elenco dei transcript — sono le stesse per ogni gioco, e ognuna porta la sua fonte.
Ogni funzione `sezione_*` restituisce le righe markdown della sezione, riga vuota finale compresa.
"""

import json
from collections import Counter
from datetime import datetime
from pathlib import Path

NOMI_USAGE = {
    "input_tokens": "input",
    "output_tokens": "output",
    "cache_creation_input_tokens": "cache creata",
    "cache_read_input_tokens": "cache letta",
}


def migliaia(numero):
    return f"{numero:,}".replace(",", ".")


def secondi_fra(prima, dopo):
    return round((datetime.fromisoformat(dopo) - datetime.fromisoformat(prima)).total_seconds())


def riga_registro(percorso_registro, id_partita):
    """La riga della partita nel registro delle partite, come dizionario colonna → valore; None se
    la riga o il registro mancano."""
    if not Path(percorso_registro).is_file():
        return None
    with open(percorso_registro, encoding="utf-8") as registro:
        intestazione = registro.readline().rstrip("\n").split("\t")
        for riga in registro:
            valori = dict(zip(intestazione, riga.rstrip("\n").split("\t")))
            if valori.get("partita") == id_partita:
                return valori
    return None


def righe_log(percorso_log, id_partita):
    with open(percorso_log, encoding="utf-8") as log:
        return [riga for riga in map(json.loads, log) if riga.get("partita") == id_partita]


def percorsi_dal_progetto(percorso_registro, percorso_log):
    """Registro e log come path dalla root del progetto, per citarli nel rapporto."""
    radice_progetto = Path(percorso_registro).resolve().parents[2]
    return (Path(percorso_registro).resolve().relative_to(radice_progetto),
            Path(percorso_log).resolve().relative_to(radice_progetto))


def prompt_di_lancio(percorso_transcript):
    """Il testo del primo record umano del transcript: il prompt con cui la regia ha lanciato il nodo."""
    if not percorso_transcript:
        return ""
    with open(percorso_transcript, encoding="utf-8") as transcript:
        for record in map(json.loads, transcript):
            if record.get("type") == "user" and (record.get("origin") or {}).get("kind") == "human":
                contenuto = (record.get("message") or {}).get("content")
                if isinstance(contenuto, list):
                    return "\n".join(blocco.get("text", "") for blocco in contenuto if isinstance(blocco, dict))
                return contenuto or ""
    return ""


def discordanze_osservazione(registro, osservazione):
    """Turni umani e token del registro contro il ricalcolo dai transcript: una frase per discordanza."""
    discordanze = []
    if int(registro["turni_umani"]) != osservazione["turni_umani"]:
        discordanze.append(f"turni umani {registro['turni_umani']} nel registro, {osservazione['turni_umani']} dai transcript")
    if int(registro["token"]) != osservazione["token"]:
        discordanze.append(f"token {registro['token']} nel registro, {osservazione['token']} dai transcript")
    return discordanze


def sezione_messaggi_per_arco(righe):
    testo = ["## Messaggi per arco", "", "| Arco | Consegnati | Rifiutati | Letti |", "| --- | --- | --- | --- |"]
    conteggi = Counter((riga["da"], riga["a"], riga["esito"]) for riga in righe)
    archi = list(dict.fromkeys((riga["da"], riga["a"]) for riga in righe))
    for da, a in archi:
        testo.append(f"| {da} → {a} | {conteggi[(da, a, 'consegnato')]} | {conteggi[(da, a, 'rifiutato')]} "
                     f"| {conteggi[(da, a, 'letto')]} |")
    motivi = Counter(riga["motivo"] for riga in righe if riga["esito"] == "rifiutato")
    testo += ["", "Rifiuti per motivo: " + (", ".join(f"`{motivo}` {numero}" for motivo, numero in motivi.items())
                                            if motivi else "nessuno") + ".",
              "", "Fonte: log, conteggio delle righe per `da`, `a`, `esito`, `motivo`.", ""]
    return testo


def messaggi_per_nodo(righe, nodi):
    """nodo → {inviati, ricevuti, letti, rifiutati}: consegnati con `da` = nodo, consegnati con
    `a` = nodo, letture del nodo, invii del nodo rifiutati."""
    conteggi = {nodo: {"inviati": 0, "ricevuti": 0, "letti": 0, "rifiutati": 0} for nodo in nodi}
    for riga in righe:
        if riga["esito"] == "consegnato":
            if riga["da"] in conteggi:
                conteggi[riga["da"]]["inviati"] += 1
            if riga["a"] in conteggi:
                conteggi[riga["a"]]["ricevuti"] += 1
        elif riga["esito"] == "letto" and riga["a"] in conteggi:
            conteggi[riga["a"]]["letti"] += 1
        elif riga["esito"] == "rifiutato" and riga["da"] in conteggi:
            conteggi[riga["da"]]["rifiutati"] += 1
    return conteggi


def sezione_messaggi_per_nodo(righe, nodi):
    conteggi = messaggi_per_nodo(righe, nodi)
    testo = ["## Messaggi per nodo", "", "| Nodo | Inviati | Ricevuti | Letti | Rifiutati |", "| --- | --- | --- | --- | --- |"]
    for nodo in nodi:
        voce = conteggi[nodo]
        testo.append(f"| {nodo} | {voce['inviati']} | {voce['ricevuti']} | {voce['letti']} | {voce['rifiutati']} |")
    testo += ["", "Fonte: log — inviati: righe `consegnato` con `da` = nodo; ricevuti: righe `consegnato` con "
              "`a` = nodo; letti: righe `letto` con `a` = nodo; rifiutati: righe `rifiutato` con `da` = nodo.", ""]
    return testo


def sezione_token_per_nodo(osservazione):
    testo = ["## Token per nodo", "",
             "| Nodo | " + " | ".join(NOMI_USAGE.values()) + " | totale | turni dell'assistente |",
             "| --- |" + " --- |" * (len(NOMI_USAGE) + 2)]
    for sessione in osservazione["sessioni"]:
        testo.append(f"| {sessione['nodo']} | " + " | ".join(migliaia(sessione["usage"][campo]) for campo in NOMI_USAGE)
                     + f" | {migliaia(sessione['token'])} | {sessione['turni_assistente']} |")
    testo.append("| **totale** | " + " | ".join(migliaia(osservazione["usage"][campo]) for campo in NOMI_USAGE)
                 + f" | **{migliaia(osservazione['token'])}** | "
                 + f"{sum(sessione['turni_assistente'] for sessione in osservazione['sessioni'])} |")
    testo += ["", "Fonte: transcript di ogni nodo, i quattro campi `usage` dei messaggi dell'assistente, "
              "ogni `message.id` contato una volta.", ""]
    return testo


def sezione_turni_umani(osservazione, righe):
    testo = ["## Turni umani e canale nativo", "",
             "| Nodo | Record umani | Turni umani dopo il lancio | Messaggi nativi ricevuti | SendMessage | Ricevuti dal bus | Senza riga nel log |",
             "| --- | --- | --- | --- | --- | --- | --- |"]
    id_consegnati = {riga["id"] for riga in righe if riga["esito"] == "consegnato"}
    for sessione in osservazione["sessioni"]:
        orfani = [id_messaggio for id_messaggio in sessione["ricevuti_dal_bus"] if id_messaggio not in id_consegnati]
        testo.append(f"| {sessione['nodo']} | {sessione['record_umani']} | {sessione['turni_umani']} "
                     f"| {sessione['messaggi_peer']} | {sessione['send_message_nativi']} "
                     f"| {len(sessione['ricevuti_dal_bus'])} | {len(orfani)} |")
    testo += ["", "Fonte: transcript — record `user` per `origin.kind` (`human`, `peer`), tool_use `SendMessage`, "
              "id dei messaggi nei `tool_result` di `bus recv`; log — righe `consegnato` con quegli id. "
              "Il primo record umano è il prompt di lancio della regia.", ""]
    return testo


def sezione_transcript(osservazione):
    testo = ["## Transcript", ""]
    for sessione in osservazione["sessioni"]:
        testo.append(f"- {sessione['nodo']}: `{str(sessione['transcript']).replace(str(Path.home()), '~')}`")
    testo.append("")
    return testo
