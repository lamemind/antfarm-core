"""Leavitt's game (1951): five nodes, each with a card of five symbols out of six, find the
symbol that is on every card by exchanging messages only along the edges of the graph.

Four topologies on the same five nodes A–E, one graph each in this directory: wheel (C at the
center), chain, circle, all-to-all. In every graph the director writes each node its card
and each node writes the director its declaration.

The declaration is single and final: for each node the first message delivered to the
director counts, and the oracle counts the later ones apart. A declaration is valid if the text
carries exactly one of the six symbols as an isolated word; with zero symbols or more than one
it is void, and it counts as wrong. The oracle reads the rows of the message log towards the
director, never a node's own account: the outcome is correct declarations out of 5.
"""

from pathlib import Path

from . import cards as libcards

NODES = libcards.NODES
TOPOLOGIES = ("wheel", "chain", "circle", "complete")
DIRECTORY = Path(__file__).resolve().parent

PROMPT = """Sei il nodo {node} di una partita del gioco di Leavitt, nel motore antfarm. Giocano cinque nodi: A, B, C, D, E.

Il gioco:

- ci sono sei simboli, codici di tre caratteri come {example}
- ogni nodo riceve da director una carta con cinque dei sei simboli; a ogni carta ne manca uno diverso, quindi un simbolo solo sta su tutte e cinque le carte
- lo scopo è che ognuno dei cinque nodi trovi il simbolo che sta su tutte le carte
- puoi scrivere solo ai nodi che la tua vista elenca, e ricevi messaggi solo dai nodi che hanno un arco verso di te

La tua vista del grafo:

{view}

Il tuo solo strumento è il bus, un comando Bash:

  {bus} recv
      aspetta il prossimo messaggio per te e lo stampa: la prima riga dice chi lo manda, il testo sta dopo la riga ---. Ogni chiamata restituisce un messaggio solo
  {bus} send <nodo> "<testo>"
      consegna il testo al nodo; il testo fra virgolette doppie, su una sola riga. Per scrivere a più nodi puoi concatenare gli invii in un solo comando con &&

Scrivi testi di al massimo {cap} caratteri: un invio troppo lungo viene negato. Se un invio viene negato, accorcia il testo e rimandalo.

Cosa fai:

1. Esegui `{bus} recv`: il primo messaggio è la tua carta, da director. Può arrivare dopo un minuto, ed è previsto. Se il comando esce senza un messaggio, richiamalo.
2. Scambia con i nodi a cui puoi scrivere ciò che serve perché tu e gli altri troviate il simbolo comune. Fra un invio e l'altro aspetta i messaggi con `{bus} recv`.
3. Quando sai qual è il simbolo comune, dichiaralo a director con `{bus} send director "<simbolo>"`. La dichiarazione è una sola e definitiva: ogni messaggio a director è una dichiarazione e conta solo il primo, quindi scrivi a director una volta sola, quando sei sicuro, e scrivi solo il simbolo.
4. La partita finisce quando tutti e cinque hanno dichiarato, e allora director ti ferma. Fino ad allora, anche dopo la tua dichiarazione, continua ad aspettare messaggi con `{bus} recv` e a rispondere ai nodi a cui puoi scrivere, se serve a loro. Non chiudere da solo.
"""


def graph(topology):
    return DIRECTORY / f"graph-{topology}.json"


def node_prompt(node, view, bus, text_cap):
    """`text_cap` is the characters of a send (match.SENT_TEXT_CAP), above which `dontAsk` denies
    the command."""
    return PROMPT.format(node=node, view=view, bus=bus, example=libcards.EXAMPLE, cap=text_cap)


def classify(text, card_data):
    """A declaration: the symbol, if the text carries exactly one of the six, and whether it is
    correct, wrong or void."""
    found = libcards.symbols_in(text, card_data["symbols"])
    if len(found) != 1:
        return {"symbol": None, "value": "void", "symbols_found": found}
    symbol = found[0]
    return {"symbol": symbol, "value": "correct" if symbol == card_data["answer"] else "wrong",
            "symbols_found": found}


def oracle(log_rows, card_data):
    """Correct declarations out of 5, from the `delivered` rows of the message log towards the director.

    For each node the first row counts, in log order; the later ones go in `subsequent`.
    A node without a row towards the director is in `missing` and counts as not correct."""
    to_director = [row for row in log_rows
                   if row.get("outcome") == "delivered" and row.get("to") == "director" and row.get("from") in NODES]
    firsts, subsequent = {}, []
    for row in to_director:
        entry = {"node": row["from"], "id": row["id"], "ts": row["ts"], "text": row["text"],
                **classify(row["text"], card_data)}
        if row["from"] in firsts:
            subsequent.append(entry)
        else:
            firsts[row["from"]] = entry
    per_node = [firsts.get(node, {"node": node, "missing": True}) for node in NODES]
    correct = sum(1 for entry in firsts.values() if entry["value"] == "correct")
    return {
        "answer": card_data["answer"],
        "per_node": per_node,
        "declarations": len(firsts),
        "correct": correct,
        "wrong": sum(1 for entry in firsts.values() if entry["value"] == "wrong"),
        "void": sum(1 for entry in firsts.values() if entry["value"] == "void"),
        "missing": [node for node in NODES if node not in firsts],
        "subsequent": subsequent,
        "outcome": f"{correct}/{len(NODES)}",
    }
