"""Rapporto di fine partita del telefono senza fili, scritto dalla regia senza LLM.

Ogni cifra viene da una di tre fonti, e ogni sezione dice quale: la riga della partita nel
registro delle partite, le righe della partita nel log dei messaggi, i transcript dei nodi. Il
racconto di un agente non entra: il testo dei messaggi si cita solo come contenuto del log.

Le sezioni comuni a ogni gioco — messaggi per arco, token per nodo, turni umani, transcript —
vengono da `bin/misure.py`; qui restano l'esito, i numeri per salto e il messaggio all'arrivo.
"""

import json

import gioco
import misure

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
            f"**{misure.migliaia(int(registro['token']))}** token",
            "",
            "Fonte: registro, colonne `esito`, `turni_umani`, `token`.",
            "",
        ]
        discordanze = []
        if registro["esito"] != esito["esito"]:
            discordanze.append(f"esito {registro['esito']} nel registro, {esito['esito']} ricalcolato dal log")
        discordanze += misure.discordanze_osservazione(registro, osservazione)
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
            secondi = misure.secondi_fra(istante_precedente, riga["ts"]) if istante_precedente else "—"
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
                       if frase not in misure.prompt_di_lancio(sessione["transcript"])]
        testo += [f"Tetto: {tetto} parole, quello che il prompt di lancio di ogni nodo porta («{frase}»). "
                  "Fonte: transcript, primo record umano di ogni nodo."
                  + (f" ⚠️ Prompt senza quel tetto: {', '.join(senza_tetto)}." if senza_tetto else "")]
    testo.append("")

    testo += misure.sezione_messaggi_per_arco(righe_log)
    testo += misure.sezione_token_per_nodo(osservazione)
    testo += misure.sezione_turni_umani(osservazione, righe_log)

    arrivo = next((riga for riga in consegnati if riga["da"] == "D" and riga["a"] == "regia"), None)
    if arrivo:
        testo += ["## Il messaggio all'arrivo", "", "> " + arrivo["testo"], "",
                  f"Fonte: log, messaggio `{arrivo['id']}` da D alla regia.", ""]

    testo += misure.sezione_transcript(osservazione)
    return "\n".join(testo), esito


def scrivi(cartella_partita, percorso_registro, percorso_log, osservazione):
    meta = json.loads((cartella_partita / "meta.json").read_text(encoding="utf-8"))
    dati_fatti = json.loads((cartella_partita / "fatti.json").read_text(encoding="utf-8"))
    righe_log = misure.righe_log(percorso_log, cartella_partita.name)
    registro = misure.riga_registro(percorso_registro, cartella_partita.name)
    contenuto, _ = componi(cartella_partita.name, meta["regola"], dati_fatti["fatti"], righe_log, osservazione,
                           registro, *misure.percorsi_dal_progetto(percorso_registro, percorso_log))
    (cartella_partita / "rapporto.md").write_text(contenuto, encoding="utf-8")
    return cartella_partita / "rapporto.md"
