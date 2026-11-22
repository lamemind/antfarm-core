#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["playwright==1.60.0"]
# ///
"""Acceptance test of the view from the DOM, with headless Chromium driven by Playwright. No LLM.

    antfarm-core/test/acceptance-view.py [--screenshots <directory>]

Starts the harness in replay, opens the page, waits for the channel to close and the runs to end,
then counts and measures from the DOM — never from a counter of the page:

- control match (telefono-copia): as many [data-node] and [data-edge] elements as the nodes and
  edges of graph.json; one [data-dot] per delivered message of the log, each one running on the
  from → to edge of its event and stopped on the recipient node; the click on each dot opens the
  detail with id and text equal, character by character, to the row of the message log
- bus negative test: the edge-denied rejection A → D is a mark on node A, with no run, and the
  drawing has no edge A → D
- synthetic spawn (test/streams/spawn.jsonl, harness in stream mode): the child appears among the
  nodes, the delta edges among the edges — parent → child included —, the header carries the
  delta version, and the messages from and to the child run on the edges born from the spawn
- synthetic graph change (test/streams/graph-changed.jsonl, harness in stream mode): after the event
  the nodes are those of the new graph, the removed edge is gone, the new edges are there, the
  header carries the new version; the dots that already ran on the removed edge stay stopped on
  their recipient, and those after the change run on the new edges
- exquisite corpse (replay of the dry-run match and of the LLM one): the graph that changes while
  the match is open. The DOM has the 5 nodes of version 2, no edge C → A, one edge C → D and one
  D → A, every dot stopped on its recipient
- button dry run (boom scenario): the human node and the source appear with their types; a
  `written` row of the log is a [data-outcome=written] dot that runs on the edge A → bottone and
  stops on the source, and the click carries the text of the row
- off-bus actions (telefono-riformula of 27 October): the click on a node carries the list of
  actions in the node-closed event; the command of B is equal, character by character, to the one
  in its transcript, read here without going through the engine code

The reference matches, whose ids are written below, and the message log are read from the state
root of whoever launches, ANTFARM_STATE_DIR, and the harness it starts inherits it: the
acceptance test passes only where that state exists.

Playwright is pinned to the version whose Chromium is already in ~/.cache/ms-playwright. With
--screenshots it saves one screenshot per scenario. Exits 1 if a check fails, 2 without
ANTFARM_STATE_DIR.
"""

import argparse
import json
import math
import re
import subprocess
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

ANTFARM = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ANTFARM / "bin"))
import graph as libgraph  # noqa: E402

STATE_ROOT = libgraph.state_root()
HARNESS = ANTFARM / "bin" / "harness.py"
MESSAGE_LOG = STATE_ROOT / "registry" / "messages.jsonl"
CONTROL_MATCH = "2026-10-26-121427-telefono-copia"
NEGATIVE_TEST_MATCH = "2026-10-26-120840-contro-prova-bus"
SOURCE_MATCH = "2026-11-08-001923-bottone-a-secco-boom"
CORPSE_DRY_RUN_MATCH = "2026-11-08-114717-cadavere-a-secco"
CORPSE_LLM_MATCH = "2026-11-08-115230-cadavere"
SPAWN_STREAM = ANTFARM / "test" / "streams" / "spawn.jsonl"
GRAPH_CHANGED_STREAM = ANTFARM / "test" / "streams" / "graph-changed.jsonl"
ACTIONS_MATCH = "2026-10-27-115351-telefono-riformula"
CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"
REPLAY_SPEED = "20"
WAIT_MS = 60000

failed = 0


def verify(description, outcome, evidence=""):
    global failed
    if not outcome:
        failed += 1
    print(f"  {'ok  ' if outcome else 'FAIL'} {description}" + (f" — {evidence}" if evidence and not outcome else ""))


def split_log_rows():
    """Row number from 1 → log row. Splits on newline only, like the harness."""
    content = MESSAGE_LOG.read_bytes().split(b"\n")
    return {index: json.loads(block) for index, block in enumerate(content, start=1) if block.strip()}


