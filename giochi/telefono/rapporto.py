"""Rapporto di fine partita del telefono senza fili, scritto dalla regia senza LLM.

Ogni cifra viene da una di tre fonti, e ogni sezione dice quale: la riga della partita nel
registro delle partite, le righe della partita nel log dei messaggi, i transcript dei nodi. Il
racconto di un agente non entra: il testo dei messaggi si cita solo come contenuto del log.
"""

import json
from collections import Counter
from datetime import datetime
from pathlib import Path

import gioco

NOMI_USAGE = {
    "input_tokens": "input",
    "output_tokens": "output",
    "cache_creation_input_tokens": "cache creata",
    "cache_read_input_tokens": "cache letta",
}


def migliaia(numero):
    return f"{numero:,}".replace(",", ".")


def riga_registro(percorso_registro, id_partita):
    with open(percorso_registro, encoding="utf-8") as registro:
        intestazione = registro.readline().rstrip("\n").split("\t")
        for riga in registro:
            valori = dict(zip(intestazione, riga.rstrip("\n").split("\t")))
            if valori.get("partita") == id_partita:
                return valori
    return None


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


def secondi_fra(prima, dopo):
    return round((datetime.fromisoformat(dopo) - datetime.fromisoformat(prima)).total_seconds())


def componi(id_partita, regola, fatti, righe_log, osservazione, registro, percorso_registro, percorso_log):
    consegnati = [riga for riga in righe_log if riga["esito"] == "consegnato"]
    partenza = next((riga for riga in consegnati if riga["da"] == "regia" and riga["a"] == "A"), None)
    tetto = gioco.tetto_parole(partenza["testo"]) if regola == "riformula" and partenza else None
    esito = gioco.oracolo(righe_log, fatti, tetto)
    totale = len(fatti)
    testo = [
        f"# Rapporto — {id_partita}",
        "",
        f"Telefono senza fili, regola `{regola}`. Tre fonti, nominate in ogni sezione:",
        "",
        f"- **registro** — `{percorso_registro}`, la riga con `partita` = `{id_partita}`",
        f"- **log** — `{percorso_log}`, le righe con `partita` = `{id_partita}`",
        "- **transcript** — i file dei nodi, elencati in fondo",
        "",
        "## Esito",
        "",
    ]
    if registro is None:
        testo += ["**Riga del registro assente**: la partita non è arrivata alla chiusura della regia.", ""]
    else:
        testo += [
            f"**{registro['esito']}** numeri intatti all'arrivo · **{registro['turni_umani']}** turni umani · "
            f"**{migliaia(int(registro['token']))}** token",
            "",
            "Fonte: registro, colonne `esito`, `turni_umani`, `token`.",
            "",
        ]
        discordanze = []
        if registro["esito"] != esito["esito"]:
            discordanze.append(f"esito {registro['esito']} nel registro, {esito['esito']} ricalcolato dal log")
        if int(registro["turni_umani"]) != osservazione["turni_umani"]:
            discordanze.append(f"turni umani {registro['turni_umani']} nel registro, {osservazione['turni_umani']} dai transcript")
        if int(registro["token"]) != osservazione["token"]:
            discordanze.append(f"token {registro['token']} nel registro, {osservazione['token']} dai transcript")
        testo += [f"⚠️ **Discordanza**: {'; '.join(discordanze)}." if discordanze
                  else "Il registro concorda col ricalcolo dal log e dai transcript.", ""]

    testo += ["## Numeri per salto", ""]
    if partenza:
        numeri = ", ".join(str(fatto["numero"]) for fatto in fatti)
        testo += [f"I {totale} numeri sono quelli del messaggio regia → A: {numeri}. "
                  "Un numero è intatto se compare nel testo del salto come gruppo di cifre isolato.", ""]
    intestazione = "| Salto | Intatti | Persi in questo salto | Parole | Secondi dal salto prima |"
    allineamento = "| --- | --- | --- | --- | --- |"
    if tetto is not None:
        intestazione += " Entro il tetto |"
        allineamento += " --- |"
    testo += [intestazione, allineamento]
    istante_precedente = None
    for salto in esito["salti"]:
        riga = next((r for r in consegnati if r["da"] == salto["da"] and r["a"] == salto["a"]), None)
        if salto.get("mancante"):
            celle = [f"{salto['da']} → {salto['a']}", "non consegnato", "—", "—", "—"] + (["—"] if tetto is not None else [])
        else:
            secondi = secondi_fra(istante_precedente, riga["ts"]) if istante_precedente else "—"
            celle = [f"{salto['da']} → {salto['a']}", f"{salto['intatti']}/{totale}",
                     ", ".join(str(numero) for numero in salto["persi"]) or "—", str(salto["parole"]), str(secondi)]
            if tetto is not None:
                celle.append("—" if salto["da"] == "regia" else ("sì" if salto["entro_tetto"] else "no"))
            istante_precedente = riga["ts"]
        testo.append("| " + " | ".join(celle) + " |")
    testo += ["", "Fonte: log, righe `consegnato` per arco — campi `testo`, `parole`, `ts`. "
              "I secondi sono la differenza fra i `ts` di due righe `consegnato` consecutive, arrotondata al secondo."]
    if tetto is not None and partenza:
        frase = f"al massimo {tetto} parole"
        senza_tetto = [sessione["nodo"] for sessione in osservazione["sessioni"]
                       if frase not in prompt_di_lancio(sessione["transcript"])]
        testo += [f"Tetto: {tetto} parole, quello che il prompt di lancio di ogni nodo porta («{frase}»). "
                  "Fonte: transcript, primo record umano di ogni nodo."
                  + (f" ⚠️ Prompt senza quel tetto: {', '.join(senza_tetto)}." if senza_tetto else "")]
    testo.append("")

    testo += ["## Messaggi per arco", "", "| Arco | Consegnati | Rifiutati | Letti |", "| --- | --- | --- | --- |"]
    conteggi = Counter((riga["da"], riga["a"], riga["esito"]) for riga in righe_log)
    archi = list(dict.fromkeys((riga["da"], riga["a"]) for riga in righe_log))
    for da, a in archi:
        testo.append(f"| {da} → {a} | {conteggi[(da, a, 'consegnato')]} | {conteggi[(da, a, 'rifiutato')]} "
                     f"| {conteggi[(da, a, 'letto')]} |")
    motivi = Counter(riga["motivo"] for riga in righe_log if riga["esito"] == "rifiutato")
    testo += ["", "Rifiuti per motivo: " + (", ".join(f"`{motivo}` {numero}" for motivo, numero in motivi.items())
                                            if motivi else "nessuno") + ".",
              "", "Fonte: log, conteggio delle righe per `da`, `a`, `esito`, `motivo`.", ""]

    testo += ["## Token per nodo", "",
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

    testo += ["## Turni umani e canale nativo", "",
              "| Nodo | Record umani | Turni umani dopo il lancio | Messaggi nativi ricevuti | SendMessage | Ricevuti dal bus | Senza riga nel log |",
              "| --- | --- | --- | --- | --- | --- | --- |"]
    id_consegnati = {riga["id"] for riga in consegnati}
    for sessione in osservazione["sessioni"]:
        orfani = [id_messaggio for id_messaggio in sessione["ricevuti_dal_bus"] if id_messaggio not in id_consegnati]
        testo.append(f"| {sessione['nodo']} | {sessione['record_umani']} | {sessione['turni_umani']} "
                     f"| {sessione['messaggi_peer']} | {sessione['send_message_nativi']} "
                     f"| {len(sessione['ricevuti_dal_bus'])} | {len(orfani)} |")
    testo += ["", "Fonte: transcript — record `user` per `origin.kind` (`human`, `peer`), tool_use `SendMessage`, "
              "id dei messaggi nei `tool_result` di `bus recv`; log — righe `consegnato` con quegli id. "
              "Il primo record umano è il prompt di lancio della regia.", ""]

    arrivo = next((riga for riga in consegnati if riga["da"] == "D" and riga["a"] == "regia"), None)
    if arrivo:
        testo += ["## Il messaggio all'arrivo", "", "> " + arrivo["testo"], "",
                  f"Fonte: log, messaggio `{arrivo['id']}` da D alla regia.", ""]

    testo += ["## Transcript", ""]
    for sessione in osservazione["sessioni"]:
        testo.append(f"- {sessione['nodo']}: `{str(sessione['transcript']).replace(str(Path.home()), '~')}`")
    testo.append("")
    return "\n".join(testo), esito


def scrivi(cartella_partita, percorso_registro, percorso_log, osservazione):
    meta = json.loads((cartella_partita / "meta.json").read_text(encoding="utf-8"))
    dati_fatti = json.loads((cartella_partita / "fatti.json").read_text(encoding="utf-8"))
    with open(percorso_log, encoding="utf-8") as log:
        righe_log = [riga for riga in map(json.loads, log) if riga.get("partita") == cartella_partita.name]
    registro = riga_registro(percorso_registro, cartella_partita.name)
    radice_progetto = Path(percorso_registro).resolve().parents[2]
    contenuto, _ = componi(cartella_partita.name, meta["regola"], dati_fatti["fatti"], righe_log, osservazione,
                           registro, Path(percorso_registro).resolve().relative_to(radice_progetto),
                           Path(percorso_log).resolve().relative_to(radice_progetto))
    (cartella_partita / "rapporto.md").write_text(contenuto, encoding="utf-8")
    return cartella_partita / "rapporto.md"

