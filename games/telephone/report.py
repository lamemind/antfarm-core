"""End-of-match report of the telephone game, written by the director without LLMs.

Every figure comes from one of three sources, and every section says which: the match row in the
match registry, the match rows in the message log, the node transcripts. An agent's own
account does not enter: the text of the messages is quoted only as log content.

The sections common to every game — messages per edge, tokens per node, human turns, transcripts —
come from `bin/measures.py`; here remain the outcome, the numbers per hop and the message at arrival.
"""

import json

import game
import measures

def compose(match_id, rule, facts, log_rows, observation, registry, registry_path, log_path):
    delivered = [row for row in log_rows if row["outcome"] == "delivered"]
    departure = next((row for row in delivered if row["from"] == "director" and row["to"] == "A"), None)
    cap = game.word_cap(departure["text"]) if rule == "rephrase" and departure else None
    outcome = game.oracle(log_rows, facts, cap)
    total = len(facts)
    text = [
        f"# Report — {match_id}",
        "",
        f"Telephone, rule `{rule}`. Three sources, named in every section:",
        "",
        f"- **registry** — `{registry_path}`, the row with `match` = `{match_id}`",
        f"- **log** — `{log_path}`, the rows with `match` = `{match_id}`",
        "- **transcript** — the node files, listed at the end",
        "",
        "## Outcome",
        "",
    ]
    if registry is None:
        text += ["**Registry row missing**: the match did not reach the director's close.", ""]
    else:
        text += [
            f"**{registry['outcome']}** intact numbers at arrival · **{registry['human_turns']}** human turns · "
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

    text += ["## Numbers per hop", ""]
    if departure:
        numbers = ", ".join(str(fact["number"]) for fact in facts)
        text += [f"The {total} numbers are those of the message director → A: {numbers}. "
                  "A number is intact if it appears in the text of the hop as an isolated group of digits.", ""]
    header = "| Hop | Intact | Lost in this hop | Words | Seconds since the previous hop |"
    alignment = "| --- | --- | --- | --- | --- |"
    if cap is not None:
        header += " Within the cap |"
        alignment += " --- |"
    text += [header, alignment]
    previous_instant = None
    for hop in outcome["hops"]:
        row = next((r for r in delivered if r["from"] == hop["from"] and r["to"] == hop["to"]), None)
        if hop.get("missing"):
            cells = [f"{hop['from']} → {hop['to']}", "not delivered", "—", "—", "—"] + (["—"] if cap is not None else [])
        else:
            seconds = measures.seconds_between(previous_instant, row["ts"]) if previous_instant else "—"
            cells = [f"{hop['from']} → {hop['to']}", f"{hop['intact']}/{total}",
                     ", ".join(str(number) for number in hop["lost"]) or "—", str(hop["words"]), str(seconds)]
            if cap is not None:
                cells.append("—" if hop["from"] == "director" else ("yes" if hop["within_cap"] else "no"))
            previous_instant = row["ts"]
        text.append("| " + " | ".join(cells) + " |")
    text += ["", "Source: log, `delivered` rows per edge — fields `text`, `words`, `ts`. "
              "The seconds are the difference between the `ts` of two consecutive `delivered` rows, rounded to the second."]
    if cap is not None and departure:
        sentence = f"al massimo {cap} parole"
        without_cap = [session["node"] for session in observation["sessions"]
                       if sentence not in measures.launch_prompt(session["transcript"])]
        text += [f"Cap: {cap} words, the one that the launch prompt of each node carries («{sentence}»). "
                  "Source: transcript, first human record of each node."
                  + (f" ⚠️ Prompts without that cap: {', '.join(without_cap)}." if without_cap else "")]
    text.append("")

    text += measures.messages_per_edge_section(log_rows)
    text += measures.tokens_per_node_section(observation)
    text += measures.human_turns_section(observation, log_rows)

    arrival = next((row for row in delivered if row["from"] == "D" and row["to"] == "director"), None)
    if arrival:
        text += ["## The message at arrival", "", "> " + arrival["text"], "",
                  f"Source: log, message `{arrival['id']}` from D to the director.", ""]

    text += measures.transcript_section(observation)
    return "\n".join(text), outcome


def write(match_dir, registry_path, log_path, observation):
    meta = json.loads((match_dir / "meta.json").read_text(encoding="utf-8"))
    fact_data = json.loads((match_dir / "facts.json").read_text(encoding="utf-8"))
    log_rows = measures.log_rows(log_path, match_dir.name)
    registry = measures.registry_row(registry_path, match_dir.name)
    content, _ = compose(match_dir.name, meta["rule"], fact_data["facts"], log_rows, observation,
                           registry, *measures.paths_from_project(registry_path, log_path))
    (match_dir / "report.md").write_text(content, encoding="utf-8")
    return match_dir / "report.md"
