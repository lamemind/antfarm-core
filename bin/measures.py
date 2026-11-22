"""The measures common to the end-of-match reports: they depend on registry, log and transcripts, not on the game.

Each game writes its own report with its own rules, its own oracle and its own
tables; the sections here — off-bus actions, messages per edge, messages per node, tokens per node,
human turns and native channel, list of the transcripts — do not depend on the game, and each one carries its source.
Every `*_section` function returns the markdown lines of the section, final empty line included.
"""

import json
from collections import Counter
from datetime import datetime
from pathlib import Path

import match as libmatch

USAGE_NAMES = {
    "input_tokens": "input",
    "output_tokens": "output",
    "cache_creation_input_tokens": "cache creation",
    "cache_read_input_tokens": "cache read",
}


def thousands(number):
    return f"{number:,}"


def seconds_between(before, after):
    return round((datetime.fromisoformat(after) - datetime.fromisoformat(before)).total_seconds())


def registry_row(registry_path, match_id):
    """The row of the match in the match registry, as a column → value dictionary; None if
    the row or the registry is missing."""
    if not Path(registry_path).is_file():
        return None
    with open(registry_path, encoding="utf-8") as registry:
        header = registry.readline().rstrip("\n").split("\t")
        for row in registry:
            values = dict(zip(header, row.rstrip("\n").split("\t")))
            if values.get("match") == match_id:
                return values
    return None


def log_rows(log_path, match_id):
    with open(log_path, encoding="utf-8") as log:
        return [row for row in map(json.loads, log) if row.get("match") == match_id]


def paths_from_project(registry_path, log_path):
    """Registry and log as paths from the state root of the project, to cite them in the report:
    `registry/matches.tsv` and `registry/messages.jsonl`, never the path on the machine. The root is the
    directory above `registry/`."""
    project_root = Path(registry_path).resolve().parents[1]
    return (Path(registry_path).resolve().relative_to(project_root),
            Path(log_path).resolve().relative_to(project_root))


def launch_prompt(transcript_path):
    """The text of the first human record of the transcript: the prompt with which the director launched the node."""
    if not transcript_path:
        return ""
    with open(transcript_path, encoding="utf-8") as transcript:
        for record in map(json.loads, transcript):
            if record.get("type") == "user" and (record.get("origin") or {}).get("kind") == "human":
                content = (record.get("message") or {}).get("content")
                if isinstance(content, list):
                    return "\n".join(block.get("text", "") for block in content if isinstance(block, dict))
                return content or ""
    return ""


def observation_discrepancies(registry, observation):
    """Human turns and tokens of the registry against the recount from the transcripts: one sentence per discrepancy."""
    discrepancies = []
    if int(registry["human_turns"]) != observation["human_turns"]:
        discrepancies.append(f"human turns {registry['human_turns']} in the registry, {observation['human_turns']} from the transcripts")
    if int(registry["token"]) != observation["token"]:
        discrepancies.append(f"tokens {registry['token']} in the registry, {observation['token']} from the transcripts")
    return discrepancies


def messages_per_edge_section(rows):
    """One row per (from, to) pair of the log. The Written column — `written` rows toward a
    source — is there only if the match has any, so the reports of the games without sources do not change."""
    with_written = any(row["outcome"] == "written" for row in rows)
    columns = ["Delivered", "Rejected", "Received"] + (["Written"] if with_written else [])
    outcomes = ["delivered", "rejected", "received"] + (["written"] if with_written else [])
    text = ["## Messages per edge", "", "| Edge | " + " | ".join(columns) + " |", "| --- |" + " --- |" * len(columns)]
    counts = Counter((row["from"], row["to"], row["outcome"]) for row in rows)
    edges = list(dict.fromkeys((row["from"], row["to"]) for row in rows))
    for from_node, to_node in edges:
        text.append(f"| {from_node} → {to_node} | " + " | ".join(str(counts[(from_node, to_node, outcome)]) for outcome in outcomes) + " |")
    reasons = Counter(row["reason"] for row in rows if row["outcome"] == "rejected")
    text += ["", "Rejections by reason: " + (", ".join(f"`{reason}` {number}" for reason, number in reasons.items())
                                            if reasons else "none") + ".",
              "", "Source: log, count of the rows by `from`, `to`, `outcome`, `reason`.", ""]
    return text


def messages_per_node(rows, nodes):
    """node → {sent, incoming, received, rejected}: delivered with `from` = node, delivered with
    `to` = node, receipts of the node, sends of the node that were rejected."""
    counts = {node: {"sent": 0, "incoming": 0, "received": 0, "rejected": 0} for node in nodes}
    for row in rows:
        if row["outcome"] == "delivered":
            if row["from"] in counts:
                counts[row["from"]]["sent"] += 1
            if row["to"] in counts:
                counts[row["to"]]["incoming"] += 1
        elif row["outcome"] == "received" and row["to"] in counts:
            counts[row["to"]]["received"] += 1
        elif row["outcome"] == "rejected" and row["from"] in counts:
            counts[row["from"]]["rejected"] += 1
    return counts


def messages_per_node_section(rows, nodes):
    counts = messages_per_node(rows, nodes)
    text = ["## Messages per node", "", "| Node | Sent | Incoming | Received | Rejected |", "| --- | --- | --- | --- | --- |"]
    for node in nodes:
        entry = counts[node]
        text.append(f"| {node} | {entry['sent']} | {entry['incoming']} | {entry['received']} | {entry['rejected']} |")
    text += ["", "Source: log — sent: `delivered` rows with `from` = node; incoming: `delivered` rows with "
              "`to` = node; received: `received` rows with `to` = node; rejected: `rejected` rows with `from` = node.", ""]
    return text


