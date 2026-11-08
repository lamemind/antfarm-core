"""Rapporto di fine partita del cadavere squisito, scritto dalla regia senza LLM.

Ogni cifra viene da una di quattro fonti, e ogni sezione dice quale: la riga della partita nel
registro delle partite, le righe della partita nel log dei messaggi, i transcript dei nodi, i file
che la regia scrive nella cartella della partita — meta.json, nodi.json, grafi/, prompt-D.txt. Il
racconto di un agente non entra: il testo delle righe si cita solo come contenuto del log.

`misura` calcola i numeri e `componi` li scrive. Le sezioni comuni a ogni gioco vengono da
`bin/misure.py`; qui restano l'esito coi quattro controlli, il cambio del grafo, il testo ricomposto,
i messaggi partiti con la v1 e letti dopo il cambio, la vista del nodo nuovo.
"""

import json
from pathlib import Path

import grafo as libgrafo
import misure
import partita as libpartita

from . import gioco

CHIUSURE = {
    "arrivo": "un nodo ha mandato la sua riga alla regia",
    "scadenza": "scadenza della partita",
    "tetto-token": "token oltre il tetto",
}


def carica_json(percorso):
    return json.loads(Path(percorso).read_text(encoding="utf-8"))


def cella(testo):
    return (testo or "").replace("|", "¦").replace("\n", " ⏎ ")


def righe_del_testo(righe):
    """Le righe del testo ricomposto, nell'ordine del log: il turno 0 della regia ad A, i turni fra
    nodi, l'arrivo alla regia. Ognuna porta il turno atteso dalla posizione e quello scritto dal
    nodo; l'avviso a C non è una riga del testo."""
    consegnati = [riga for riga in righe if riga["esito"] == "consegnato"]
    filtrate = [riga for riga in consegnati
                if riga["da"] in gioco.NODI or (riga["da"] == "regia" and riga["a"] == "A")]
    return [{"riga": riga, "atteso": atteso, "scritto": gioco.turno_dichiarato(riga["testo"])}
            for atteso, riga in enumerate(filtrate)]


def senza_prefisso(testo):
    """Il testo della riga senza il `turno N:` iniziale, se c'è."""
    trovato = gioco.ESPRESSIONE_TURNO.match(testo or "")
    return (testo or "")[trovato.end():].strip() if trovato else (testo or "")


def prima_vista_di_d(osservazione):
    """L'esito della prima `bus vista` di D, dal transcript; None se D non l'ha chiamata o non è una sessione."""
    sessione = next((voce for voce in osservazione["sessioni"] if voce["nodo"] == gioco.NODO_NUOVO), None)
    if sessione is None or not sessione["transcript"]:
        return None
    for nome, comando, esito, _ in libpartita.chiamate_bash(sessione["transcript"]):
        if nome == "Bash" and comando.strip() == f"{libpartita.BUS} vista":
            return esito
    return None


def misura(cartella_partita, percorso_registro, percorso_log, osservazione):
    """Tutti i numeri del rapporto di una partita, dalle sue fonti."""
    cartella_partita = Path(cartella_partita)
    id_partita = cartella_partita.name
    meta = carica_json(cartella_partita / "meta.json")
    nodi = carica_json(cartella_partita / "nodi.json")["nodi"]
    righe = misure.righe_log(percorso_log, id_partita)
    oracolo = gioco.oracolo_della_partita(cartella_partita, righe)
    v1, v2 = cartella_partita / "grafi" / "v1.json", cartella_partita / "grafi" / "v2.json"
    delta = libgrafo.delta(carica_json(v1)["grafo"], carica_json(v2)["grafo"]) if v1.is_file() and v2.is_file() else None
    file_prompt = cartella_partita / f"prompt-{gioco.NODO_NUOVO}.txt"
    avvisi = [riga for riga in righe if riga["esito"] == "consegnato" and riga["da"] == "regia" and riga["a"] == "C"]
    return {
        "partita": id_partita,
        "chiusura": meta.get("chiusura"),
        "registro": misure.riga_registro(percorso_registro, id_partita),
        "oracolo": oracolo,
        "delta": delta,
        "avvisi": avvisi,
        "letture_avviso": [riga for riga in righe if riga["esito"] == "letto" and riga["id"] in {avviso["id"] for avviso in avvisi}],
        "righe_del_testo": righe_del_testo(righe),
        "rifiuti": [riga for riga in righe if riga["esito"] == "rifiutato"],
        "letti_dopo": gioco.letti_dopo_il_cambio(righe, oracolo["in_vigore_v2"]),
        "testo_prompt_d": file_prompt.read_text(encoding="utf-8") if file_prompt.is_file() else None,
        "prima_vista_di_d": prima_vista_di_d(osservazione),
        "azioni": {nodo: nodi[nodo].get("azioni") for nodo in gioco.NODI if nodo in nodi},
        "righe": righe,
    }


