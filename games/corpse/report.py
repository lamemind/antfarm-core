"""End-of-match report of the exquisite corpse, written by the director without an LLM.

Every figure comes from one of four sources, and every section says which: the match row in the
match registry, the match rows in the message log, the node transcripts, the files the director
writes in the match directory — meta.json, nodes.json, graphs/, prompt-D.txt. An agent's account
does not enter: the text of the lines is quoted only as log content.

`measure` computes the numbers and `compose` writes them. The sections common to every game come
from `bin/measures.py`; here remain the outcome with the four checks, the graph change, the
recomposed text, the messages sent with v1 and received after the change, the new node's view.
"""

import json
from pathlib import Path

import graph as libgraph
import measures
import match as libmatch

from . import game

CLOSURES = {
    "arrival": "a node sent its line to director",
    "timeout": "match timeout",
    "token-cap": "tokens over the cap",
}


def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def cell(text):
    return (text or "").replace("|", "¦").replace("\n", " ⏎ ")


def text_lines(rows):
    """The lines of the recomposed text, in log order: the director's turn 0 to A, the turns between
    nodes, the arrival at the director. Each one carries the turn expected from its position and the
    one written by the node; the notice to C is not a line of the text."""
    delivered = [row for row in rows if row["outcome"] == "delivered"]
    filtered = [row for row in delivered
                if row["from"] in game.NODES or (row["from"] == "director" and row["to"] == "A")]
    return [{"row": row, "expected": expected, "written": game.declared_turn(row["text"])}
            for expected, row in enumerate(filtered)]


def without_prefix(text):
    """The text of the line without the leading `turno N:`, if present."""
    hit = game.TURN_PATTERN.match(text or "")
    return (text or "")[hit.end():].strip() if hit else (text or "")


def first_view_of_d(observation):
    """The outcome of D's first `bus view`, from the transcript; None if D did not call it or is not a session."""
    session = next((entry for entry in observation["sessions"] if entry["node"] == game.NEW_NODE), None)
    if session is None or not session["transcript"]:
        return None
    for name, command, outcome, _ in libmatch.bash_calls(session["transcript"]):
        if name == "Bash" and command.strip() == f"{libmatch.BUS} view":
            return outcome
    return None


def measure(match_dir, registry_path, log_path, observation):
    """All the numbers of a match report, from its sources."""
    match_dir = Path(match_dir)
    match_id = match_dir.name
    meta = load_json(match_dir / "meta.json")
    nodes = load_json(match_dir / "nodes.json")["nodes"]
    rows = measures.log_rows(log_path, match_id)
    oracle = game.match_oracle(match_dir, rows)
    v1, v2 = match_dir / "graphs" / "v1.json", match_dir / "graphs" / "v2.json"
    delta = libgraph.delta(load_json(v1)["graph"], load_json(v2)["graph"]) if v1.is_file() and v2.is_file() else None
    prompt_file = match_dir / f"prompt-{game.NEW_NODE}.txt"
    notices = [row for row in rows if row["outcome"] == "delivered" and row["from"] == "director" and row["to"] == "C"]
    return {
        "match": match_id,
        "closure": meta.get("closure"),
        "registry": measures.registry_row(registry_path, match_id),
        "oracle": oracle,
        "delta": delta,
        "notices": notices,
        "notice_receipts": [row for row in rows if row["outcome"] == "received" and row["id"] in {notice["id"] for notice in notices}],
        "text_lines": text_lines(rows),
        "rejections": [row for row in rows if row["outcome"] == "rejected"],
        "received_after": game.received_after_change(rows, oracle["effective_v2"]),
        "prompt_text_d": prompt_file.read_text(encoding="utf-8") if prompt_file.is_file() else None,
        "first_view_of_d": first_view_of_d(observation),
        "actions": {node: nodes[node].get("actions") for node in game.NODES if node in nodes},
        "rows": rows,
    }


