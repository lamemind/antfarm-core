"""Il gioco di Leavitt (1951): cinque nodi, ognuno con una carta di cinque simboli su sei, trovano
il simbolo che sta su tutte le carte scambiandosi messaggi solo lungo gli archi del grafo.

Quattro topologie sugli stessi cinque nodi A–E, un grafo ciascuna in questa cartella: ruota (C al
centro), catena, cerchio, tutti con tutti. In ogni grafo la regia scrive a ogni nodo la sua carta
e ogni nodo scrive alla regia la sua dichiarazione.

La dichiarazione è unica e definitiva: per ogni nodo conta il primo messaggio consegnato alla
regia, e i successivi l'oracolo li conta a parte. Una dichiarazione vale se il testo porta
esattamente uno dei sei simboli come parola isolata; con zero simboli o con più di uno è nulla,
e si conta come sbagliata. L'oracolo legge le righe del log dei messaggi verso la regia, mai il
racconto di un nodo: l'esito è dichiarazioni giuste su 5.
"""

from pathlib import Path

from . import carte as libcarte

NODI = libcarte.NODI
TOPOLOGIE = ("ruota", "catena", "cerchio", "completo")
CARTELLA = Path(__file__).resolve().parent

PROMPT = """Sei il nodo {nodo} di una partita del gioco di Leavitt, nel motore orchestra. Giocano cinque nodi: A, B, C, D, E.

Il gioco:

- ci sono sei simboli, codici di tre caratteri come {esempio}
- ogni nodo riceve dalla regia una carta con cinque dei sei simboli; a ogni carta ne manca uno diverso, quindi un simbolo solo sta su tutte e cinque le carte
- lo scopo è che ognuno dei cinque nodi trovi il simbolo che sta su tutte le carte
- puoi scrivere solo ai nodi che la tua vista elenca, e ricevi messaggi solo dai nodi che hanno un arco verso di te

La tua vista del grafo:

{vista}

Il tuo solo strumento è il bus, un comando Bash:

  {bus} recv
      aspetta il prossimo messaggio per te e lo stampa: la prima riga dice chi lo manda, il testo sta dopo la riga ---. Ogni chiamata restituisce un messaggio solo
  {bus} send <nodo> "<testo>"
      consegna il testo al nodo; il testo fra virgolette doppie, su una sola riga. Per scrivere a più nodi puoi concatenare gli invii in un solo comando con &&

Cosa fai:

1. Esegui `{bus} recv`: il primo messaggio è la tua carta, dalla regia. Può arrivare dopo un minuto, ed è previsto. Se il comando esce senza un messaggio, richiamalo.
2. Scambia con i nodi a cui puoi scrivere ciò che serve perché tu e gli altri troviate il simbolo comune. Fra un invio e l'altro aspetta i messaggi con `{bus} recv`.
3. Quando sai qual è il simbolo comune, dichiaralo alla regia con `{bus} send regia "<simbolo>"`. La dichiarazione è una sola e definitiva: ogni messaggio alla regia è una dichiarazione e conta solo il primo, quindi scrivi alla regia una volta sola, quando sei sicuro, e scrivi solo il simbolo.
4. La partita finisce quando tutti e cinque hanno dichiarato, e allora la regia ti ferma. Fino ad allora, anche dopo la tua dichiarazione, continua ad aspettare messaggi con `{bus} recv` e a rispondere ai nodi a cui puoi scrivere, se serve a loro. Non chiudere da solo.
"""


def grafo(topologia):
    return CARTELLA / f"grafo-{topologia}.json"


def prompt_nodo(nodo, vista, bus):
    return PROMPT.format(nodo=nodo, vista=vista, bus=bus, esempio=libcarte.ESEMPIO)


def classifica(testo, dati_carte):
    """Una dichiarazione: il simbolo, se il testo ne porta esattamente uno dei sei, ed è giusta,
    sbagliata o nulla."""
    trovati = libcarte.simboli_in(testo, dati_carte["simboli"])
    if len(trovati) != 1:
        return {"simbolo": None, "valore": "nulla", "simboli_trovati": trovati}
    simbolo = trovati[0]
    return {"simbolo": simbolo, "valore": "giusta" if simbolo == dati_carte["risposta"] else "sbagliata",
            "simboli_trovati": trovati}


def oracolo(righe_log, dati_carte):
    """Dichiarazioni giuste su 5, dalle righe `consegnato` del log dei messaggi verso la regia.

    Per ogni nodo vale la prima riga, nell'ordine del log; le successive stanno in `successive`.
    Un nodo senza riga verso la regia è in `mancanti` e conta come non giusto."""
    verso_regia = [riga for riga in righe_log
                   if riga.get("esito") == "consegnato" and riga.get("a") == "regia" and riga.get("da") in NODI]
    prime, successive = {}, []
    for riga in verso_regia:
        voce = {"nodo": riga["da"], "id": riga["id"], "ts": riga["ts"], "testo": riga["testo"],
                **classifica(riga["testo"], dati_carte)}
        if riga["da"] in prime:
            successive.append(voce)
        else:
            prime[riga["da"]] = voce
    per_nodo = [prime.get(nodo, {"nodo": nodo, "mancante": True}) for nodo in NODI]
    giuste = sum(1 for voce in prime.values() if voce["valore"] == "giusta")
    return {
        "risposta": dati_carte["risposta"],
        "per_nodo": per_nodo,
        "dichiarazioni": len(prime),
        "giuste": giuste,
        "sbagliate": sum(1 for voce in prime.values() if voce["valore"] == "sbagliata"),
        "nulle": sum(1 for voce in prime.values() if voce["valore"] == "nulla"),
        "mancanti": [nodo for nodo in NODI if nodo not in prime],
        "successive": successive,
        "esito": f"{giuste}/{len(NODI)}",
    }
