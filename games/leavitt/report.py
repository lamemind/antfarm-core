"""End-of-match report of Leavitt's game, written by the director without LLMs.

Every figure comes from one of four sources, and every section says which: the match row in the
match registry, the match rows in the message log, the node transcripts, and meta.json and
cards.json, written by the director. An agent's own account does not enter: the text of the
messages is quoted only as log content.

`measure` computes the numbers and `compose` writes them; the topology comparison calls the same
`measure`, so its numbers are those of the reports. The sections common to every game come
from `bin/measures.py`; here remain outcome, cards, declarations and centrality.
"""

import json
from pathlib import Path

import graph as libgraph
import measures

from . import cards as libcards
from . import game


def decimal(number):
    return f"{number:.2f}"


def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def centrality(rows, graph):
    """Messages between players — `delivered` rows with sender and recipient among A–E —, degree of
    each node towards the other players, and the max-degree node that receives the most against the
    mean of the other four: in the wheel it is the center, in the other topologies the most loaded
    among the max-degree nodes."""
    between_players = [row for row in rows
                     if row["outcome"] == "delivered" and row["from"] in game.NODES and row["to"] in game.NODES]
    per_node = {node: {
        "degree": len([recipient for recipient in libgraph.recipients(graph, node, "write") if recipient in game.NODES]),
        "sent": sum(1 for row in between_players if row["from"] == node),
        "incoming": sum(1 for row in between_players if row["to"] == node),
    } for node in game.NODES}
    max_degree = max(entry["degree"] for entry in per_node.values())
    center = max((node for node in game.NODES if per_node[node]["degree"] == max_degree),
                 key=lambda node: per_node[node]["incoming"])
    others_mean = sum(per_node[node]["incoming"] for node in game.NODES if node != center) / (len(game.NODES) - 1)
    return {
        "per_node": per_node,
        "total": len(between_players),
        "center": center,
        "center_degree": max_degree,
        "center_incoming": per_node[center]["incoming"],
        "others_mean": round(others_mean, 2),
        "saturation": round(per_node[center]["incoming"] / others_mean, 2) if others_mean else None,
    }


def measure(match_dir, registry_path, log_path, observation):
    """All the numbers of a match report, from its sources."""
    match_dir = Path(match_dir)
    match_id = match_dir.name
    meta = load_json(match_dir / "meta.json")
    card_data = load_json(match_dir / "cards.json")
    rows = measures.log_rows(log_path, match_id)
    outcome = game.oracle(rows, card_data)
    delivered_cards = {row["to"]: row for row in rows
                  if row["outcome"] == "delivered" and row["from"] == "director" and row["to"] in game.NODES}
    first_card = min((row["ts"] for row in delivered_cards.values()), default=None)
    declarations = []
    for entry in outcome["per_node"]:
        entry = dict(entry)
        if not entry.get("missing") and first_card:
            entry["seconds"] = measures.seconds_between(first_card, entry["ts"])
        entry["subsequent"] = sum(1 for later in outcome["subsequent"] if later["node"] == entry["node"])
        declarations.append(entry)
    declared_seconds = [entry["seconds"] for entry in declarations if "seconds" in entry]
    first_exchange = next((row["ts"] for row in rows if row["outcome"] == "delivered"
                          and row["from"] in game.NODES and row["to"] in game.NODES), None)
    nodes = load_json(match_dir / "nodes.json")["nodes"]
    return {
        "match": match_id,
        "topology": meta["topology"],
        "seed": meta["seed"],
        "closure": meta.get("closure"),
        "registry": measures.registry_row(registry_path, match_id),
        "outcome": outcome,
        "cards": {node: {"delivered": libcards.symbols_in(row["text"], card_data["symbols"]),
                         "id": row["id"], "from_cards_json": card_data["cards"][node]}
                  for node, row in delivered_cards.items()},
        "card_data": card_data,
        "first_card": first_card,
        "declarations": declarations,
        "seconds_fifth": max(declared_seconds) if len(declared_seconds) == len(game.NODES) else None,
        "seconds_last": max(declared_seconds, default=None),
        "seconds_first_exchange": measures.seconds_between(first_card, first_exchange) if first_card and first_exchange else None,
        "actions": {node: nodes.get(node, {}).get("actions") for node in game.NODES},
        "centrality": centrality(rows, load_json(match_dir / "graph.json")),
        "messages_per_node": measures.messages_per_node(rows, ["director", *game.NODES]),
        "delivered": sum(1 for row in rows if row["outcome"] == "delivered"),
        "token": observation["token"],
        "tokens_per_node": {session["node"]: session["token"] for session in observation["sessions"]},
        "assistant_turns": sum(session["assistant_turns"] for session in observation["sessions"]),
        "human_turns": observation["human_turns"],
        "rows": rows,
    }