def componi(misurato, osservazione, percorso_registro, percorso_log):
    registro, oracolo = misurato["registro"], misurato["oracolo"]
    testo = [
        f"# Rapporto — {misurato['partita']}",
        "",
        "Cadavere squisito: ognuno scrive una riga vedendo solo l'ultima del precedente, e a partita aperta entra un nodo "
        "nuovo. Il testo si butta: si guarda se ogni messaggio porta la versione con cui è partito e se il nuovo riceve il "
        "grafo com'è adesso. Quattro fonti, nominate in ogni sezione:",
        "",
        f"- **registro** — `{percorso_registro}`, la riga con `partita` = `{misurato['partita']}`",
        f"- **log** — `{percorso_log}`, le righe con `partita` = `{misurato['partita']}`",
        "- **transcript** — i file dei nodi, elencati in fondo",
        "- **regia** — `meta.json`, `nodi.json`, `grafi/` e `prompt-D.txt` nella cartella della partita",
        "",
        "## Esito",
        "",
    ]
    if registro is None:
        testo += ["**Riga del registro assente**: la partita non è arrivata alla chiusura della regia, "
                  "o è una partita a secco.", ""]
    else:
        testo += [
            f"**{registro['esito']}** controlli superati · **{registro['turni_umani']}** turni umani · "
            f"**{misure.migliaia(int(registro['token']))}** token",
            "",
            "Fonte: registro, colonne `esito`, `turni_umani`, `token`.",
            "",
        ]
        discordanze = []
        if registro["esito"] != oracolo["esito"]:
            discordanze.append(f"esito {registro['esito']} nel registro, {oracolo['esito']} dall'oracolo rieseguito")
        discordanze += misure.discordanze_osservazione(registro, osservazione)
        testo += [f"⚠️ **Discordanza**: {'; '.join(discordanze)}." if discordanze
                  else "Il registro concorda con l'oracolo rieseguito e col ricalcolo dai transcript.", ""]
    testo += [f"Chiusura: {CHIUSURE.get(misurato['chiusura'], misurato['chiusura'])}. Fonte: regia, `meta.json`, campo `chiusura`.", "",
              "| # | Controllo | Esito | Evidenza |", "| --- | --- | --- | --- |"]
    testo += [f"| {voce['controllo']} | {voce['nome']} | {'superato' if voce['superato'] else '**fallito**'} | {cella(voce['evidenza'])} |"
              for voce in oracolo["controlli"]]
    testo += ["", "Fonte: oracolo rieseguito qui su log, `grafi/v2.json` e `prompt-D.txt`.", ""]

    testo += ["## Il cambio del grafo", ""]
    delta = misurato["delta"]
    if delta is None:
        testo += ["La versione 2 non è entrata in vigore: la partita non è arrivata al sesto messaggio fra nodi.", ""]
    else:
        nodi = ", ".join(nodo["id"] for nodo in delta["nodi_aggiunti"]) or "nessuno"
        aggiunti = ", ".join(f"{arco['da']} → {arco['a']}" for arco in delta["archi_aggiunti"]) or "nessuno"
        tolti = ", ".join(f"{arco['da']} → {arco['a']}" for arco in delta["archi_tolti"]) or "nessuno"
        testo += [f"Versione 2 in vigore da `{oracolo['in_vigore_v2']}`. Nodi aggiunti: {nodi}. Archi aggiunti: {aggiunti}. Archi tolti: {tolti}.",
                  "Fonte: regia, `grafi/v1.json` e `grafi/v2.json`.", ""]
        for avviso in misurato["avvisi"]:
            lettura = next((riga for riga in misurato["letture_avviso"] if riga["id"] == avviso["id"]), None)
            testo.append(f"Avviso a C: partito `{avviso['ts']}` con la versione {avviso['versione']}, "
                         + (f"letto `{lettura['ts']}`." if lettura else "mai letto."))
        testo += ["", "Fonte: log, righe `consegnato` e `letto` da regia a C.", ""]
    rifiuti_c_a = [riga for riga in misurato["rifiuti"] if riga["da"] == "C" and riga["a"] == "A"]
    volte = "volta" if len(rifiuti_c_a) == 1 else "volte"
    testo += [f"**C ha scritto ad A dopo il cambio: {len(rifiuti_c_a)} {volte}** (righe rifiutate C → A)."
              + (" ⚠️ La misura «modifica del grafo a partita in corso» di T41 le conterebbe come archi chiesti da un agente." if rifiuti_c_a else ""),
              "", f"Rifiuti totali della partita: {len(misurato['rifiuti'])}. Fonte: log, righe `rifiutato`.", ""]

    voci = misurato["righe_del_testo"]
    sbagliati = [voce for voce in voci if voce["scritto"] != voce["atteso"]]
    testo += ["## Il testo ricomposto", "",
              f"Una riga per turno, nell'ordine del log. La colonna Posizione è il turno che la riga occupa nel log, Turno scritto quello "
              f"che il nodo ha messo nel testo: **{len(sbagliati)}** non coincidono.", "",
              "| Posizione | Turno scritto | Autore → destinatario | Versione | Riga |", "| --- | --- | --- | --- | --- |"]
    for voce in voci:
        riga = voce["riga"]
        scritto = "—" if voce["scritto"] is None else str(voce["scritto"])
        if voce["scritto"] != voce["atteso"]:
            scritto += " ⚠️"
        testo.append(f"| {voce['atteso']} | {scritto} | {riga['da']} → {riga['a']} | {riga['versione']} | {cella(senza_prefisso(riga['testo']))} |")
    testo += ["", "Fonte: log, righe `consegnato` da un nodo del gioco o dalla regia ad A: campi `da`, `a`, `versione`, `testo`.", ""]

    letti = misurato["letti_dopo"]
    testo += ["## Messaggi partiti con la v1 e letti dopo il cambio", ""]
    if letti:
        testo += ["| Da → a | Partito | Letto | Testo |", "| --- | --- | --- | --- |"]
        testo += [f"| {riga['da']} → {riga['a']} | {riga['ts']} | {riga['letto']} | {cella(riga['testo'])} |" for riga in letti]
    else:
        testo.append("Nessuno.")
    testo += ["", "Il bus li ha consegnati con la v1 ed è corretto: la versione dice con quale grafo un messaggio è partito. "
              "Fonte: log, righe `consegnato` con versione 1 la cui riga `letto` ha un `ts` successivo all'entrata in vigore della v2.", ""]

    testo += ["## La vista del nodo nuovo", ""]
    prompt_d = misurato["testo_prompt_d"]
    if prompt_d is None:
        testo.append("`prompt-D.txt` assente: D non è stato lanciato.")
    else:
        estratto = [riga for riga in prompt_d.splitlines() if riga.startswith("# Vista di D") or riga.startswith("- D → ")]
        testo += ["La vista nel prompt di lancio di D:", "", *[f"    {riga}" for riga in estratto]]
    prima = misurato["prima_vista_di_d"]
    testo += ["", (f"Prima `bus vista` di D: `{cella(prima)}`." if prima
                   else "D non ha chiamato `bus vista`, o non è una sessione: la vista gli è arrivata col prompt, che è ciò che l'oracolo misura."),
              "", "Fonte: regia, `prompt-D.txt`; transcript di D per la `bus vista`, fuori dall'oracolo.", ""]

    testo += misure.sezione_azioni_fuori_bus(misurato["azioni"])
    testo += misure.sezione_messaggi_per_nodo(misurato["righe"], ["regia", *gioco.NODI])
    testo += misure.sezione_messaggi_per_arco(misurato["righe"])
    testo += misure.sezione_token_per_nodo(osservazione)
    testo += misure.sezione_turni_umani(osservazione, misurato["righe"])
    testo += misure.sezione_transcript(osservazione)
    return "\n".join(testo)


def scrivi(cartella_partita, percorso_registro, percorso_log, osservazione):
    misurato = misura(cartella_partita, percorso_registro, percorso_log, osservazione)
    contenuto = componi(misurato, osservazione, *misure.percorsi_dal_progetto(percorso_registro, percorso_log))
    (Path(cartella_partita) / "rapporto.md").write_text(contenuto, encoding="utf-8")
    return Path(cartella_partita) / "rapporto.md"