def compose(measured, observation, registry_path, log_path):
    registry, oracle = measured["registry"], measured["oracle"]
    text = [
        f"# Report — {measured['match']}",
        "",
        "Exquisite corpse: each player writes a line seeing only the last line of the previous one, and a new node "
        "enters while the match is open. The text is thrown away: the report checks whether each message carries the "
        "version it departed with and whether the new node receives the graph as it is now. Four sources, named in every section:",
        "",
        f"- **registry** — `{registry_path}`, the row with `match` = `{measured['match']}`",
        f"- **log** — `{log_path}`, the rows with `match` = `{measured['match']}`",
        "- **transcript** — the node files, listed at the end",
        "- **director** — `meta.json`, `nodes.json`, `graphs/` and `prompt-D.txt` in the match directory",
        "",
        "## Outcome",
        "",
    ]
    if registry is None:
        text += ["**Registry row missing**: the match did not reach the director's close, "
                  "or it is a dry-run match.", ""]
    else:
        text += [
            f"**{registry['outcome']}** checks passed · **{registry['human_turns']}** human turns · "
            f"**{measures.thousands(int(registry['token']))}** tokens",
            "",
            "Source: registry, columns `outcome`, `human_turns`, `token`.",
            "",
        ]
        discrepancies = []
        if registry["outcome"] != oracle["outcome"]:
            discrepancies.append(f"outcome {registry['outcome']} in the registry, {oracle['outcome']} from the rerun oracle")
        discrepancies += measures.observation_discrepancies(registry, observation)
        text += [f"⚠️ **Discrepancy**: {'; '.join(discrepancies)}." if discrepancies
                  else "The registry agrees with the rerun oracle and with the recount from the transcripts.", ""]
    text += [f"Closure: {CLOSURES.get(measured['closure'], measured['closure'])}. Source: director, `meta.json`, field `closure`.", "",
              "| # | Check | Outcome | Evidence |", "| --- | --- | --- | --- |"]
    text += [f"| {entry['check']} | {entry['name']} | {'passed' if entry['passed'] else '**failed**'} | {cell(entry['evidence'])} |"
              for entry in oracle["checks"]]
    text += ["", "Source: oracle rerun here on the log, `graphs/v2.json` and `prompt-D.txt`.", ""]

    text += ["## The graph change", ""]
    delta = measured["delta"]
    if delta is None:
        text += ["Version 2 did not take effect: the match did not reach the sixth message between nodes.", ""]
    else:
        nodes = ", ".join(node["id"] for node in delta["nodes_added"]) or "none"
        added = ", ".join(f"{edge['from']} → {edge['to']}" for edge in delta["edges_added"]) or "none"
        removed = ", ".join(f"{edge['from']} → {edge['to']}" for edge in delta["edges_removed"]) or "none"
        text += [f"Version 2 effective since `{oracle['effective_v2']}`. Nodes added: {nodes}. Edges added: {added}. Edges removed: {removed}.",
                  "Source: director, `graphs/v1.json` and `graphs/v2.json`.", ""]
        for notice in measured["notices"]:
            receipt = next((row for row in measured["notice_receipts"] if row["id"] == notice["id"]), None)
            text.append(f"Notice to C: sent `{notice['ts']}` with version {notice['version']}, "
                         + (f"received `{receipt['ts']}`." if receipt else "never received."))
        text += ["", "Source: log, `delivered` and `received` rows from director to C.", ""]
    rejections_c_a = [row for row in measured["rejections"] if row["from"] == "C" and row["to"] == "A"]
    times = "time" if len(rejections_c_a) == 1 else "times"
    text += [f"**C wrote to A after the change: {len(rejections_c_a)} {times}** (rejected rows C → A)."
              + (" ⚠️ The T41 measure «graph change while the match is running» would count them as edges requested by an agent." if rejections_c_a else ""),
              "", f"Total rejections in the match: {len(measured['rejections'])}. Source: log, `rejected` rows.", ""]

    entries = measured["text_lines"]
    wrong = [entry for entry in entries if entry["written"] != entry["expected"]]
    text += ["## The recomposed text", "",
              f"One line per turn, in log order. The Position column is the turn the line occupies in the log, Written turn the one "
              f"the node put in the text: **{len(wrong)}** do not match.", "",
              "| Position | Written turn | Author → recipient | Version | Line |", "| --- | --- | --- | --- | --- |"]
    for entry in entries:
        row = entry["row"]
        written = "—" if entry["written"] is None else str(entry["written"])
        if entry["written"] != entry["expected"]:
            written += " ⚠️"
        text.append(f"| {entry['expected']} | {written} | {row['from']} → {row['to']} | {row['version']} | {cell(without_prefix(row['text']))} |")
    text += ["", "Source: log, `delivered` rows from a game node or from director to A: fields `from`, `to`, `version`, `text`.", ""]

    received = measured["received_after"]
    text += ["## Messages sent with v1 and received after the change", ""]
    if received:
        text += ["| From → to | Sent | Received | Text |", "| --- | --- | --- | --- |"]
        text += [f"| {row['from']} → {row['to']} | {row['ts']} | {row['received']} | {cell(row['text'])} |" for row in received]
    else:
        text.append("None.")
    text += ["", "The bus delivered them with v1, and that is correct: the version tells with which graph a message departed. "
              "Source: log, `delivered` rows with version 1 whose `received` row has a `ts` later than the time v2 took effect.", ""]

    text += ["## The new node's view", ""]
    prompt_d = measured["prompt_text_d"]
    if prompt_d is None:
        text.append("`prompt-D.txt` missing: D was not launched.")
    else:
        excerpt = [line for line in prompt_d.splitlines() if line.startswith("# Vista di D") or line.startswith("- D → ")]
        text += ["The view in D's launch prompt:", "", *[f"    {line}" for line in excerpt]]
    first_view = measured["first_view_of_d"]
    text += ["", (f"D's first `bus view`: `{cell(first_view)}`." if first_view
                   else "D did not call `bus view`, or is not a session: the view reached it with the prompt, which is what the oracle measures."),
              "", "Source: director, `prompt-D.txt`; D's transcript for the `bus view`, outside the oracle.", ""]

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
