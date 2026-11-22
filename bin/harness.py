#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["websockets==17.3"]
# ///
"""Observability harness of the engine: reads the engine's sources, turns them into events, publishes them on a WebSocket.

    antfarm-core/bin/harness.py live [--port N]
        follows live the matches open at startup and those born later. Reads the message log
        from the start and then follows its tail, and the match directories for the opening,
        the launch and closing of the nodes, the closing of the match: it starts before or after
        the director without losing rows. Every client receives first the events already
        emitted, then the new ones.

    antfarm-core/bin/harness.py replay <match>... [--speed V] [--port N]
        replays matches already played, one after the other, in ts order and with their timing
        divided by V (default 1); V = 0 emits without waiting. Every client receives the whole
        replay from the start, then the channel closes.

    antfarm-core/bin/harness.py stream <events.jsonl> [--speed V] [--port N]
        like replay, but from a file of ready-made events, sent exactly as they are written: the
        acceptance test of what the engine does not produce yet, such as the spawn.

Three event classes, schema in schema/event.schema.json: lifecycle (match-opened, match-closed,
node-launched, node-closed, from meta.json and nodes.json; graph-changed, from graphs/), message
(delivered, rejected, received, written: the four kinds of row in the message log), spawn. Every
event wraps the source row intact in the `row` field; match-opened carries meta.json and the first
version of the match's graph, and the view draws from there; graph-changed carries the file of the
new version and, at the top, the delta from the previous one. The log rows without a match
(unknown-sender) do not become events: they are counted, and the count goes to stderr on shutdown.

One port only, bound to 127.0.0.1: `/` serves the view, `/channel.json` the mode and the speed,
`/events` the WebSocket. The WebSocket rejects pages from another origin, because the log carries
the full text of the messages. The harness does not write in registry/ or in matches/: it only
reads. It reads them from the state root, ANTFARM_STATE_DIR, and every mode requires it.

Default port 8740; --port 0 lets the system choose it. The first line on stdout carries the
address. It stops on SIGINT or SIGTERM.
"""

import argparse
import asyncio
import json
import signal
import sys
from datetime import datetime
from http import HTTPStatus
from pathlib import Path

from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

sys.path.insert(0, str(Path(__file__).resolve().parent))
import graph as libgraph  # noqa: E402

STATE_ROOT = libgraph.state_root()
MATCHES = STATE_ROOT / "matches"
MESSAGE_LOG = STATE_ROOT / "registry" / "messages.jsonl"
VIEW_DIR = libgraph.code_root() / "view"
ADDRESS = "127.0.0.1"
DEFAULT_PORT = 8740
LIVE_STEP_S = 0.1
EVENTS_PATH = "/events"
VIEW_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/view.js": ("view.js", "text/javascript; charset=utf-8"),
    "/view.css": ("view.css", "text/css; charset=utf-8"),
}
# At equal ts, the lifecycle order: the new graph before the launch of the node that enters it,
# the launch before the messages, the closing after.
PRIORITY_AT_EQUAL_TS = {"graph-changed": -1, "node-launched": 0, "delivered": 1, "rejected": 1, "received": 1, "written": 1,
                        "spawn": 1, "node-closed": 2}