def compose(measured, observation, registry_path, log_path):
    outcome, registry = measured["outcome"], measured["registry"]
    total = len(game.NODES)
    text = [
        f"# Report — {measured['match']}",
        "",
        f"Leavitt's game, topology `{measured['topology']}`, seed {measured['seed']}. Four sources, named in every section:",
        "",
        f"- **registry** — `{registry_path}`, the row with `match` = `{measured['match']}`",
        f"- **log** — `{log_path}`, the rows with `match` = `{measured['match']}`",
        "- **transcript** — the node files, listed at the end",
        "- **director** — `meta.json` and `cards.json` in the match directory",
        "",
        "## Outcome",
        "",
    ]
    if registry is None:
        text += ["**Registry row missing**: the match did not reach the director's close, "
                  "or it is a dry-run match.", ""]
    else:
        text += [
            f"**{registry['outcome']}** correct declarations · **{registry['human_turns']}** human turns · "
            f"**{measures.thousands(int(registry['token']))}** tokens",
            "",
            "Source: registry, columns `outcome`, `human_turns`, `token`.",
            "",
        ]
        discrepancies = []
        if registry["outcome"] != outcome["outcome"]:
            discrepancies.append(f"outcome {registry['outcome']} in the registry, {outcome['outcome']} recomputed from the log")
        discrepancies += measures.observation_discrepancies(registry, observation)
        text += [f"⚠️ **Discrepancy**: {'; '.join(discrepancies)}." if discrepancies
                  else "The registry agrees with the recomputation from the log and the transcripts.", ""]
    closures = {"five-declarations": "five nodes declared", "timeout": "match timeout",
                "token-cap": "tokens over the cap"}
    text += [f"Closure: {closures.get(measured['closure'], measured['closure'])}. Source: director, `meta.json`, field `closure`.", ""]

    text += ["## The cards", "",
              f"Common symbol: **{outcome['answer']}**. Match symbols: "
              + ", ".join(f"`{symbol}`" for symbol in measured["card_data"]["symbols"]) + ".", "",
              "| Node | Delivered card | Missing symbol | Equal to cards.json |", "| --- | --- | --- | --- |"]
    for node in game.NODES:
        card = measured["cards"].get(node)
        if card is None:
            text.append(f"| {node} | not delivered | — | — |")
            continue
        lacking = [symbol for symbol in measured["card_data"]["symbols"] if symbol not in card["delivered"]]
        equal = sorted(card["delivered"]) == sorted(card["from_cards_json"])
        text.append(f"| {node} | {' '.join(card['delivered'])} | {', '.join(lacking)} | {'yes' if equal else 'no'} |")
    text += ["", "Source: log, `delivered` rows from director to each node, the match symbols found in the `text`; "
              "director, `cards.json`, for the common symbol and the comparison.", ""]

    text += ["## Declarations", "",
              "| Node | Symbol | Value | Seconds since the first card | Later messages to director | Text |",
              "| --- | --- | --- | --- | --- | --- |"]
    for entry in measured["declarations"]:
        if entry.get("missing"):
            text.append(f"| {entry['node']} | — | missing | — | 0 | — |")
        else:
            text.append(f"| {entry['node']} | {entry['symbol'] or '—'} | {entry['value']} | {entry['seconds']} "
                         f"| {entry['subsequent']} | {entry['text'].replace('|', '¦')} |")
    if measured["seconds_fifth"] is not None:
        timing = f"Time to the fifth declaration: **{measured['seconds_fifth']} s** from the first card."
    else:
        timing = (f"Fifth declaration did not arrive: {outcome['declarations']} declarations, the last one at "
                 f"{measured['seconds_last'] if measured['seconds_last'] is not None else '—'} s from the first card.")
    text += ["", timing, "",
              f"Correct {outcome['correct']} · wrong {outcome['wrong']} · void {outcome['void']} · "
              f"missing {len(outcome['missing'])}, out of {total}.", "",
              "Source: log, `delivered` rows towards the director: for each node the first one is the declaration, "
              "the others are later. Valid if the `text` carries exactly one of the six symbols as an isolated word, "
              "void otherwise. The seconds are the difference between the `ts` of the declaration and that of the "
              "first card delivered by the director, rounded to the second.", ""]

    center = measured["centrality"]
    text += ["## Centrality", "",
              "| Node | Degree | Sent to other players | Received from other players |", "| --- | --- | --- | --- |"]
    for node in game.NODES:
        entry = center["per_node"][node]
        text.append(f"| {node} | {entry['degree']} | {entry['sent']} | {entry['incoming']} |")
    first = measured["seconds_first_exchange"]
    saturation = (f"{decimal(center['saturation'])}×" if center["saturation"] is not None
                   else "not computable (mean of the others 0)")
    text += ["", f"Messages between players: **{center['total']}**. Most loaded max-degree node: **{center['center']}** "
              f"(degree {center['center_degree']}), {center['center_incoming']} messages received against a mean of "
              f"{decimal(center['others_mean'])} in the other four: saturation **{saturation}**. First message between players: "
              f"{str(first) + ' s' if first is not None else 'none'} from the first card.", "",
              "Source: log, `delivered` rows with `from` and `to` among A–E; degree from the match's `graph.json`, `write` "
              "edges towards other players. Among several max-degree nodes, the one that receives the most counts.", ""]

    text += measures.off_bus_actions_section(measured["actions"])
    text += measures.messages_per_node_section(measured["rows"], ["director", *game.NODES])
    text += measures.messages_per_edge_section(measured["rows"])
    text += measures.tokens_per_node_section(observation)
    text += measures.human_turns_section(observation, measured["rows"])
    text += measures.transcript_section(observation)
    return "\n".join(text)


def write(match_dir, registry_path, log_path, observation):
    measured = measure(match_dir, registry_path, log_path, observation)
    content = compose(measured, observation, *measures.paths_from_project(registry_path, log_path))
    (Path(match_dir) / "report.md").write_text(content, encoding="utf-8")
    return Path(match_dir) / "report.md"
