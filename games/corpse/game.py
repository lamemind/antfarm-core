"""The surrealists' exquisite corpse: each player writes a line seeing only the last line of the previous one.

The text is thrown away. The game tests the graph that changes while the match is open: after the
sixth message between nodes the director writes version 2 of the graph, D enters between C and A
and the edge C → A disappears.

A, B, C pass the line around in a circle. The director sends turn 0 to A. Whoever receives turn N
writes a single line and sends it to the successor as turn N+1; whoever receives the last turn, 16,
sends it to the director and the match closes. The view tells the successor: when the graph changes
the director notifies C, which has just written and is waiting in `bus recv`, and the notice says
to reread the view without naming the successor. The first arrival at the director from a node
closes the match, even if that node miscounted the turns: the node writes the turn number, and the
oracle does not use it.

The oracle is four deterministic checks, and the outcome is the number of checks passed out of 4.
Three read the rows of the message log; the third reads prompt-D.txt, because it measures the view
that the new node receives from the mechanism, not D's choice to look at it.
"""

import json
import re
from datetime import datetime
from pathlib import Path

INITIAL_NODES = ("A", "B", "C")
NEW_NODE = "D"
NODES = (*INITIAL_NODES, NEW_NODE)
CHANGE_TURN = 6
LAST_TURN = 16
DIRECTORY = Path(__file__).resolve().parent
GRAPH_V1 = DIRECTORY / "graph-v1.json"
GRAPH_V2 = DIRECTORY / "graph-v2.json"
FIRST_LINE = "turno 0: Il cadavere squisito berrà il vino nuovo."
NOTICE = "Il grafo è cambiato: rileggi la tua vista con `bus view`. Questo messaggio non è un turno."
TURN_PATTERN = re.compile(r"\s*turno\s+(\d+)\s*:", re.IGNORECASE)

JOINING_OPEN_MATCH = """Entri a partita già cominciata: i turni precedenti non li hai visti, e non ti servono.

"""

PROMPT = """Sei il nodo {node} di una partita di «cadavere squisito» nel motore antfarm: un testo scritto a turno, dove ognuno vede solo l'ultima riga del precedente.

{joining}La tua vista del grafo, cioè a chi puoi scrivere:

{view}

Il tuo successore è il nodo, diverso da director, che la vista elenca su «Puoi scrivere a».

Il tuo solo strumento è il bus, un comando Bash:

  {bus} recv
      aspetta il prossimo messaggio per te e lo stampa: la prima riga dice chi lo manda, il testo sta dopo la riga ---. Ogni chiamata restituisce un messaggio solo; se il comando esce senza un messaggio, richiamalo
  {bus} send <nodo> "<testo>"
      consegna il testo al nodo; il testo fra virgolette doppie, su una sola riga
  {bus} view
      stampa la tua vista del grafo, com'è adesso

Scrivi ogni comando col path intero del bus. Puoi unire l'invio e l'attesa del messaggio dopo in un solo comando: `{bus} send <nodo> "<testo>" && {bus} recv`.

Le regole:

1. Ogni turno arriva come messaggio nella forma `turno N: <riga>`.
2. Quando ricevi il turno N scrivi una riga sola, nuova, di fantasia, di al massimo 12 parole, che continua quella che hai ricevuto. Non riportare mai le righe precedenti.
3. Manda la tua riga al tuo successore nella forma `turno N+1: <la tua riga>`, col numero calcolato.
4. Se hai ricevuto il turno {last}, manda la tua riga a director invece che al successore, nella forma `turno {after_last}: <la tua riga>`. A director scrivi solo in questo caso.
5. Se director ti avvisa che il grafo è cambiato, l'avviso non è un turno: esegui `{bus} view`, ricava da lì il tuo successore e torna ad aspettare con `{bus} recv`.
6. La partita finisce quando director ti ferma. Fino ad allora non chiudere da solo: dopo ogni invio torna ad aspettare con `{bus} recv`.

Comincia eseguendo `{bus} recv`: il primo messaggio può arrivare dopo alcuni minuti, ed è previsto.
"""


def node_prompt(node, view, bus):
    """The launch prompt: the single-line rule, the bus usage, the view at the current version."""
    joining = JOINING_OPEN_MATCH if node == NEW_NODE else ""
    return PROMPT.format(node=node, joining=joining, view=view, bus=bus, last=LAST_TURN, after_last=LAST_TURN + 1)


