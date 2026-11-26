"""Telephone: chain director → A → B → C → D → director, ten facts with one number each.

Two rules. `copy`: each node forwards the identical text, and it is the engine's control match —
below 10/10 it is an engine defect, not a game outcome. `rephrase`: each node rewrites the text in
its own words within 60% of the words of the start message, and it has the shape of a summary
report. The oracle counts, hop by hop, the fact numbers that arrive intact.
"""

import math

import facts as libfacts

NODES = ("A", "B", "C", "D")
SUCCESSOR = {"A": "B", "B": "C", "C": "D", "D": "director"}
HOPS = (("director", "A"), ("A", "B"), ("B", "C"), ("C", "D"), ("D", "director"))
REPHRASE_QUOTA = 0.6

PROMPT = """Sei il nodo {node} di una partita di «telefono senza fili» del motore antfarm.

La tua vista del grafo, cioè a chi puoi scrivere:

{view}

Il tuo solo strumento è il bus, un comando Bash:

  {bus} recv
      aspetta il prossimo messaggio per te e lo stampa: la prima riga dice chi lo manda, il testo sta dopo la riga ---
  {bus} send {successor} "<testo>"
      consegna il testo a {successor}

Scrivi testi di al massimo {cap} caratteri: un invio troppo lungo viene negato. Se un invio viene negato, accorcia il testo e rimandalo.

Cosa fai, in ordine:

1. Esegui `{bus} recv` e aspetta: il messaggio può arrivare dopo alcuni minuti, ed è previsto. Se il comando esce senza un messaggio, richiamalo.
2. Quando il messaggio arriva, {rule}
3. Consegna il testo a {successor} con `{bus} send {successor} "<testo>"`, una volta sola: il testo fra virgolette doppie, su una sola riga.
4. Fermati: non aspettare altri messaggi e non fare altro.
"""

RULES = {
    "copy": "copia il testo così com'è: tutto ciò che sta dopo la riga ---, senza togliere, aggiungere o cambiare niente.",
    "rephrase": ("riscrivilo con parole tue, in al massimo {cap} parole, perché chi lo riceve ne sappia il più possibile. "
                  "Non copiarlo: riformulalo."),
}


def word_cap(start_message):
    return math.floor(len(start_message.split()) * REPHRASE_QUOTA)


def node_prompt(node, view, bus, rule, cap, text_cap):
    """`cap` is the word cap of the rephrase rule; `text_cap` the characters of a send
    (match.SENT_TEXT_CAP), above which `dontAsk` denies the command."""
    return PROMPT.format(node=node, view=view, bus=bus, successor=SUCCESSOR[node],
                         rule=RULES[rule].format(cap=cap), cap=text_cap)


def oracle(log_rows, facts, cap=None):
    """Intact numbers per hop, from the `delivered` rows of the match's message log."""
    delivered = [row for row in log_rows if row.get("outcome") == "delivered"]
    hops = []
    previous_numbers = None
    interrupted = None
    for from_node, to_node in HOPS:
        row = next((row for row in delivered if row["from"] == from_node and row["to"] == to_node), None)
        if row is None:
            hops.append({"from": from_node, "to": to_node, "missing": True})
            interrupted = interrupted or f"{from_node}→{to_node}"
            continue
        numbers = libfacts.intact_numbers(row["text"], facts)
        hop = {
            "from": from_node, "to": to_node, "id": row["id"], "words": row["words"], "intact": len(numbers), "numbers": numbers,
            "lost": [n for n in (previous_numbers or []) if n not in numbers],
            "added": [n for n in numbers if previous_numbers is not None and n not in previous_numbers],
        }
        if cap is not None and from_node != "director":
            hop["within_cap"] = row["words"] <= cap
        hops.append(hop)
        previous_numbers = numbers
    arrival = hops[-1]
    intact = 0 if arrival.get("missing") else arrival["intact"]
    return {
        "hops": hops,
        "intact_at_arrival": intact,
        "total": len(facts),
        "outcome": f"interrupted {interrupted}" if interrupted else f"{intact}/{len(facts)}",
    }
