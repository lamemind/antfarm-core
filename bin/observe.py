#!/usr/bin/env python3
"""Observability of a match: the three checks on each session launched by the director, human turns, tokens.

The three checks, for each LLM node of nodes.json:

1. transcript — the session file exists and carries at least one assistant turn
2. parent — nodes.json, written by the director at launch, carries parent (the director of the match,
   with its pid) and task, and the transcript carries the same sessionId: the link does not come from a
   declaration of the agent
3. token — every assistant message carries the four `usage` fields as integers, so the sum
   is computable

Human turns: `user` records with `origin.kind` = `human` after the first, which is the launch prompt.
Tokens: sum of input_tokens, output_tokens, cache_creation_input_tokens, cache_read_input_tokens
per assistant message, counting each `message.id` only once — Claude Code writes one
record per block (thinking, text, tool_use) and each one repeats the usage of the message.
Next to the checks: native messages received (`origin.kind` = `peer`) and calls to SendMessage.

Usage:  antfarm-core/bin/observe.py <match directory> [--json]
Exits 1 if a session fails a check, 2 on wrong usage.
"""

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import match as libmatch  # noqa: E402

USAGE_FIELDS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
MATCH_REGISTRY = libmatch.STATE_ROOT / "registry" / "matches.tsv"
REGISTRY_COLUMNS = ("match", "game", "outcome", "human_turns", "token", "cwd")


RECEIVED_PATTERN = re.compile(r"^messaggio (m-[0-9a-f]+) da \S+ · grafo v\d+$", re.MULTILINE)


def result_text(block):
    content = block.get("content")
    if isinstance(content, list):
        return "\n".join(part.get("text", "") for part in content if isinstance(part, dict))
    return content or ""


def read_records(path):
    """The records of a transcript, one per row. A row that does not decode is skipped: Claude Code
    appends to the transcript while a session runs, and a read can land on the last row half
    written. The next read finds it whole, and the tokens count each `message.id` once, so a row
    skipped and read again later does not count twice."""
    records = []
    with open(path, encoding="utf-8", errors="replace") as transcript:
        for row in transcript:
            if not row.strip():
                continue
            try:
                records.append(json.loads(row))
            except json.JSONDecodeError:
                continue
    return records


def assistant_messages(records):
    """message.id → message, for every assistant record that carries an id: a message split over
    several records counts once."""
    messages = {}
    for record in records:
        if record.get("type") == "assistant" and (record.get("message") or {}).get("id"):
            messages[record["message"]["id"]] = record["message"]
    return messages


def usage_of(messages):
    """The four usage fields summed over the messages."""
    return {field: sum((message.get("usage") or {}).get(field) or 0 for message in messages.values())
            for field in USAGE_FIELDS}


def transcript_tokens(path):
    """The tokens of the transcript at the path, as measure_session counts them, for whoever already
    has the path and would not look it up again."""
    return sum(usage_of(assistant_messages(read_records(path))).values())


def measure_session(node_id, node_data, director_pid):
    path = libmatch.transcript(node_data["sessionId"])
    measure = {"node": node_id, "sessionId": node_data["sessionId"], "transcript": str(path) if path else None}
    records = read_records(path) if path else []

    messages = assistant_messages(records)
    measure["assistant_turns"] = len(messages)

    parent = node_data.get("parent") or {}
    session_id_in_records = {record["sessionId"] for record in records if record.get("sessionId")}
    measure["parent"] = parent
    measure["task"] = node_data.get("task")

    valid_usage = all(
        all(isinstance((message.get("usage") or {}).get(field), int) for field in USAGE_FIELDS)
        for message in messages.values()
    )
    measure["usage"] = usage_of(messages)
    measure["token"] = sum(measure["usage"].values())

    users = [record for record in records if record.get("type") == "user"]
    humans = [record for record in users if (record.get("origin") or {}).get("kind") == "human"]
    measure["human_turns"] = max(0, len(humans) - 1)
    measure["human_records"] = len(humans)
    measure["peer_messages"] = sum(1 for record in users if (record.get("origin") or {}).get("kind") == "peer")
    uses = [block for record in records if record.get("type") == "assistant"
           for block in (record.get("message") or {}).get("content") or [] if block.get("type") == "tool_use"]
    measure["received_from_bus"] = [
        hit.group(1)
        for record in users
        for block in ((record.get("message") or {}).get("content") or [] if isinstance((record.get("message") or {}).get("content"), list) else [])
        if block.get("type") == "tool_result"
        for hit in [RECEIVED_PATTERN.search(result_text(block))] if hit
    ]
    measure["tools_used"] = sorted({block["name"] for block in uses})
    measure["native_send_messages"] = sum(1 for block in uses if block["name"] == "SendMessage")
    measure["models"] = sorted({message.get("model") for message in messages.values() if message.get("model")})

    measure["checks"] = {
        "transcript": path is not None and len(messages) >= 1,
        "parent": (parent.get("node") == "director" and parent.get("pid") == director_pid
                     and bool(node_data.get("task")) and session_id_in_records == {node_data["sessionId"]}),
        "token": path is not None and len(messages) >= 1 and valid_usage,
    }
    return measure


def observe(match_dir):
    match = libmatch.Match(match_dir)
    nodes = match.nodes()["nodes"]
    director_pid = nodes.get("director", {}).get("pid")
    sessions = [measure_session(node_id, data, director_pid) for node_id, data in nodes.items() if data.get("type") == "llm"]
    return {
        "match": match.id,
        "sessions": sessions,
        "human_turns": sum(session["human_turns"] for session in sessions),
        "token": sum(session["token"] for session in sessions),
        "usage": {field: sum(session["usage"][field] for session in sessions) for field in USAGE_FIELDS},
        "peer_messages": sum(session["peer_messages"] for session in sessions),
        "native_send_messages": sum(session["native_send_messages"] for session in sessions),
        "checks_ok": bool(sessions) and all(all(session["checks"].values()) for session in sessions),
    }


def register_match(row):
    """Appends a row to the match registry, with the header if the file does not exist."""
    MATCH_REGISTRY.parent.mkdir(parents=True, exist_ok=True)
    new = not MATCH_REGISTRY.exists()
    with open(MATCH_REGISTRY, "a", encoding="utf-8") as registry:
        if new:
            registry.write("\t".join(REGISTRY_COLUMNS) + "\n")
        registry.write("\t".join(str(row[column]) for column in REGISTRY_COLUMNS) + "\n")


def as_text(observation):
    rows = [f"match {observation['match']}"]
    for session in observation["sessions"]:
        checks = " · ".join(f"{name} {'ok' if ok else 'FAIL'}" for name, ok in session["checks"].items())
        rows.append(f"  {session['node']}: {checks} · human turns {session['human_turns']}"
                     f" · tokens {session['token']} · peer {session['peer_messages']}"
                     f" · SendMessage {session['native_send_messages']} · tools {','.join(session['tools_used'])}")
    rows.append(f"  total: human turns {observation['human_turns']} · tokens {observation['token']}"
                 f" · checks {'ok' if observation['checks_ok'] else 'FAILED'}")
    return "\n".join(rows)


def main():
    args = [arg for arg in sys.argv[1:] if arg != "--json"]
    if len(args) != 1 or not (Path(args[0]) / "nodes.json").is_file():
        print(__doc__.strip(), file=sys.stderr)
        return 2
    observation = observe(Path(args[0]))
    print(json.dumps(observation, ensure_ascii=False, indent=2) if "--json" in sys.argv else as_text(observation))
    return 0 if observation["checks_ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