def off_bus_actions_section(actions_per_node):
    """The actions of each node split between denied bus calls and off-channel actions, with the
    total row. `actions_per_node` is node → `actions` field of the nodes.json entry; `None`, a
    node without a list, writes a dash and does not enter the total. The class of an entry is decided
    here, at composition: nodes.json stays the raw data."""
    text = ["## Off-bus actions", "", "| Node | Denied bus calls | Off-channel actions |", "| --- | --- | --- |"]
    totals = Counter()
    for node, actions in actions_per_node.items():
        if actions is None:
            text.append(f"| {node} | — | — |")
            continue
        classes = Counter(libmatch.action_class(action) for action in actions)
        totals.update(classes)
        text.append(f"| {node} | {classes[libmatch.DENIED_BUS_CALL]} | {classes[libmatch.OFF_CHANNEL_ACTION]} |")
    text.append(f"| Total | {totals[libmatch.DENIED_BUS_CALL]} | {totals[libmatch.OFF_CHANNEL_ACTION]} |")
    text += ["", "The tool calls of a node that are not a Bash command starting with the path of the bus, split in two. "
              "**Denied bus call**: the outcome is the rejection by `--permission-mode dontAsk` and every segment of the command — "
              "split on `;`, `&&`, `||`, `|` and newline, outside quotes — is a variable assignment or "
              "an invocation of the bus, with the path or with the variable that carries it, and at least one invokes it: the node wanted to use "
              "the channel, and the rule `Bash(<bus> *)` covers only the commands that start with the path. **Off-channel "
              "action**: every other entry. Source: director, `nodes.json`, `actions` field of each node, classified when "
              "the report is composed.", ""]
    return text


def tokens_per_node_section(observation):
    text = ["## Tokens per node", "",
             "| Node | " + " | ".join(USAGE_NAMES.values()) + " | total | assistant turns |",
             "| --- |" + " --- |" * (len(USAGE_NAMES) + 2)]
    for session in observation["sessions"]:
        text.append(f"| {session['node']} | " + " | ".join(thousands(session["usage"][field]) for field in USAGE_NAMES)
                     + f" | {thousands(session['token'])} | {session['assistant_turns']} |")
    text.append("| **total** | " + " | ".join(thousands(observation["usage"][field]) for field in USAGE_NAMES)
                 + f" | **{thousands(observation['token'])}** | "
                 + f"{sum(session['assistant_turns'] for session in observation['sessions'])} |")
    text += ["", "Source: transcript of each node, the four `usage` fields of the assistant messages, "
              "each `message.id` counted once.", ""]
    return text


def human_turns_section(observation, rows):
    text = ["## Human turns and native channel", "",
             "| Node | Human records | Human turns after launch | Native messages received | SendMessage | Received from the bus | Without a log row |",
             "| --- | --- | --- | --- | --- | --- | --- |"]
    delivered_ids = {row["id"] for row in rows if row["outcome"] == "delivered"}
    for session in observation["sessions"]:
        orphans = [message_id for message_id in session["received_from_bus"] if message_id not in delivered_ids]
        text.append(f"| {session['node']} | {session['human_records']} | {session['human_turns']} "
                     f"| {session['peer_messages']} | {session['native_send_messages']} "
                     f"| {len(session['received_from_bus'])} | {len(orphans)} |")
    text += ["", "Source: transcript — `user` records by `origin.kind` (`human`, `peer`), tool_use `SendMessage`, "
              "message ids in the `tool_result` of `bus recv`; log — `delivered` rows with those ids. "
              "The first human record is the launch prompt of the director.", ""]
    return text


def human_node(rows, match_dir, node="umano"):
    """The passive human node: the messages it gets, how many of them were received, how many stay
    in its mailbox, how many answers it sent. No process reads its mailbox, so in
    a match without human turns the received and the answers are 0 and every message stays in the mailbox."""
    mailbox = Path(match_dir) / "mailboxes" / node
    return {
        "messages": [row for row in rows if row["outcome"] == "delivered" and row["to"] == node],
        "received": sum(1 for row in rows if row["outcome"] == "received" and row["to"] == node),
        "answers": sum(1 for row in rows if row["outcome"] == "delivered" and row["from"] == node),
        "in_mailbox": len(list(mailbox.glob("[0-9]*.json"))) if mailbox.is_dir() else 0,
    }


def human_node_section(rows, match_dir, node="umano"):
    count = human_node(rows, match_dir, node)
    messages = count["messages"]
    text = ["## Messages to the human", ""]
    if messages:
        text += ["| # | From | ts | Text |", "| --- | --- | --- | --- |"]
        text += [f"| {index} | {row['from']} | {row['ts']} | {row['text'].replace('|', '¦')} |"
                  for index, row in enumerate(messages, start=1)]
        text.append("")
    synopsis = (f"Messages to the node `{node}`: **{len(messages)}** · received {count['received']} · still in the mailbox "
               f"{count['in_mailbox']} · answers from the human {count['answers']}.")
    if messages and not count["answers"]:
        synopsis += " All without an answer: the human node is passive."
    text += [synopsis, "",
              f"Source: log — `delivered` rows with `to` = `{node}`, `received` rows with `to` = `{node}`, `delivered` rows "
              f"with `from` = `{node}`; match directory, files in `mailboxes/{node}/` never taken.", ""]
    return text


def transcript_section(observation):
    text = ["## Transcripts", ""]
    for session in observation["sessions"]:
        text.append(f"- {session['node']}: `{str(session['transcript']).replace(str(Path.home()), '~')}`")
    text.append("")
    return text
