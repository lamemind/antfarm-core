"""Rapporto di fine partita del gioco di Leavitt, scritto dalla regia senza LLM.

Ogni cifra viene da una di quattro fonti, e ogni sezione dice quale: la riga della partita nel
registro delle partite, le righe della partita nel log dei messaggi, i transcript dei nodi, e
meta.json e carte.json, scritti dalla regia. Il racconto di un agente non entra: il testo dei
messaggi si cita solo come contenuto del log.

`misura` calcola i numeri e `componi` li scrive; il confronto delle topologie chiama la stessa
`misura`, quindi i suoi numeri sono quelli dei rapporti. Le sezioni comuni a ogni gioco vengono
da `bin/misure.py`; qui restano esito, carte, dichiarazioni e centralità.
"""

import json
from pathlib import Path

import grafo as libgrafo
import misure

from . import carte as libcarte
from . import gioco


def decimale(numero):
    return f"{numero:.2f}".replace(".", ",")


def carica_json(percorso):
    return json.loads(Path(percorso).read_text(encoding="utf-8"))


def centralita(righe, grafo):
    """Messaggi fra giocatori — righe `consegnato` con mittente e destinatario fra A–E —, grado di
    ogni nodo verso gli altri giocatori, e il nodo di grado massimo che riceve di più contro la
    media degli altri quattro: nella ruota è il centro, nelle altre topologie il più carico fra i
    nodi di grado massimo."""
    fra_giocatori = [riga for riga in righe
                     if riga["esito"] == "consegnato" and riga["da"] in gioco.NODI and riga["a"] in gioco.NODI]
    per_nodo = {nodo: {
        "grado": len([destinatario for destinatario in libgrafo.destinatari(grafo, nodo, "scrive") if destinatario in gioco.NODI]),
        "inviati": sum(1 for riga in fra_giocatori if riga["da"] == nodo),
        "ricevuti": sum(1 for riga in fra_giocatori if riga["a"] == nodo),
    } for nodo in gioco.NODI}
    grado_massimo = max(voce["grado"] for voce in per_nodo.values())
    centro = max((nodo for nodo in gioco.NODI if per_nodo[nodo]["grado"] == grado_massimo),
                 key=lambda nodo: per_nodo[nodo]["ricevuti"])
    media_altri = sum(per_nodo[nodo]["ricevuti"] for nodo in gioco.NODI if nodo != centro) / (len(gioco.NODI) - 1)
    return {
        "per_nodo": per_nodo,
        "totale": len(fra_giocatori),
        "centro": centro,
        "grado_centro": grado_massimo,
        "ricevuti_centro": per_nodo[centro]["ricevuti"],
        "media_altri": round(media_altri, 2),
        "saturazione": round(per_nodo[centro]["ricevuti"] / media_altri, 2) if media_altri else None,
    }


