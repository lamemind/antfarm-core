"""Reading of the access graph: the data file that says who knows, reads, writes, modifies, spawns whom.

Module shared by view.py, bus and director.py. It does not validate: validate-graph.py checks the shape,
and the director runs it before it opens the match. Standard library only, because the bus imports it
on every message.

It also says where the two roots of the engine are. The code root is the engine directory:
`bin/`, `games/`, `schema/`, `view/`, `test/`. The state root is the directory of a project
that uses the engine: `matches/` and `registry/`. The two are distinct, because an installed engine
serves many projects and the state of a project never enters the history of the engine.
"""

import json
import os
import sys
from pathlib import Path

RIGHTS = ("know", "read", "write", "modify", "spawn")
# The environment variable that carries the state root. Required and without a default: an engine
# that guessed it would write to the wrong place in silence.
STATE_VARIABLE = "ANTFARM_STATE_DIR"
# The lock file next to graph.json: the bus takes it shared to read version and departure time
# together, the director takes it exclusive to write a new version.
GRAPH_LOCK = ".graph.lock"


def load(path):
    with open(path, encoding="utf-8") as graph_file:
        return json.load(graph_file)


def node_ids(graph):
    return [node["id"] for node in graph["nodes"]]


def node(graph, node_id):
    for candidate in graph["nodes"]:
        if candidate["id"] == node_id:
            return candidate
    return None


def rights(graph, from_node, to_node):
    """Set of the rights of the edge from → to; empty if the edge does not exist."""
    for edge in graph["edges"]:
        if edge["from"] == from_node and edge["to"] == to_node:
            return set(edge["rights"])
    return set()


def outgoing_edges(graph, from_node):
    return [edge for edge in graph["edges"] if edge["from"] == from_node]


def recipients(graph, from_node, right):
    """The nodes toward which `from_node` has `right`, in the order the file declares them."""
    return [edge["to"] for edge in outgoing_edges(graph, from_node) if right in edge["rights"]]


def delta(before, after):
    """What changes between two versions of the graph: nodes and edges added and removed, whole entries,
    in file order, plus the new version. An edge that changes its rights, or a node that changes a field,
    appears among the removed and among the added, so the delta loses nothing."""
    def node_key(entry):
        return json.dumps(entry, sort_keys=True, ensure_ascii=False)

    def edge_key(entry):
        return entry["from"], entry["to"], frozenset(entry["rights"])

    def difference(entries, others, key):
        present = {key(entry) for entry in others}
        return [entry for entry in entries if key(entry) not in present]

    return {
        "version": after["version"],
        "nodes_added": difference(after["nodes"], before["nodes"], node_key),
        "nodes_removed": difference(before["nodes"], after["nodes"], node_key),
        "edges_added": difference(after["edges"], before["edges"], edge_key),
        "edges_removed": difference(before["edges"], after["edges"], edge_key),
    }


def code_root():
    """The engine directory, derived from the path of this file: `bin/`, `games/`, `schema/`, `view/`."""
    return Path(__file__).resolve().parent.parent


def state_root():
    """The state directory, from `ANTFARM_STATE_DIR`, as an absolute path: `matches/` and `registry/`
    live under it and are created on first use.

    Exits with an error, code 2 and the path on stderr, if the variable is missing or empty, if the path
    is not an existing directory, or if it is the engine directory or lies inside it. A mistyped root
    is not created in silence: whoever writes the state creates the tree with `mkdir(parents=True)`.
    And the engine ignores `matches/` and `registry/` in git, so a write inside the engine would not
    show in `git status`."""
    value = os.environ.get(STATE_VARIABLE, "")
    if not value:
        exit_without_root(f"{STATE_VARIABLE} is not set: whoever uses the engine chooses the state root, "
                          f"for example `{STATE_VARIABLE}=. <engine>/bin/director.py …` from the project directory")
    root = Path(value).resolve()
    if not root.is_dir():
        exit_without_root(f"{STATE_VARIABLE}={value}: {root} is not an existing directory")
    code = code_root()
    if root == code or code in root.parents:
        exit_without_root(f"{STATE_VARIABLE}={value}: {root} is the engine directory or lies inside it ({code}); "
                          "the state of a project lives outside the engine directory")
    return root


def exit_without_root(reason):
    print(f"invalid state root — {reason}", file=sys.stderr)
    raise SystemExit(2)
