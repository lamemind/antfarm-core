#!/usr/bin/env python3
"""First falsifier of the orchestration custodian: do the games run without the human?

    antfarm-core/test/first-falsifier.py [--json]

For every match in the match registry it reads the transcripts of its LLM nodes, found through the
`sessionId` in nodes.json, and counts the `user` entries after the first one that are not a
`tool_result`. The first `user` entry is the director's launch message. The transcript format is read
here directly, without going through `bin/observe.py`: this measure checks the `human_turns` that the
engine declares in the registry, so it does not inherit its parser.

A match counts if it has `report.md`, every transcript is found and the foreign entries are 0. It prints
one line per match and the count of distinct games with at least one match that counts.
It reads the registry and the matches from the state root, ANTFARM_STATE_DIR.
Exits 0 if the distinct games are at least 3, 1 otherwise, 2 without ANTFARM_STATE_DIR.
"""

import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
import graph as libgraph  # noqa: E402

STATE_ROOT = libgraph.state_root()
REGISTRY = STATE_ROOT / "registry" / "matches.tsv"
MATCHES = STATE_ROOT / "matches"
CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"
GAMES_THRESHOLD = 3


def user_entries(path):
    entries = (json.loads(row) for row in path.read_text(encoding="utf-8").splitlines() if row.strip())
    return [entry for entry in entries if entry.get("type") == "user"]


def is_tool_result(entry):
    content = (entry.get("message") or {}).get("content")
    return isinstance(content, list) and bool(content) and all(isinstance(block, dict) and block.get("type") == "tool_result" for block in content)


def measure_match(row):
    directory = MATCHES / row["match"]
    nodes = json.loads((directory / "nodes.json").read_text(encoding="utf-8"))["nodes"]
    sessions = {node: entry["sessionId"] for node, entry in nodes.items() if entry.get("type") == "llm"}
    foreign_entries, missing, tool_result = 0, [], 0
    for node, session in sessions.items():
        found = sorted(CLAUDE_PROJECTS.glob(f"*/{session}.jsonl"))
        if not found:
            missing.append(node)
            continue
        after_launch = user_entries(found[0])[1:]
        foreign_entries += sum(not is_tool_result(entry) for entry in after_launch)
        tool_result += sum(is_tool_result(entry) for entry in after_launch)
    report = (directory / "report.md").is_file()
    return {"match": row["match"], "game": row["game"], "nodes": len(sessions), "tool_result": tool_result,
            "foreign_entries": foreign_entries, "missing_transcripts": missing, "report": report,
            "counts": report and not missing and foreign_entries == 0 and bool(sessions)}


def main():
    with open(REGISTRY, encoding="utf-8", newline="") as registry:
        measures = [measure_match(row) for row in csv.DictReader(registry, delimiter="\t")]
    games = sorted({measure["game"] for measure in measures if measure["counts"]})
    if "--json" in sys.argv:
        print(json.dumps({"matches": measures, "games": games, "threshold": GAMES_THRESHOLD}, ensure_ascii=False, indent=2))
    else:
        for measure in measures:
            print(f"{measure['match']} · {measure['game']} · nodes {measure['nodes']} · tool_result {measure['tool_result']} · "
                  f"foreign entries {measure['foreign_entries']} · missing transcripts {measure['missing_transcripts'] or 0} · "
                  f"report {'yes' if measure['report'] else 'no'} · {'counts' if measure['counts'] else 'does NOT count'}")
        print(f"distinct games with at least one match that counts: {len(games)} ({', '.join(games)}) · threshold {GAMES_THRESHOLD}")
    return 0 if len(games) >= GAMES_THRESHOLD else 1


if __name__ == "__main__":
    sys.exit(main())