# <editor-fold defaultstate="collapsed" desc="Sources: engine files → events">
def read_json(path):
    """The content of a JSON file; None if it is missing or half written."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def split_log_rows(content):
    """(index, row) for every row of the log, with the index from 1 as the line number in the file.
    The split is on newline only: the bus writes without ASCII escapes, and a text can carry
    Unicode line separators that splitlines() would take for newlines."""
    rows = []
    for index, block in enumerate(content.split(b"\n"), start=1):
        if not block.strip():
            continue
        try:
            rows.append((index, json.loads(block)))
        except json.JSONDecodeError:
            continue
    return rows


def instant(ts):
    return datetime.fromisoformat(ts).timestamp()


def message_event(index, row):
    return {"class": "message", "type": row["outcome"], "ts": row["ts"], "match": row["match"],
            "index": index, "row": row}


def match_opened_event(directory, meta, graph):
    return {"class": "lifecycle", "type": "match-opened", "ts": meta["start"], "match": directory.name,
            "row": {"meta": meta, "graph": graph}}


def match_closed_event(directory, meta):
    return {"class": "lifecycle", "type": "match-closed", "ts": meta["end"], "match": directory.name, "row": meta}


def graph_versions(directory):
    """The entries of the match's graphs/v<N>.json, in version order. A match born before
    graphs/ gives the empty list."""
    entries = [read_json(path) for path in (directory / "graphs").glob("v*.json")]
    return sorted((entry for entry in entries if entry is not None), key=lambda entry: entry["graph"]["version"])


def initial_graph(directory):
    """The graph the match opened with: the first stored version, or graph.json for a match
    born before graphs/, which never changed graph."""
    versions = graph_versions(directory)
    return versions[0]["graph"] if versions else read_json(directory / "graph.json")


def graph_changed_event(directory, previous, entry):
    return {"class": "lifecycle", "type": "graph-changed", "ts": entry["effective"], "match": directory.name,
            "delta": libgraph.delta(previous["graph"], entry["graph"]), "row": entry}


def node_event(kind, directory, node_id, entry):
    field = "launched" if kind == "node-launched" else "closed"
    return {"class": "lifecycle", "type": kind, "ts": entry[field], "match": directory.name, "node": node_id,
            "row": entry}


def node_events(directory, nodes):
    """node-launched for every entry with `launched`, node-closed for every entry with `closed`. The
    director does not carry `launched`: it is the one that opens the match."""
    events = []
    for node_id, entry in (nodes or {}).get("nodes", {}).items():
        if entry.get("launched"):
            events.append(node_event("node-launched", directory, node_id, entry))
        if entry.get("closed"):
            events.append(node_event("node-closed", directory, node_id, entry))
    return events
# </editor-fold>


# <editor-fold defaultstate="collapsed" desc="Replay: a match already played, replayed with its own timing">
def match_dir(arg):
    for candidate in (MATCHES / arg, Path(arg)):
        if (candidate / "meta.json").is_file() and (candidate / "graph.json").is_file():
            return candidate.resolve()
    raise SystemExit(f"match not found: {arg} (neither under {MATCHES} nor as a directory)")


def match_events(directory, match_rows):
    """The events of a closed match in replay order: the opening with the graph of the first
    version, then graph changes, launches, messages and node closings in ts order, then the
    closing of the match. Opening and closing sit at the two ends because meta.json carries
    seconds and the log milliseconds: the closing truncated to the second would fall before the
    last receipt."""
    meta, graph = read_json(directory / "meta.json"), initial_graph(directory)
    versions = graph_versions(directory)
    center = [graph_changed_event(directory, previous, entry) for previous, entry in zip(versions, versions[1:])]
    center += node_events(directory, read_json(directory / "nodes.json"))
    center += [message_event(index, row) for index, row in match_rows]
    center.sort(key=lambda event: (instant(event["ts"]), PRIORITY_AT_EQUAL_TS[event["type"]]))
    closure = [match_closed_event(directory, meta)] if meta.get("end") else []
    return [match_opened_event(directory, meta, graph)] + center + closure


def schedule(groups, speed):
    """(seconds from the start of the replay, text of the event) for every event. Within a group
    the times are those of the ts divided by the speed, never backwards; between a group and the
    next there is no pause."""
    result, offset = [], 0.0
    for events in groups:
        if not events:
            continue
        origin, last = instant(events[0]["ts"]), offset
        for event in events:
            when = offset + ((instant(event["ts"]) - origin) / speed if speed > 0 else 0.0)
            last = max(last, when)
            result.append((last, json.dumps(event, ensure_ascii=False)))
        offset = last
    return result


def schedule_from_stream(path, speed):
    """The schedule of an events file: file order, times from the ts, text sent as it is written."""
    texts = [text for text in Path(path).read_text(encoding="utf-8").split("\n") if text.strip()]
    events = [json.loads(text) for text in texts]
    result, last = [], 0.0
    origin = instant(events[0]["ts"]) if events else 0.0
    for event, text in zip(events, texts):
        when = (instant(event["ts"]) - origin) / speed if speed > 0 else 0.0
        last = max(last, when)
        result.append((last, text))
    return result


async def replay_events(connection, scheduled_events):
    loop = asyncio.get_running_loop()
    departure = loop.time()
    try:
        for when, text in scheduled_events:
            wait = departure + when - loop.time()
            if wait > 0:
                await asyncio.sleep(wait)
            await connection.send(text)
        await connection.close(1000, "end of replay")
    except ConnectionClosed:
        pass
# </editor-fold>


# <editor-fold defaultstate="collapsed" desc="Live: tail of the log and match directories">
class FollowedMatch:
    def __init__(self, directory, version):
        self.directory = directory
        self.version = version
        self.launched = set()
        self.closed_nodes = set()
        self.closed = False


class Live:
    """Follows the engine while it plays. On every tick it reads the new rows of the log, then
    looks at the match directories. Before emitting a closing — of a node or of the match — it
    reads the log again: the director writes the closing after the last row of that match, so the
    second read brings every row that precedes it."""

    def __init__(self):
        self.position = 0
        self.remainder = b""
        self.rows_read = 0
        self.followed_matches = {}
        self.ignored = set()
        self.without_match = 0
        self.without_dir = 0
        self.emitted = []
        self.subscribers = set()

    def start(self):
        """Matches already closed at startup are not followed: they are material for the replay."""
        if MATCHES.is_dir():
            for directory in MATCHES.iterdir():
                if (read_json(directory / "nodes.json") or {}).get("status") == "closed":
                    self.ignored.add(directory.name)

    def publish(self, event):
        text = json.dumps(event, ensure_ascii=False)
        self.emitted.append(text)
        for queue in self.subscribers:
            queue.put_nowait(text)

    def subscribe(self):
        """A queue that starts with the events already emitted and receives the new ones: copy and
        subscription sit in the same synchronous step, so no event falls between the two."""
        queue = asyncio.Queue()
        for text in self.emitted:
            queue.put_nowait(text)
        self.subscribers.add(queue)
        return queue

    def new_rows(self):
        try:
            with open(MESSAGE_LOG, "rb") as log:
                log.seek(self.position)
                data = log.read()
        except FileNotFoundError:
            return []
        self.position += len(data)
        blocks = (self.remainder + data).split(b"\n")
        self.remainder = blocks.pop()
        first = self.rows_read + 1
        self.rows_read += len(blocks)
        return [(first + index - 1, row) for index, row in split_log_rows(b"\n".join(blocks))]

    def open_match(self, match):
        if match in self.followed_matches:
            return self.followed_matches[match]
        directory = MATCHES / match
        meta, graph = read_json(directory / "meta.json"), initial_graph(directory)
        if meta is None or graph is None:
            return None
        followed_match = self.followed_matches[match] = FollowedMatch(directory, graph["version"])
        self.publish(match_opened_event(directory, meta, graph))
        return followed_match

    def update_graphs(self, followed_match):
        """The versions that appeared in graphs/, each with the delta from the previous one. It runs
        before a message is emitted, and `graphs/v<N>.json` exists before a message carries N: the
        client already has the edge the dot must run along."""
        versions = graph_versions(followed_match.directory)
        for previous, entry in zip(versions, versions[1:]):
            if entry["graph"]["version"] > followed_match.version:
                followed_match.version = entry["graph"]["version"]
                self.publish(graph_changed_event(followed_match.directory, previous, entry))

    def update_launches(self, followed_match):
        nodes = read_json(followed_match.directory / "nodes.json")
        for event in node_events(followed_match.directory, nodes):
            if event["type"] == "node-launched" and event["node"] not in followed_match.launched:
                followed_match.launched.add(event["node"])
                self.publish(event)
        return nodes

    def read_log(self):
        for index, row in self.new_rows():
            match = row.get("match")
            if match is None:
                self.without_match += 1
                continue
            if match in self.ignored:
                continue
            followed_match = self.open_match(match)
            if followed_match is None:
                self.without_dir += 1
                continue
            self.update_graphs(followed_match)
            self.update_launches(followed_match)
            self.publish(message_event(index, row))

    def update_match(self, match):
        followed_match = self.open_match(match)
        if followed_match is None or followed_match.closed:
            return
        self.update_graphs(followed_match)
        nodes = self.update_launches(followed_match)
        closures = [event for event in node_events(followed_match.directory, nodes)
                    if event["type"] == "node-closed" and event["node"] not in followed_match.closed_nodes]
        meta = read_json(followed_match.directory / "meta.json") or {}
        if not closures and not meta.get("end"):
            return
        self.read_log()
        for event in sorted(closures, key=lambda event: instant(event["ts"])):
            followed_match.closed_nodes.add(event["node"])
            self.publish(event)
        if meta.get("end"):
            followed_match.closed = True
            self.publish(match_closed_event(followed_match.directory, meta))

    def tick(self):
        self.read_log()
        if MATCHES.is_dir():
            for directory in sorted(MATCHES.iterdir()):
                if directory.is_dir() and directory.name not in self.ignored:
                    self.update_match(directory.name)

    async def follow(self):
        while True:
            self.tick()
            await asyncio.sleep(LIVE_STEP_S)


async def forward(connection, queue):
    try:
        while True:
            await connection.send(await queue.get())
    except ConnectionClosed:
        pass


async def serve_live(live, connection):
    queue = live.subscribe()
    sending = asyncio.create_task(forward(connection, queue))
    try:
        await connection.wait_closed()
    finally:
        live.subscribers.discard(queue)
        sending.cancel()
# </editor-fold>


# <editor-fold defaultstate="collapsed" desc="HTTP on the same port: view, channel state, origin">
def http_response(channel):
    def reply(connection, request):
        path = request.path.split("?", 1)[0]
        if path == EVENTS_PATH:
            origin = request.headers.get("Origin")
            allowed = {f"http://{ADDRESS}:{channel['port']}", f"http://localhost:{channel['port']}"}
            if origin is not None and origin not in allowed:
                return connection.respond(HTTPStatus.FORBIDDEN, f"origin not allowed: {origin}\n")
            return None
        if path == "/channel.json":
            return with_type(connection.respond(HTTPStatus.OK, json.dumps(channel, ensure_ascii=False)),
                             "application/json; charset=utf-8")
        if path in VIEW_FILES:
            name, kind = VIEW_FILES[path]
            try:
                text = (VIEW_DIR / name).read_text(encoding="utf-8")
            except OSError:
                return connection.respond(HTTPStatus.NOT_FOUND, f"view missing: {name}\n")
            return with_type(connection.respond(HTTPStatus.OK, text), kind)
        return connection.respond(HTTPStatus.NOT_FOUND, "not found\n")
    return reply


def with_type(response, kind):
    del response.headers["Content-Type"]
    response.headers["Content-Type"] = kind
    response.headers["Cache-Control"] = "no-store"
    return response
# </editor-fold>


async def start(args):
    channel = {"mode": args.mode, "port": args.port, "speed": None, "matches": [], "stream": None}
    live, scheduled_events, follower = None, None, None
    if args.mode == "live":
        live = Live()
        live.start()
    elif args.mode == "replay":
        directories = [match_dir(arg) for arg in args.matches]
        rows = split_log_rows(MESSAGE_LOG.read_bytes()) if MESSAGE_LOG.exists() else []
        groups = [match_events(directory, [(index, row) for index, row in rows if row.get("match") == directory.name])
                  for directory in directories]
        scheduled_events = schedule(groups, args.speed)
        channel.update(speed=args.speed, matches=[directory.name for directory in directories])
    else:
        scheduled_events = schedule_from_stream(args.events_file, args.speed)
        channel.update(speed=args.speed, stream=str(args.events_file))

    async def handle(connection):
        if live is not None:
            await serve_live(live, connection)
        else:
            await replay_events(connection, scheduled_events)

    async with serve(handle, ADDRESS, args.port, process_request=http_response(channel)) as server:
        channel["port"] = server.sockets[0].getsockname()[1]
        detail = (f"{len(scheduled_events)} events" if scheduled_events is not None
                  else f"{len(live.ignored)} matches already closed, not followed")
        print(f"harness · {args.mode} · http://{ADDRESS}:{channel['port']}/ · {detail}", flush=True)
        if live is not None:
            follower = asyncio.create_task(live.follow())
        stopped = asyncio.get_running_loop().create_future()
        for signum in (signal.SIGINT, signal.SIGTERM):
            asyncio.get_running_loop().add_signal_handler(signum, lambda: stopped.done() or stopped.set_result(None))
        await stopped
        if follower is not None:
            follower.cancel()
    if live is not None:
        print(f"harness: log rows without a match, not emitted: {live.without_match} · "
              f"rows of matches without a readable directory: {live.without_dir} · "
              f"events emitted: {len(live.emitted)}", file=sys.stderr)
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    subparsers = parser.add_subparsers(dest="mode", required=True)
    live_parser = subparsers.add_parser("live")
    replay = subparsers.add_parser("replay")
    replay.add_argument("matches", nargs="+")
    stream = subparsers.add_parser("stream")
    stream.add_argument("events_file")
    for command in (live_parser, replay, stream):
        command.add_argument("--port", type=int, default=DEFAULT_PORT)
    for command in (replay, stream):
        command.add_argument("--speed", type=float, default=1.0)
    args = parser.parse_args()
    if getattr(args, "speed", 0) < 0:
        parser.error("--speed cannot be negative")
    return asyncio.run(start(args))


if __name__ == "__main__":
    sys.exit(main())