def misura(cartella_partita, percorso_registro, percorso_log, osservazione):
    """Tutti i numeri del rapporto di una partita, dalle sue fonti."""
    cartella_partita = Path(cartella_partita)
    id_partita = cartella_partita.name
    meta = carica_json(cartella_partita / "meta.json")
    dati_carte = carica_json(cartella_partita / "carte.json")
    righe = misure.righe_log(percorso_log, id_partita)
    esito = gioco.oracolo(righe, dati_carte)
    consegnate = {riga["a"]: riga for riga in righe
                  if riga["esito"] == "consegnato" and riga["da"] == "regia" and riga["a"] in gioco.NODI}
    prima_carta = min((riga["ts"] for riga in consegnate.values()), default=None)
    dichiarazioni = []
    for voce in esito["per_nodo"]:
        voce = dict(voce)
        if not voce.get("mancante") and prima_carta:
            voce["secondi"] = misure.secondi_fra(prima_carta, voce["ts"])
        voce["successive"] = sum(1 for successiva in esito["successive"] if successiva["nodo"] == voce["nodo"])
        dichiarazioni.append(voce)
    secondi_dichiarati = [voce["secondi"] for voce in dichiarazioni if "secondi" in voce]
    primo_scambio = next((riga["ts"] for riga in righe if riga["esito"] == "consegnato"
                          and riga["da"] in gioco.NODI and riga["a"] in gioco.NODI), None)
    nodi = carica_json(cartella_partita / "nodi.json")["nodi"]
    return {
        "partita": id_partita,
        "topologia": meta["topologia"],
        "seme": meta["seme"],
        "chiusura": meta.get("chiusura"),
        "registro": misure.riga_registro(percorso_registro, id_partita),
        "esito": esito,
        "carte": {nodo: {"consegnata": libcarte.simboli_in(riga["testo"], dati_carte["simboli"]),
                         "id": riga["id"], "da_carte_json": dati_carte["carte"][nodo]}
                  for nodo, riga in consegnate.items()},
        "dati_carte": dati_carte,
        "prima_carta": prima_carta,
        "dichiarazioni": dichiarazioni,
        "secondi_quinta": max(secondi_dichiarati) if len(secondi_dichiarati) == len(gioco.NODI) else None,
        "secondi_ultima": max(secondi_dichiarati, default=None),
        "secondi_primo_scambio": misure.secondi_fra(prima_carta, primo_scambio) if prima_carta and primo_scambio else None,
        "azioni_fuori_bus": {nodo: nodi.get(nodo, {}).get("azioni_fuori_bus") for nodo in gioco.NODI},
        "centralita": centralita(righe, carica_json(cartella_partita / "grafo.json")),
        "messaggi_per_nodo": misure.messaggi_per_nodo(righe, ["regia", *gioco.NODI]),
        "consegnati": sum(1 for riga in righe if riga["esito"] == "consegnato"),
        "token": osservazione["token"],
        "token_per_nodo": {sessione["nodo"]: sessione["token"] for sessione in osservazione["sessioni"]},
        "turni_assistente": sum(sessione["turni_assistente"] for sessione in osservazione["sessioni"]),
        "turni_umani": osservazione["turni_umani"],
        "righe": righe,
    }