class Harness:
    """The harness running for the length of a scenario, on a port chosen by the system."""

    def __init__(self, *args):
        self.args = [str(HARNESS), *args, "--port", "0"]

    def __enter__(self):
        self.process = subprocess.Popen(self.args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        first_line = self.process.stdout.readline()
        hit = re.search(r"http://127\.0\.0\.1:(\d+)/", first_line)
        if not hit:
            self.process.kill()
            raise SystemExit(f"the harness does not start: {first_line}{self.process.stderr.read()}")
        self.address = hit.group(0)
        return self

    def __exit__(self, *_):
        self.process.terminate()
        self.process.wait(timeout=10)


def open_and_wait(page, address):
    page.goto(address)
    page.wait_for_selector('body[data-status="finished"]', state="attached", timeout=WAIT_MS)
    page.wait_for_function(
        "() => [...document.querySelectorAll('[data-dot][data-outcome=delivered], [data-dot][data-outcome=written]')]"
        ".every(p => p.hasAttribute('data-arrived'))",
        timeout=WAIT_MS,
    )


def measure_dom(page):
    return page.evaluate("""() => {
        const center = (element) => { const r = element.getBoundingClientRect(); return { x: r.x + r.width / 2, y: r.y + r.height / 2, radius: r.width / 2 }; };
        const nodes = Object.fromEntries([...document.querySelectorAll('[data-node]')].map(g => [g.dataset.node, { ...center(g.querySelector('circle')), classes: g.getAttribute('class') }]));
        const edges = Object.fromEntries([...document.querySelectorAll('[data-edge]')].map(g => [g.dataset.edge, g.querySelector('path.edge').id]));
        const dots = [...document.querySelectorAll('[data-dot]')].map(p => ({
            key: p.dataset.dot,
            outcome: p.dataset.outcome,
            event: JSON.parse(p.dataset.event),
            run: p.querySelector('animateMotion mpath')?.getAttribute('href') ?? null,
            position: center(p),
        }));
        return { nodes, edges, dots, version: document.getElementById('version').textContent };
    }""")


def nearest_node(nodes, point):
    return min(nodes, key=lambda node_id: math.dist((nodes[node_id]["x"], nodes[node_id]["y"]), (point["x"], point["y"])))


def detail_after_click(page, key):
    page.locator(f'[data-dot="{key}"]').click()
    return json.loads(page.locator("#detail-json").text_content())


def scenario_control(page, screenshots, log):
    print(f"control match {CONTROL_MATCH}")
    graph = json.loads((STATE_ROOT / "matches" / CONTROL_MATCH / "graph.json").read_text(encoding="utf-8"))
    delivered = {index: row for index, row in log.items()
                 if row.get("match") == CONTROL_MATCH and row["outcome"] == "delivered"}
    with Harness("replay", CONTROL_MATCH, "--speed", REPLAY_SPEED) as harness:
        open_and_wait(page, harness.address)
        dom = measure_dom(page)
        verify(f"drawn nodes = nodes of graph.json ({len(graph['nodes'])})", len(dom["nodes"]) == len(graph["nodes"]),
               f"{len(dom['nodes'])} in the DOM")
        verify(f"drawn edges = edges of graph.json ({len(graph['edges'])})", len(dom["edges"]) == len(graph["edges"]),
               f"{len(dom['edges'])} in the DOM")
        verify("every edge of the file has its element", sorted(dom["edges"]) == sorted(f"{edge['from']}→{edge['to']}" for edge in graph["edges"]))
        verify(f"one dot per delivered message ({len(delivered)})",
               len(dom["dots"]) == len(delivered) and all(p["outcome"] == "delivered" for p in dom["dots"]),
               f"{len(dom['dots'])} dots in the DOM")
        expected_keys = sorted(f"{CONTROL_MATCH}:{index}" for index in delivered)
        verify("the dot keys are match + index of the delivered rows", sorted(p["key"] for p in dom["dots"]) == expected_keys)
        for dot in dom["dots"]:
            row = dot["event"]["row"]
            expected_edge = f"{row['from']}→{row['to']}"
            on_its_edge = dot["run"] is not None and dot["run"] == f"#{dom['edges'].get(expected_edge)}"
            stopped_at = nearest_node(dom["nodes"], dot["position"])
            destination = dom["nodes"][row["to"]]
            distance = math.dist((destination["x"], destination["y"]), (dot["position"]["x"], dot["position"]["y"]))
            verify(f"dot {dot['key']}: runs on edge {expected_edge}, stopped on {row['to']}",
                   on_its_edge and stopped_at == row["to"] and distance <= destination["radius"] * 1.4,
                   f"run {dot['run']}, nearest node {stopped_at}, distance {distance:.0f}px")
        for dot in dom["dots"]:
            index = dot["event"]["index"]
            detail = detail_after_click(page, dot["key"])
            verify(f"click on {dot['key']}: id and text equal to row {index} of the log",
                   detail["row"]["id"] == log[index]["id"] and detail["row"]["text"] == log[index]["text"],
                   f"id {detail['row']['id']} vs {log[index]['id']}")
        if screenshots:
            page.screenshot(path=str(screenshots / "view-telephone-copy.png"))


def scenario_rejection(page, screenshots, log):
    print(f"bus negative test {NEGATIVE_TEST_MATCH}")
    rows = {index: row for index, row in log.items() if row.get("match") == NEGATIVE_TEST_MATCH}
    rejections = {index: row for index, row in rows.items() if row["outcome"] == "rejected"}
    delivered = {index: row for index, row in rows.items() if row["outcome"] == "delivered"}
    with Harness("replay", NEGATIVE_TEST_MATCH, "--speed", REPLAY_SPEED) as harness:
        open_and_wait(page, harness.address)
        dom = measure_dom(page)
        verify("no edge A → D in the drawing", "A→D" not in dom["edges"], f"edges: {sorted(dom['edges'])}")
        verify(f"one mark per rejection and one dot per delivered message ({len(rejections)} + {len(delivered)})",
               sum(p["outcome"] == "rejected" for p in dom["dots"]) == len(rejections)
               and sum(p["outcome"] == "delivered" for p in dom["dots"]) == len(delivered))
        for mark in (p for p in dom["dots"] if p["outcome"] == "rejected"):
            row = mark["event"]["row"]
            stopped_at = nearest_node(dom["nodes"], mark["position"])
            verify(f"rejection {row['reason']} {row['from']} → {row['to']}: no run, stopped on sender {row['from']}",
                   mark["run"] is None and stopped_at == row["from"], f"run {mark['run']}, nearest node {stopped_at}")
            detail = detail_after_click(page, mark["key"])
            index = mark["event"]["index"]
            verify(f"click on the rejection: reason and text equal to row {index} of the log",
                   detail["row"]["reason"] == log[index]["reason"] and detail["row"]["text"] == log[index]["text"])
        if screenshots:
            page.screenshot(path=str(screenshots / "view-negative-test-bus.png"))


def scenario_spawn(page, screenshots, _log):
    print(f"synthetic spawn {SPAWN_STREAM.relative_to(ANTFARM)}")
    events = [json.loads(text) for text in SPAWN_STREAM.read_text(encoding="utf-8").split("\n") if text.strip()]
    graph = next(event for event in events if event["type"] == "match-opened")["row"]["graph"]
    spawn = next(event for event in events if event["class"] == "spawn")
    child, parent = spawn["child"]["id"], spawn["parent"]
    expected_edges = sorted({f"{edge['from']}→{edge['to']}" for edge in graph["edges"] + spawn["delta"]["edges"]})
    delivered_to_child = [event for event in events if event["type"] == "delivered" and child in (event["row"]["from"], event["row"]["to"])]
    with Harness("stream", SPAWN_STREAM, "--speed", REPLAY_SPEED) as harness:
        open_and_wait(page, harness.address)
        dom = measure_dom(page)
        verify(f"the child {child} appears among the drawn nodes", child in dom["nodes"], f"nodes: {sorted(dom['nodes'])}")
        verify(f"drawn nodes = nodes of graph v{graph['version']} + nodes of the delta",
               len(dom["nodes"]) == len(graph["nodes"]) + len(spawn["delta"]["nodes"]), f"{len(dom['nodes'])} in the DOM")
        verify(f"the edge {parent} → {child} appears in the drawing", f"{parent}→{child}" in dom["edges"])
        verify("drawn edges = edges of the graph + edges of the delta", sorted(dom["edges"]) == expected_edges,
               f"{sorted(dom['edges'])}")
        verify(f"the header carries the delta version (v{spawn['delta']['version']})",
               dom["version"].startswith(f"v{spawn['delta']['version']} "), dom["version"])
        for event in delivered_to_child:
            row = event["row"]
            dot = next((p for p in dom["dots"] if p["key"] == f"{event['match']}:{event['index']}"), None)
            runs = dot is not None and dot["run"] == f"#{dom['edges'].get(row['from'] + '→' + row['to'])}"
            stopped_at = nearest_node(dom["nodes"], dot["position"]) if dot else None
            verify(f"the message {row['from']} → {row['to']} runs on the edge born from the spawn and stops on {row['to']}",
                   runs and stopped_at == row["to"], f"dot {dot and dot['run']}, nearest node {stopped_at}")
        if screenshots:
            page.locator(f'[data-node="{child}"]').click()
            page.screenshot(path=str(screenshots / "view-synthetic-spawn.png"))


def verify_graph_changed(dom, new_graph, delta, delivered):
    """The DOM after a graph-changed: nodes and edges of the new version, every dot stopped on its
    recipient. A dot on the removed edge no longer has its run; the others run on their own edge."""
    removed = {f"{edge['from']}→{edge['to']}" for edge in delta["edges_removed"]}
    verify(f"[data-node] = nodes of the new version ({len(new_graph['nodes'])})",
           sorted(dom["nodes"]) == sorted(node["id"] for node in new_graph["nodes"]), f"{sorted(dom['nodes'])}")
    verify("[data-edge] = edges of the new version",
           sorted(dom["edges"]) == sorted(f"{edge['from']}→{edge['to']}" for edge in new_graph["edges"]), f"{sorted(dom['edges'])}")
    for edge in delta["edges_removed"]:
        verify(f"no [data-edge] for {edge['from']} → {edge['to']}", f"{edge['from']}→{edge['to']}" not in dom["edges"])
    for edge in delta["edges_added"]:
        verify(f"one [data-edge] for {edge['from']} → {edge['to']}", f"{edge['from']}→{edge['to']}" in dom["edges"])
    verify(f"the header carries version {new_graph['version']}", dom["version"].startswith(f"v{new_graph['version']} "), dom["version"])
    dots = [p for p in dom["dots"] if p["outcome"] == "delivered"]
    verify(f"one dot per delivered message ({len(delivered)})", len(dots) == len(delivered), f"{len(dots)} in the DOM")
    for dot in dots:
        row = dot["event"]["row"]
        key = f"{row['from']}→{row['to']}"
        stopped_at = nearest_node(dom["nodes"], dot["position"])
        destination = dom["nodes"][row["to"]]
        distance = math.dist((destination["x"], destination["y"]), (dot["position"]["x"], dot["position"]["y"]))
        right_run = dot["run"] is None if key in removed else dot["run"] == f"#{dom['edges'].get(key)}"
        verify(f"dot {dot['key']} ({key}, v{row['version']}): stopped on {row['to']}",
               right_run and stopped_at == row["to"] and distance <= destination["radius"] * 1.4,
               f"run {dot['run']}, nearest node {stopped_at}, distance {distance:.0f}px")


def scenario_graph_changed(page, screenshots, _log):
    print(f"synthetic graph change {GRAPH_CHANGED_STREAM.relative_to(ANTFARM)}")
    events = [json.loads(text) for text in GRAPH_CHANGED_STREAM.read_text(encoding="utf-8").split("\n") if text.strip()]
    change = next(event for event in events if event["type"] == "graph-changed")
    delivered = [event for event in events if event["type"] == "delivered"]
    with Harness("stream", GRAPH_CHANGED_STREAM, "--speed", REPLAY_SPEED) as harness:
        open_and_wait(page, harness.address)
        dom = measure_dom(page)
        verify_graph_changed(dom, change["row"]["graph"], change["delta"], delivered)
        rejections = [p for p in dom["dots"] if p["outcome"] == "rejected"]
        verify("the rejection after the change is a mark stopped on the sender C",
               len(rejections) == 1 and rejections[0]["run"] is None and nearest_node(dom["nodes"], rejections[0]["position"]) == "C",
               f"{[(p['key'], p['run']) for p in rejections]}")
        if screenshots:
            page.screenshot(path=str(screenshots / "view-graph-changed.png"))


def scenario_corpse(page, screenshots, log, match):
    print(f"exquisite corpse {match}")
    directory = STATE_ROOT / "matches" / match
    v1 = json.loads((directory / "graphs" / "v1.json").read_text(encoding="utf-8"))["graph"]
    v2 = json.loads((directory / "graphs" / "v2.json").read_text(encoding="utf-8"))["graph"]
    pairs = lambda graph: {(edge["from"], edge["to"]) for edge in graph["edges"]}
    delta = {"edges_removed": [edge for edge in v1["edges"] if (edge["from"], edge["to"]) not in pairs(v2)],
             "edges_added": [edge for edge in v2["edges"] if (edge["from"], edge["to"]) not in pairs(v1)]}
    delivered = [row for row in log.values() if row.get("match") == match and row["outcome"] == "delivered"]
    with Harness("replay", match, "--speed", REPLAY_SPEED) as harness:
        open_and_wait(page, harness.address)
        dom = measure_dom(page)
        verify("[data-node] = 5", len(dom["nodes"]) == 5, f"{sorted(dom['nodes'])}")
        verify("no [data-edge] for C → A", "C→A" not in dom["edges"])
        verify("one [data-edge] for C → D", "C→D" in dom["edges"])
        verify("one [data-edge] for D → A", "D→A" in dom["edges"])
        verify_graph_changed(dom, v2, delta, delivered)
        if screenshots:
            page.screenshot(path=str(screenshots / f"view-{match}.png"))


def scenario_corpse_dry_run(page, screenshots, log):
    scenario_corpse(page, screenshots, log, CORPSE_DRY_RUN_MATCH)


def scenario_corpse_llm(page, screenshots, log):
    scenario_corpse(page, screenshots, log, CORPSE_LLM_MATCH)


def scenario_source(page, screenshots, log):
    print(f"button dry run {SOURCE_MATCH}")
    graph = json.loads((STATE_ROOT / "matches" / SOURCE_MATCH / "graph.json").read_text(encoding="utf-8"))
    written_rows = {index: row for index, row in log.items()
                    if row.get("match") == SOURCE_MATCH and row["outcome"] == "written"}
    with Harness("replay", SOURCE_MATCH, "--speed", REPLAY_SPEED) as harness:
        open_and_wait(page, harness.address)
        dom = measure_dom(page)
        verify(f"drawn nodes = nodes of graph.json ({len(graph['nodes'])})", len(dom["nodes"]) == len(graph["nodes"]))
        for entry in graph["nodes"]:
            if entry["type"] in ("human", "source"):
                verify(f"node {entry['id']} carries the class type-{entry['type']}",
                       f"type-{entry['type']}" in (dom["nodes"].get(entry["id"], {}).get("classes") or ""))
        written_dots = [p for p in dom["dots"] if p["outcome"] == "written"]
        verify(f"one written dot per written row of the log ({len(written_rows)})",
               len(written_rows) >= 1 and sorted(p["key"] for p in written_dots) == sorted(f"{SOURCE_MATCH}:{i}" for i in written_rows),
               f"{len(written_dots)} written dots in the DOM")
        for dot in written_dots:
            row = dot["event"]["row"]
            edge = f"{row['from']}→{row['to']}"
            stopped_at = nearest_node(dom["nodes"], dot["position"])
            verify(f"written dot {dot['key']}: runs on edge {edge}, stopped on {row['to']}",
                   dot["run"] == f"#{dom['edges'].get(edge)}" and stopped_at == row["to"],
                   f"run {dot['run']}, nearest node {stopped_at}")
            detail = detail_after_click(page, dot["key"])
            index = dot["event"]["index"]
            verify(f"click on the written dot: text equal to row {index} of the log",
                   detail["row"]["text"] == log[index]["text"] and detail["row"]["outcome"] == "written")
        if screenshots:
            page.screenshot(path=str(screenshots / "view-button-dry-run.png"))


def launch_bus(node_entry):
    """The path of the bus the node played with, from the `Bash(<bus> *)` rule of --allowedTools
    in the launch command written in nodes.json. A match played by an engine that lived in
    another directory carries in the transcript the bus of that time, not the bus of the current engine."""
    command = node_entry["command"]
    return command[command.index("--allowedTools") + 1].removeprefix("Bash(").removesuffix(" *)")


def off_bus_commands_from_transcript(session_id, bus):
    """The Bash commands that are not bus commands, read directly from the session transcript:
    the oracle of the acceptance test does not go through the parser of the engine under test."""
    found = sorted(CLAUDE_PROJECTS.glob(f"*/{session_id}.jsonl"))
    if not found:
        return None
    commands = []
    for row in found[0].read_text(encoding="utf-8").splitlines():
        record = json.loads(row)
        content = (record.get("message") or {}).get("content")
        if record.get("type") != "assistant" or not isinstance(content, list):
            continue
        for block in content:
            if block.get("type") == "tool_use" and block["name"] == "Bash":
                command = block["input"]["command"]
                if not command.lstrip().startswith(bus):
                    commands.append(command)
    return commands


def actions_after_click(page, node_id):
    """The click on the node, then the `actions` list of its node-closed event as the panel shows it."""
    page.locator(f'[data-node="{node_id}"] circle').click()
    detail = json.loads(page.locator("#detail-json").text_content())
    title = page.locator("#detail-title").text_content()
    closures = [event for event in detail["events"] if event["type"] == "node-closed"]
    return title, closures[0]["row"]["actions"] if len(closures) == 1 else None


def scenario_actions(page, screenshots, _log):
    print(f"off-bus actions {ACTIONS_MATCH}")
    nodes = json.loads((STATE_ROOT / "matches" / ACTIONS_MATCH / "nodes.json").read_text(encoding="utf-8"))["nodes"]
    with Harness("replay", ACTIONS_MATCH, "--speed", REPLAY_SPEED) as harness:
        open_and_wait(page, harness.address)
        page.wait_for_selector('[data-node="D"].closed', state="attached", timeout=WAIT_MS)
        title, actions_b = actions_after_click(page, "B")
        verify("the click on B opens the detail of node B", title == "node · B", title)
        commands_b = off_bus_commands_from_transcript(nodes["B"]["sessionId"], launch_bus(nodes["B"]))
        verify("the transcript of B is readable and carries one off-bus command", commands_b is not None and len(commands_b) == 1, f"{commands_b}")
        verify("click on B: one action, error false", actions_b is not None and len(actions_b) == 1 and actions_b[0]["error"] is False, f"{actions_b}")
        if commands_b and actions_b:
            verify("click on B: command equal, character by character, to the transcript", actions_b[0]["command"] == commands_b[0],
                   f"{len(actions_b[0]['command'])} vs {len(commands_b[0])} characters")
        _, actions_a = actions_after_click(page, "A")
        verify("click on A: one action, error true", actions_a is not None and len(actions_a) == 1 and actions_a[0]["error"] is True, f"{actions_a}")
        _, actions_c = actions_after_click(page, "C")
        verify("click on C: empty list", actions_c == [], f"{actions_c}")
        if screenshots:
            actions_after_click(page, "B")
            page.screenshot(path=str(screenshots / "view-off-bus-actions.png"))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--screenshots", type=Path)
    args = parser.parse_args()
    if args.screenshots:
        args.screenshots.mkdir(parents=True, exist_ok=True)
    log = split_log_rows()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        for scenario in (scenario_control, scenario_rejection, scenario_spawn, scenario_graph_changed, scenario_corpse_dry_run, scenario_corpse_llm, scenario_source, scenario_actions):
            page = browser.new_page(viewport={"width": 1400, "height": 900})
            page_errors = []
            page.on("pageerror", lambda error: page_errors.append(str(error)))
            scenario(page, args.screenshots, log)
            verify("no JavaScript error in the page", not page_errors, "; ".join(page_errors))
            page.close()
        browser.close()
    print()
    if failed:
        print(f"VIEW ACCEPTANCE TEST FAILED: {failed} checks")
        return 1
    print("view acceptance test: all ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