def between_nodes(log_rows):
    """The `delivered` rows between two game nodes, in log order: the turns of the corpse."""
    return [row for row in log_rows
            if row.get("outcome") == "delivered" and row.get("from") in NODES and row.get("to") in NODES]


def arrivals(log_rows):
    """The `delivered` rows from a game node to director."""
    return [row for row in log_rows
            if row.get("outcome") == "delivered" and row.get("from") in NODES and row.get("to") == "director"]


def declared_turn(text):
    """The turn number the node wrote in the text; None if the text does not start with `turno N:`."""
    hit = TURN_PATTERN.match(text or "")
    return int(hit.group(1)) if hit else None


def instant(ts):
    return datetime.fromisoformat(ts)


def consistent_version(row, effective_v2):
    """The version of a message against the time v2 took effect. The bus reads time and version
    under the same lock with which the director writes the change, so a message with an earlier
    time carries v1 and one with a later time carries v2. The log has millisecond precision: within
    the same millisecond as the change both are valid."""
    if effective_v2 is None:
        return row["version"] == 1
    departure, change = instant(row["ts"]), instant(effective_v2)
    if departure == change:
        return row["version"] in (1, 2)
    return row["version"] == (1 if departure < change else 2)


def oracle(log_rows, effective_v2, prompt_text_d):
    """The four checks. `log_rows` are the match rows, `effective_v2` the time in graphs/v2.json
    (None if v2 did not take effect), `prompt_text_d` the content of prompt-D.txt (None if the
    file is missing)."""
    turns = between_nodes(log_rows)
    wrong_version = [row for row in turns if not consistent_version(row, effective_v2)]
    c_a_with_v2 = [row for row in turns if row["from"] == "C" and row["to"] == "A" and row["version"] == 2]
    multi_line = [row for row in turns if len((row["text"] or "").splitlines()) != 1]
    view_v2 = prompt_text_d is not None and "grafo versione 2" in prompt_text_d
    edge_d_a = prompt_text_d is not None and "- D → A: " in prompt_text_d
    checks = [
        {"check": 1, "name": "departure version", "passed": not wrong_version,
         "evidence": f"{len(turns)} messages between nodes, {len(wrong_version)} with the wrong version",
         "off_rule": [row["id"] for row in wrong_version]},
        {"check": 2, "name": "no delivery on C → A with v2", "passed": not c_a_with_v2,
         "evidence": f"{len(c_a_with_v2)} deliveries on C → A with version 2",
         "off_rule": [row["id"] for row in c_a_with_v2]},
        {"check": 3, "name": "the new node's view", "passed": view_v2 and edge_d_a,
         "evidence": ("prompt-D.txt missing" if prompt_text_d is None
                      else f"graph version 2: {'yes' if view_v2 else 'no'} · edge D → A: {'yes' if edge_d_a else 'no'}"),
         "off_rule": []},
        {"check": 4, "name": "one line per message", "passed": not multi_line,
         "evidence": f"{len(multi_line)} messages between nodes with more than one line",
         "off_rule": [row["id"] for row in multi_line]},
    ]
    passed_checks = sum(check["passed"] for check in checks)
    return {"checks": checks, "passed": passed_checks, "outcome": f"{passed_checks}/{len(checks)}", "messages_between_nodes": len(turns)}


def effective_v2(match_dir):
    """The time version 2 took effect; None if the match did not get there."""
    path = Path(match_dir) / "graphs" / "v2.json"
    return json.loads(path.read_text(encoding="utf-8"))["effective"] if path.is_file() else None


def match_oracle(match_dir, log_rows):
    """The oracle on the files of a match: the log, graphs/v2.json, prompt-D.txt."""
    prompt_file = Path(match_dir) / f"prompt-{NEW_NODE}.txt"
    prompt_text = prompt_file.read_text(encoding="utf-8") if prompt_file.is_file() else None
    change = effective_v2(match_dir)
    return {**oracle(log_rows, change, prompt_text), "effective_v2": change}


def received_after_change(log_rows, effective_v2):
    """The messages sent with v1 and received after v2 took effect."""
    if effective_v2 is None:
        return []
    change = instant(effective_v2)
    receipts = {row["id"]: row for row in log_rows if row.get("outcome") == "received"}
    result = []
    for row in log_rows:
        receipt = receipts.get(row.get("id"))
        if row.get("outcome") == "delivered" and row.get("version") == 1 and receipt and instant(receipt["ts"]) > change:
            result.append({**row, "received": receipt["ts"]})
    return result
