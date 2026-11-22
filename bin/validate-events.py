#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["jsonschema>=4.18"]
# ///
"""Gate of the harness events: exits 0 if every line of the file is a valid event, 1 if one is not.

Checks every event against schema/event.schema.json — which refers to schema/graph.schema.json
for nodes, edges and graph — and against the consistency between envelope and row that the schema
does not express: same match, same outcome, same ts in the envelope and in the source row; for the
spawn, the child among the nodes of the delta and an edge from the parent to the child among the
edges; for graph-changed, a delta consistent with the graph of the new version. On the whole stream:
no message emitted twice, that is no repeated (match, index) pair.

Usage:  antfarm-core/bin/validate-events.py <events.jsonl>     one JSON event per line, as the harness emits it
Exits 2 on wrong usage or unreadable file.
"""

import json
import sys
from pathlib import Path

import jsonschema
from referencing import Registry, Resource

SCHEMA_DIR = Path(__file__).resolve().parent.parent / "schema"
EVENT_SCHEMA = SCHEMA_DIR / "event.schema.json"
GRAPH_SCHEMA = SCHEMA_DIR / "graph.schema.json"


def validator():
    event_schema = json.loads(EVENT_SCHEMA.read_text(encoding="utf-8"))
    graph_schema = json.loads(GRAPH_SCHEMA.read_text(encoding="utf-8"))
    registry = Registry().with_resources([
        (event_schema["$id"], Resource.from_contents(event_schema)),
        (graph_schema["$id"], Resource.from_contents(graph_schema)),
    ])
    return jsonschema.Draft202012Validator(event_schema, registry=registry)


def shape_errors(events_validator, event):
    for error in sorted(events_validator.iter_errors(event), key=lambda e: list(e.absolute_path)):
        position = "/".join(str(part) for part in error.absolute_path) or "(root)"
        yield f"shape: {position}: {error.message}"


def graph_changed_errors(event):
    """The delta against the file of the new version: same version, same time, every added entry
    present in the graph, every removed entry absent — except those removed and added again."""
    row, delta = event["row"], event["delta"]
    graph = row["graph"]
    if row["effective"] != event["ts"]:
        yield f"envelope ts {event['ts']} ≠ row effective {row['effective']}"
    if graph["version"] != delta["version"]:
        yield f"delta version {delta['version']} ≠ graph version {graph['version']}"
    node_ids = {node["id"] for node in graph["nodes"]}
    pairs = {(edge["from"], edge["to"]) for edge in graph["edges"]}
    for node in delta["nodes_added"]:
        if node["id"] not in node_ids:
            yield f"the added node {node['id']} is not in the graph"
    for edge in delta["edges_added"]:
        if (edge["from"], edge["to"]) not in pairs:
            yield f"the added edge {edge['from']} → {edge['to']} is not in the graph"
    readded_nodes = {node["id"] for node in delta["nodes_added"]}
    for node in delta["nodes_removed"]:
        if node["id"] in node_ids and node["id"] not in readded_nodes:
            yield f"the removed node {node['id']} is still in the graph"
    readded_edges = {(edge["from"], edge["to"]) for edge in delta["edges_added"]}
    for edge in delta["edges_removed"]:
        pair = (edge["from"], edge["to"])
        if pair in pairs and pair not in readded_edges:
            yield f"the removed edge {edge['from']} → {edge['to']} is still in the graph"


def consistency_errors(event):
    row = event["row"]
    event_class, kind = event["class"], event["type"]
    if event_class == "message":
        if row["match"] != event["match"]:
            yield f"envelope match {event['match']} ≠ row match {row['match']}"
        if row["outcome"] != kind:
            yield f"type {kind} ≠ row outcome {row['outcome']}"
        if row["ts"] != event["ts"]:
            yield f"envelope ts {event['ts']} ≠ row ts {row['ts']}"
    elif kind == "match-opened":
        if row["meta"]["match"] != event["match"]:
            yield f"envelope match {event['match']} ≠ meta.json match {row['meta']['match']}"
        if row["meta"]["start"] != event["ts"]:
            yield f"envelope ts {event['ts']} ≠ meta.json start {row['meta']['start']}"
    elif kind == "match-closed":
        if row["match"] != event["match"]:
            yield f"envelope match {event['match']} ≠ meta.json match {row['match']}"
        if row["end"] != event["ts"]:
            yield f"envelope ts {event['ts']} ≠ meta.json end {row['end']}"
    elif kind in ("node-launched", "node-closed"):
        field = "launched" if kind == "node-launched" else "closed"
        if row.get(field) != event["ts"]:
            yield f"envelope ts {event['ts']} ≠ {field} of the node entry {row.get(field)}"
    elif kind == "graph-changed":
        yield from graph_changed_errors(event)
    elif event_class == "spawn":
        child, parent, delta = event["child"]["id"], event["parent"], event["delta"]
        if child not in [node["id"] for node in delta["nodes"]]:
            yield f"the child {child} is not among the nodes of the delta"
        if not any(edge["from"] == parent and edge["to"] == child for edge in delta["edges"]):
            yield f"no edge {parent} → {child} among the edges of the delta"


def main():
    if len(sys.argv) != 2:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    try:
        # Split on newline only: a text can carry Unicode line separators, which
        # splitlines() would take for newlines, breaking the event apart.
        rows = Path(sys.argv[1]).read_text(encoding="utf-8").split("\n")
    except OSError as error:
        print(f"unreadable file: {error}", file=sys.stderr)
        return 2
    events_validator = validator()
    errors, count, seen_messages = [], {}, set()
    for number, text in enumerate(rows, start=1):
        if not text.strip():
            continue
        try:
            event = json.loads(text)
        except json.JSONDecodeError as error:
            errors.append(f"line {number}: unreadable JSON: {error}")
            continue
        event_errors = list(shape_errors(events_validator, event))
        if not event_errors:
            event_errors = list(consistency_errors(event))
        if not event_errors and event["class"] == "message":
            key = (event["match"], event["index"])
            if key in seen_messages:
                event_errors.append(f"message emitted twice: match {key[0]}, index {key[1]}")
            seen_messages.add(key)
        errors += [f"line {number}: {error}" for error in event_errors]
        if not event_errors:
            class_type = f"{event['class']}/{event['type']}"
            count[class_type] = count.get(class_type, 0) + 1
    if not errors and not count:
        errors.append("no events in the file: an empty stream does not pass the gate")
    for error in errors:
        print(error, file=sys.stderr)
    if errors:
        print(f"INVALID EVENTS: {len(errors)} errors — {sys.argv[1]}", file=sys.stderr)
        return 1
    total = sum(count.values())
    print(f"valid events: {total} · " + " · ".join(f"{key} {number}" for key, number in sorted(count.items())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
