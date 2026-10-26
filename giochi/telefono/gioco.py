"""Il telefono senza fili: catena regia → A → B → C → D → regia, dieci fatti con un numero ciascuno.

Due regole. `copia`: ogni nodo inoltra il testo identico, ed è la partita di controllo del motore —
sotto 10/10 è un difetto del motore, non un esito del gioco. `riformula`: ogni nodo riscrive con
parole proprie entro il 60% delle parole del messaggio di partenza, ed è la forma del rapporto
sintetico. L'oracolo conta, salto per salto, i numeri dei fatti che arrivano intatti.
"""

import math

import fatti as libfatti

NODI = ("A", "B", "C", "D")
SUCCESSORE = {"A": "B", "B": "C", "C": "D", "D": "regia"}
SALTI = (("regia", "A"), ("A", "B"), ("B", "C"), ("C", "D"), ("D", "regia"))
QUOTA_RIFORMULAZIONE = 0.6

PROMPT = """Sei il nodo {nodo} di una partita di «telefono senza fili» del motore orchestra.

La tua vista del grafo, cioè a chi puoi scrivere:

{vista}

Il tuo solo strumento è il bus, un comando Bash:

  {bus} recv
      aspetta il prossimo messaggio per te e lo stampa: la prima riga dice chi lo manda, il testo sta dopo la riga ---
  {bus} send {successore} "<testo>"
      consegna il testo a {successore}

Cosa fai, in ordine:

1. Esegui `{bus} recv` e aspetta: il messaggio può arrivare dopo alcuni minuti, ed è previsto. Se il comando esce senza un messaggio, richiamalo.
2. Quando il messaggio arriva, {regola}
3. Consegna il testo a {successore} con `{bus} send {successore} "<testo>"`, una volta sola: il testo fra virgolette doppie, su una sola riga.
4. Fermati: non aspettare altri messaggi e non fare altro.
"""

REGOLE = {
    "copia": "copia il testo così com'è: tutto ciò che sta dopo la riga ---, senza togliere, aggiungere o cambiare niente.",
    "riformula": ("riscrivilo con parole tue, in al massimo {tetto} parole, perché chi lo riceve ne sappia il più possibile. "
                  "Non copiarlo: riformulalo."),
}


def tetto_parole(messaggio_di_partenza):
    return math.floor(len(messaggio_di_partenza.split()) * QUOTA_RIFORMULAZIONE)


def prompt_nodo(nodo, vista, bus, regola, tetto):
    return PROMPT.format(nodo=nodo, vista=vista, bus=bus, successore=SUCCESSORE[nodo],
                         regola=REGOLE[regola].format(tetto=tetto))


def oracolo(righe_log, fatti, tetto=None):
    """Numeri intatti per salto, dalle righe `consegnato` del log dei messaggi della partita."""
    consegnati = [riga for riga in righe_log if riga.get("esito") == "consegnato"]
    salti = []
    precedenti = None
    interrotta = None
    for da, a in SALTI:
        riga = next((riga for riga in consegnati if riga["da"] == da and riga["a"] == a), None)
        if riga is None:
            salti.append({"da": da, "a": a, "mancante": True})
            interrotta = interrotta or f"{da}→{a}"
            continue
        numeri = libfatti.numeri_intatti(riga["testo"], fatti)
        salto = {
            "da": da, "a": a, "id": riga["id"], "parole": riga["parole"], "intatti": len(numeri), "numeri": numeri,
            "persi": [n for n in (precedenti or []) if n not in numeri],
            "aggiunti": [n for n in numeri if precedenti is not None and n not in precedenti],
        }
        if tetto is not None and da != "regia":
            salto["entro_tetto"] = riga["parole"] <= tetto
        salti.append(salto)
        precedenti = numeri
    arrivo = salti[-1]
    intatti = 0 if arrivo.get("mancante") else arrivo["intatti"]
    return {
        "salti": salti,
        "intatti_all_arrivo": intatti,
        "totale": len(fatti),
        "esito": f"interrotta {interrotta}" if interrotta else f"{intatti}/{len(fatti)}",
    }
