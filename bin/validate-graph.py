#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["jsonschema>=4.18"]
# ///
"""Gate of the access graph: exits 0 if the file is a valid graph, 1 if it is not.

Checks the shape against schema/graph.schema.json and the consistency the schema does not express:
unique node ids, both endpoints of every edge among the declared nodes, no edge from a node to
itself, no repeated (from, to) pair — the rights of a pair sit in a single edge.
For sources: the path is relative and does not leave the match directory — no absolute
path, no `..` —, because the blast radius of a match is its directory; and a
source has no outgoing edges, because it does not speak.

Usage:  antfarm-core/bin/validate-graph.py <graph.json>
Exits 2 on wrong usage or unreadable file.
"""

import json
import sys
from pathlib import Path, PurePosixPath

import jsonschema

SCHEMA = Path(__file__).resolve().parent.parent / "schema" / "graph.schema.json"


def shape_errors(graph):
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)
    for error in sorted(validator.iter_errors(graph), key=lambda e: list(e.absolute_path)):
        position = "/".join(str(part) for part in error.absolute_path) or "(root)"
        yield f"shape: {position}: {error.message}"


def path_errors(node):
    """The path of a source: relative and inside the match directory."""
    path = PurePosixPath(node["path"])
    if path.is_absolute():
        yield f"source {node['id']}: absolute path, it must be relative to the match directory: {path}"
    if ".." in path.parts:
        yield f"source {node['id']}: the path contains `..` and leaves the match directory: {path}"


def consistency_errors(graph):
    seen, sources = set(), set()
    for node in graph["nodes"]:
        if node["id"] in seen:
            yield f"repeated node: {node['id']}"
        seen.add(node["id"])
        if node["type"] == "source":
            sources.add(node["id"])
            yield from path_errors(node)
    pairs = set()
    for index, edge in enumerate(graph["edges"]):
        for endpoint in ("from", "to"):
            if edge[endpoint] not in seen:
                yield f"edges/{index}: '{endpoint}' points to a missing node: {edge[endpoint]}"
        if edge["from"] in sources:
            yield f"edges/{index}: outgoing edge from the source {edge['from']}: a source does not speak"
        if edge["from"] == edge["to"]:
            yield f"edges/{index}: edge from {edge['from']} to itself"
        pair = (edge["from"], edge["to"])
        if pair in pairs:
            yield f"edges/{index}: repeated pair {edge['from']} → {edge['to']}"
        pairs.add(pair)


def main():
    if len(sys.argv) != 2:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    try:
        graph = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        print(f"unreadable graph: {error}", file=sys.stderr)
        return 2
    errors = list(shape_errors(graph))
    if not errors:
        errors = list(consistency_errors(graph))
    for error in errors:
        print(error, file=sys.stderr)
    if errors:
        print(f"INVALID GRAPH: {len(errors)} errors — {sys.argv[1]}", file=sys.stderr)
        return 1
    print(f"valid graph: version {graph['version']}, {len(graph['nodes'])} nodes, {len(graph['edges'])} edges")
    return 0


if __name__ == "__main__":
    sys.exit(main())
