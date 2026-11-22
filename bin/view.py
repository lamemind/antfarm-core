#!/usr/bin/env python3
"""Per-agent view: from the access graph, the text a node receives, and nothing else.

It carries the graph version, the outgoing edges of the node with their rights and, for each right,
the nodes toward which the node has it. A source — a file, a node that does not speak — toward which the node
has `write` sits on a line of its own and not among the nodes to write to, because it has no mailbox: it is
written with `bus write`, not with `bus send`. Incoming edges are not there: knowing who writes to you
is not a right, and the whole graph reaches no agent.

Usage:  antfarm-core/bin/view.py <graph.json> <node> [--json]
Exits 1 if the node is not in the graph, 2 on wrong usage.
"""

import json
import sys

import graph as libgraph


def view(graph, node_id):
    edges = libgraph.outgoing_edges(graph, node_id)
    return {
        "node": node_id,
        "version": graph["version"],
        "edges": [{"to": edge["to"], "rights": edge["rights"]} for edge in edges],
        "rights": {right: libgraph.recipients(graph, node_id, right) for right in libgraph.RIGHTS},
        "sources": [edge["to"] for edge in edges if (libgraph.node(graph, edge["to"]) or {}).get("type") == "source"],
    }


def as_text(data):
    rows = [f"# Vista di {data['node']} · grafo versione {data['version']}", "", "Archi uscenti:"]
    if data["edges"]:
        rows += [f"- {data['node']} → {edge['to']}: {', '.join(edge['rights'])}" for edge in data["edges"]]
    else:
        rows.append("- (nessuno)")
    rows.append("")
    labels = {
        "know": "Conosci",
        "read": "Puoi leggere la traccia di",
        "write": "Puoi scrivere a",
        "modify": "Puoi modificare gli archi intorno a",
        "spawn": "Puoi spawnare da",
    }
    sources = data.get("sources", [])
    for right in libgraph.RIGHTS:
        nodes = data["rights"][right]
        if right == "write":
            nodes = [node for node in nodes if node not in sources]
        rows.append(f"{labels[right]}: {', '.join(nodes) if nodes else '(nessuno)'}")
    writable = [source for source in sources if source in data["rights"]["write"]]
    if writable:
        rows.append(f"Puoi scrivere i file (sorgenti, con bus write): {', '.join(writable)}")
    return "\n".join(rows)


def main():
    args = [arg for arg in sys.argv[1:] if arg != "--json"]
    if len(args) != 2:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    path, node_id = args
    graph = libgraph.load(path)
    if libgraph.node(graph, node_id) is None:
        print(f"node not in the graph: {node_id}", file=sys.stderr)
        return 1
    data = view(graph, node_id)
    print(json.dumps(data, ensure_ascii=False, indent=2) if "--json" in sys.argv else as_text(data))
    return 0


if __name__ == "__main__":
    sys.exit(main())
