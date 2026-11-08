"""Le misure comuni ai rapporti di fine partita: dipendono da registro, log e transcript, non dal gioco.

Ogni gioco scrive il proprio rapporto con le proprie regole, il proprio oracolo e le proprie
tabelle; le sezioni qui — azioni fuori bus, messaggi per arco, messaggi per nodo, token per nodo,
turni umani e canale nativo, elenco dei transcript — non dipendono dal gioco, e ognuna porta la sua fonte.
Ogni funzione `sezione_*` restituisce le righe markdown della sezione, riga vuota finale compresa.
"""

import json
from collections import Counter
from datetime import datetime
from pathlib import Path

import partita as libpartita

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
    """Una riga per coppia (da, a) del log. La colonna Scritti — righe `scritto` verso una
    sorgente — c'è solo se la partita ne ha, così i rapporti dei giochi senza sorgenti non cambiano."""
    con_scritti = any(riga["esito"] == "scritto" for riga in righe)
    colonne = ["Consegnati", "Rifiutati", "Letti"] + (["Scritti"] if con_scritti else [])
    esiti = ["consegnato", "rifiutato", "letto"] + (["scritto"] if con_scritti else [])
    testo = ["## Messaggi per arco", "", "| Arco | " + " | ".join(colonne) + " |", "| --- |" + " --- |" * len(colonne)]
    conteggi = Counter((riga["da"], riga["a"], riga["esito"]) for riga in righe)
    archi = list(dict.fromkeys((riga["da"], riga["a"]) for riga in righe))
    for da, a in archi:
        testo.append(f"| {da} → {a} | " + " | ".join(str(conteggi[(da, a, esito)]) for esito in esiti) + " |")
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


def sezione_azioni_fuori_bus(azioni_per_nodo):
    """Le azioni di ogni nodo divise fra chiamate al bus negate e azioni fuori dal canale, con la
    riga del totale. `azioni_per_nodo` è nodo → campo `azioni` della voce di nodi.json; `None`, un
    nodo senza elenco, scrive un trattino e non entra nel totale. La classe di una voce si decide
    qui, alla composizione: nodi.json resta il dato grezzo."""
    testo = ["## Azioni fuori bus", "", "| Nodo | Chiamate al bus negate | Azioni fuori dal canale |", "| --- | --- | --- |"]
    totali = Counter()
    for nodo, azioni in azioni_per_nodo.items():
        if azioni is None:
            testo.append(f"| {nodo} | — | — |")
            continue
        classi = Counter(libpartita.classe_azione(azione) for azione in azioni)
        totali.update(classi)
        testo.append(f"| {nodo} | {classi[libpartita.CHIAMATA_BUS_NEGATA]} | {classi[libpartita.AZIONE_FUORI_CANALE]} |")
    testo.append(f"| Totale | {totali[libpartita.CHIAMATA_BUS_NEGATA]} | {totali[libpartita.AZIONE_FUORI_CANALE]} |")
    testo += ["", "Le chiamate a tool di un nodo che non sono un comando Bash che comincia col path del bus, divise in due. "
              "**Chiamata al bus negata**: l'esito è il rifiuto di `--permission-mode dontAsk` e ogni segmento del comando — "
              "spezzato su `;`, `&&`, `||`, `|` e a capo, fuori dalle virgolette — è un'assegnazione di variabile o "
              "un'invocazione del bus, col path o con la variabile che lo porta, e almeno uno lo invoca: il nodo voleva usare "
              "il canale, e la regola `Bash(<bus> *)` copre solo i comandi che cominciano col path. **Azione fuori dal "
              "canale**: ogni altra voce. Fonte: regia, `nodi.json`, campo `azioni` di ogni nodo, classificato alla "
              "composizione del rapporto.", ""]
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


def nodo_umano(righe, cartella_partita, nodo="umano"):
    """Il nodo umano passivo: i messaggi che riceve, quanti ne sono stati letti, quanti restano
    nella sua casella, quante risposte ha mandato. Nessun processo legge la sua casella, quindi in
    una partita senza turni umani i letti e le risposte sono 0 e ogni messaggio resta in casella."""
    casella = Path(cartella_partita) / "caselle" / nodo
    return {
        "messaggi": [riga for riga in righe if riga["esito"] == "consegnato" and riga["a"] == nodo],
        "letti": sum(1 for riga in righe if riga["esito"] == "letto" and riga["a"] == nodo),
        "risposte": sum(1 for riga in righe if riga["esito"] == "consegnato" and riga["da"] == nodo),
        "in_casella": len(list(casella.glob("[0-9]*.json"))) if casella.is_dir() else 0,
    }


def sezione_nodo_umano(righe, cartella_partita, nodo="umano"):
    conteggio = nodo_umano(righe, cartella_partita, nodo)
    messaggi = conteggio["messaggi"]
    testo = ["## Messaggi all'umano", ""]
    if messaggi:
        testo += ["| # | Da | ts | Testo |", "| --- | --- | --- | --- |"]
        testo += [f"| {indice} | {riga['da']} | {riga['ts']} | {riga['testo'].replace('|', '¦')} |"
                  for indice, riga in enumerate(messaggi, start=1)]
        testo.append("")
    sintesi = (f"Messaggi al nodo `{nodo}`: **{len(messaggi)}** · letti {conteggio['letti']} · ancora nella casella "
               f"{conteggio['in_casella']} · risposte dell'umano {conteggio['risposte']}.")
    if messaggi and not conteggio["risposte"]:
        sintesi += " Tutti senza risposta: il nodo umano è passivo."
    testo += [sintesi, "",
              f"Fonte: log — righe `consegnato` con `a` = `{nodo}`, righe `letto` con `a` = `{nodo}`, righe `consegnato` "
              f"con `da` = `{nodo}`; cartella della partita, file in `caselle/{nodo}/` mai prelevati.", ""]
    return testo


def sezione_transcript(osservazione):
    testo = ["## Transcript", ""]
    for sessione in osservazione["sessioni"]:
        testo.append(f"- {sessione['nodo']}: `{str(sessione['transcript']).replace(str(Path.home()), '~')}`")
    testo.append("")
    return testo