def componi(misurato, osservazione, percorso_registro, percorso_log):
    esito, registro = misurato["esito"], misurato["registro"]
    totale = len(gioco.NODI)
    testo = [
        f"# Rapporto — {misurato['partita']}",
        "",
        f"Gioco di Leavitt, topologia `{misurato['topologia']}`, seme {misurato['seme']}. Quattro fonti, nominate in ogni sezione:",
        "",
        f"- **registro** — `{percorso_registro}`, la riga con `partita` = `{misurato['partita']}`",
        f"- **log** — `{percorso_log}`, le righe con `partita` = `{misurato['partita']}`",
        "- **transcript** — i file dei nodi, elencati in fondo",
        "- **regia** — `meta.json` e `carte.json` nella cartella della partita",
        "",
        "## Esito",
        "",
    ]
    if registro is None:
        testo += ["**Riga del registro assente**: la partita non è arrivata alla chiusura della regia, "
                  "o è una partita a secco.", ""]
    else:
        testo += [
            f"**{registro['esito']}** dichiarazioni giuste · **{registro['turni_umani']}** turni umani · "
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
    chiusure = {"cinque-dichiarazioni": "cinque nodi hanno dichiarato", "scadenza": "scadenza della partita",
                "tetto-token": "token oltre il tetto"}
    testo += [f"Chiusura: {chiusure.get(misurato['chiusura'], misurato['chiusura'])}. Fonte: regia, `meta.json`, campo `chiusura`.", ""]

    testo += ["## Le carte", "",
              f"Simbolo comune: **{esito['risposta']}**. Simboli della partita: "
              + ", ".join(f"`{simbolo}`" for simbolo in misurato["dati_carte"]["simboli"]) + ".", "",
              "| Nodo | Carta consegnata | Simbolo che manca | Uguale a carte.json |", "| --- | --- | --- | --- |"]
    for nodo in gioco.NODI:
        carta = misurato["carte"].get(nodo)
        if carta is None:
            testo.append(f"| {nodo} | non consegnata | — | — |")
            continue
        manca = [simbolo for simbolo in misurato["dati_carte"]["simboli"] if simbolo not in carta["consegnata"]]
        uguale = sorted(carta["consegnata"]) == sorted(carta["da_carte_json"])
        testo.append(f"| {nodo} | {' '.join(carta['consegnata'])} | {', '.join(manca)} | {'sì' if uguale else 'no'} |")
    testo += ["", "Fonte: log, righe `consegnato` da regia a ogni nodo, i simboli della partita trovati nel `testo`; "
              "regia, `carte.json`, per il simbolo comune e il confronto.", ""]

    testo += ["## Dichiarazioni", "",
              "| Nodo | Simbolo | Valore | Secondi dalla prima carta | Messaggi successivi alla regia | Testo |",
              "| --- | --- | --- | --- | --- | --- |"]
    for voce in misurato["dichiarazioni"]:
        if voce.get("mancante"):
            testo.append(f"| {voce['nodo']} | — | mancante | — | 0 | — |")
        else:
            testo.append(f"| {voce['nodo']} | {voce['simbolo'] or '—'} | {voce['valore']} | {voce['secondi']} "
                         f"| {voce['successive']} | {voce['testo'].replace('|', '¦')} |")
    if misurato["secondi_quinta"] is not None:
        tempo = f"Tempo alla quinta dichiarazione: **{misurato['secondi_quinta']} s** dalla prima carta."
    else:
        tempo = (f"Quinta dichiarazione non arrivata: {esito['dichiarazioni']} dichiarazioni, l'ultima a "
                 f"{misurato['secondi_ultima'] if misurato['secondi_ultima'] is not None else '—'} s dalla prima carta.")
    testo += ["", tempo, "",
              f"Giuste {esito['giuste']} · sbagliate {esito['sbagliate']} · nulle {esito['nulle']} · "
              f"mancanti {len(esito['mancanti'])}, su {totale}.", "",
              "Fonte: log, righe `consegnato` verso la regia: per ogni nodo la prima è la dichiarazione, le altre "
              "sono successive. Valida se il `testo` porta esattamente uno dei sei simboli come parola isolata, nulla "
              "altrimenti. I secondi sono la differenza fra il `ts` della dichiarazione e quello della prima carta "
              "consegnata dalla regia, arrotondata al secondo.", ""]

    centro = misurato["centralita"]
    testo += ["## Centralità", "",
              "| Nodo | Grado | Inviati ad altri giocatori | Ricevuti da altri giocatori |", "| --- | --- | --- | --- |"]
    for nodo in gioco.NODI:
        voce = centro["per_nodo"][nodo]
        testo.append(f"| {nodo} | {voce['grado']} | {voce['inviati']} | {voce['ricevuti']} |")
    primo = misurato["secondi_primo_scambio"]
    saturazione = (f"{decimale(centro['saturazione'])}×" if centro["saturazione"] is not None
                   else "non calcolabile (media degli altri 0)")
    testo += ["", f"Messaggi fra giocatori: **{centro['totale']}**. Nodo di grado massimo più carico: **{centro['centro']}** "
              f"(grado {centro['grado_centro']}), {centro['ricevuti_centro']} messaggi ricevuti contro una media di "
              f"{decimale(centro['media_altri'])} negli altri quattro: saturazione **{saturazione}**. Primo messaggio fra giocatori: "
              f"{str(primo) + ' s' if primo is not None else 'nessuno'} dalla prima carta.", "",
              "Fonte: log, righe `consegnato` con `da` e `a` fra A–E; grado dalla `grafo.json` della partita, archi `scrive` "
              "verso altri giocatori. Fra più nodi di grado massimo vale quello che riceve di più.", ""]

    azioni = misurato["azioni_fuori_bus"]
    testo += ["## Azioni fuori bus", "", "| Nodo | Azioni fuori bus |", "| --- | --- |"]
    testo += [f"| {nodo} | {azioni[nodo] if azioni[nodo] is not None else '—'} |" for nodo in gioco.NODI]
    testo += ["", "Le chiamate a tool di un nodo che non sono un comando Bash che comincia col path del bus. Con "
              "`--permission-mode dontAsk` passano solo i comandi di sola lettura dentro la cartella del nodo; gli "
              "altri sono negati, come `B=<bus>; $B send …`, che la regola `Bash(<bus> *)` non copre. Fonte: regia, "
              "`nodi.json`, campo `azioni_fuori_bus` di ogni nodo, contato sul transcript alla chiusura.", ""]

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
