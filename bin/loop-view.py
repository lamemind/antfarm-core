#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["websockets==17.3"]
# ///
"""The view of the loop: reads the state of the loop from disk, publishes it on a WebSocket, and turns
the click on a switch into a request of the human.

    ANTFARM_STATE_DIR=<project> antfarm-core/bin/loop-view.py [--port N]

It is a process apart from the loop: it stays up when the loop dies, and shows it, and the loop
stays standard library only. It reads the files that bin/loopstate.py describes — status,
switches, session registry, mailboxes, pending requests, interpellations, grants — every
POLL_S seconds, and the tail of registry/loop.jsonl. It publishes the state every time it changes,
and the new rows of the log as they arrive; a client receives first the current state and the
last LOG_ROWS rows of the log.

One port only, bound to 127.0.0.1:

    GET  /                                    the page: view/loop.html, loop.js, loop.css
    GET  /events                              the WebSocket: `state` and `log` messages
    POST /switch?position=<p>&on=true|false   deposits a switch request with sender `human` in
                                              loop/requests/, as `loop.py switch` does, waits up
                                              to DECISION_WAIT_S for the decision of the loop and
                                              answers with it in JSON

The loop decides the request: the view checks only the shape of the parameters. A request the loop
does not see in time — the loop is down — stays among the pending ones, and the loop rejects it as
`stale` at its next start, because it starts in neutral.

The WebSocket and the POST reject a page of another origin, from the Origin header that a browser
always sends; a request without Origin passes. A local process — curl, or a session with free
Bash — can therefore switch a position on as the human: the fence of a session is in the flags of
its launch, from its grant, not in this port.

The view writes only request files: the switches, the registry, the status and the log belong to
the loop. Default port 8741; --port 0 lets the system choose it. The first line on stdout carries
the address. It stops on SIGINT or SIGTERM; exits 2 without a state root.
"""

import argparse
import asyncio
import collections
import json
import re
import signal
import sys
from http import HTTPStatus
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

sys.path.insert(0, str(Path(__file__).resolve().parent))
import graph as libgraph  # noqa: E402
import grants as libgrants  # noqa: E402
import loopstate as libstate  # noqa: E402

STATE_ROOT = libgraph.state_root()
CODE_ROOT = libgraph.code_root()
VIEW_DIR = CODE_ROOT / "view"
# The id of a position as the grant schema declares it: the view refuses another shape before
# depositing anything.
POSITION_PATTERN = re.compile(json.loads((CODE_ROOT / "schema" / "grant.schema.json").read_text(encoding="utf-8"))
                              ["$defs"]["id"]["pattern"])
ADDRESS = "127.0.0.1"
DEFAULT_PORT = 8741
POLL_S = 0.25
LOG_ROWS = 500
DECISION_WAIT_S = 5
EVENTS_PATH = "/events"
SWITCH_PATH = "/switch"
VIEW_FILES = {
    "/": ("loop.html", "text/html; charset=utf-8"),
    "/loop.js": ("loop.js", "text/javascript; charset=utf-8"),
    "/loop.css": ("loop.css", "text/css; charset=utf-8"),
}
SESSION_FIELDS = ("id", "position", "type", "status", "pid", "starttime", "short_id", "sessionId", "launched",
                  "launched_by", "request", "reason", "wakes", "last_wake", "closed", "close_reason", "cwd")


# <editor-fold defaultstate="collapsed" desc="State on disk → one snapshot">
def loop_process(status):
    """The loop as its status file describes it, with `alive` from /proc: a status whose pid and
    start time no longer exist is the last word of a loop that died."""
    if status is None:
        return None
    pid, starttime = status.get("pid"), status.get("starttime")
    alive = bool(pid and starttime) and libstate.alive(pid, starttime)
    return {**{field: status.get(field) for field in ("pid", "started", "heartbeat", "cycle_s", "code_root")},
            "alive": alive}


def grant_positions(paths):
    """position → grant, for every grant file readable as JSON: the description and the type of
    launch, and the org chart when the loop has not written its status yet."""
    if not paths.grants.is_dir():
        return {}
    found = {}
    for content in libgrants.read_directory(paths.grants).values():
        if isinstance(content, dict) and isinstance(content.get("position"), str):
            found[content["position"]] = content
    return found


def positions_of(status, grants):
    """The positions the loop holds, from its status: the last valid set of grants. Without a status,
    the positions of the grant files, as they are on disk, with no figure of the loop."""
    if status is not None:
        return dict(status.get("positions") or {}), "status"
    return {position: {"parent": grant.get("parent"), "max_sessions": grant.get("max_sessions"),
                       "budget_tokens": grant.get("budget_tokens")}
            for position, grant in grants.items()}, "grants"


def session_view(entry):
    view = {field: entry.get(field) for field in SESSION_FIELDS}
    if entry.get("status") == "alive":
        view["process"] = "alive" if entry.get("pid") and entry.get("starttime") and \
            libstate.alive(entry["pid"], entry["starttime"]) else "gone"
    return view


def mailbox_positions(paths):
    try:
        return sorted(directory.name for directory in paths.mailboxes.iterdir() if directory.is_dir())
    except OSError:
        return []


def snapshot(paths):
    """Everything the page shows except the log, read from disk now."""
    status = libstate.read_json(paths.status)
    grants = grant_positions(paths)
    positions, source = positions_of(status, grants)
    for position, figures in positions.items():
        grant = grants.get(position) or {}
        figures["description"] = grant.get("description")
        figures["launch_type"] = (grant.get("launch") or {}).get("type")
    registry = libstate.read_json(paths.sessions, {"sessions": {}})
    return {
        "state_root": str(paths.state),
        "loop": loop_process(status),
        "positions": positions,
        "positions_source": source,
        "switches": libstate.read_json(paths.switches, {}),
        "sessions": [session_view(entry) for entry in (registry.get("sessions") or {}).values()],
        "mailboxes": {position: [message for _, message in libstate.pending_messages(paths, position)]
                      for position in mailbox_positions(paths)},
        "received": {position: len(libstate.entry_files(paths.received(position))) for position in mailbox_positions(paths)},
        "requests": [request for request in (libstate.read_json(path) for path in libstate.entry_files(paths.requests))
                     if request is not None],
        "interpellations": [entry for entry in (libstate.read_json(path)
                                                for path in libstate.entry_files(paths.interpellations))
                            if entry is not None],
    }
# </editor-fold>


# <editor-fold defaultstate="collapsed" desc="Live: state and log tail, published to every client">
class Live:
    def __init__(self, paths):
        self.paths = paths
        self.state_text = None
        self.offset = 0
        self.remainder = b""
        self.rows = collections.deque(maxlen=LOG_ROWS)
        self.subscribers = set()

    def publish(self, message):
        text = json.dumps(message, ensure_ascii=False)
        for queue in self.subscribers:
            queue.put_nowait(text)

    def subscribe(self):
        """A queue that starts with the current state and the last rows of the log: copy and
        subscription sit in the same synchronous step, so nothing falls between the two."""
        queue = asyncio.Queue()
        queue.put_nowait(json.dumps({"type": "state", "state": json.loads(self.state_text)}, ensure_ascii=False))
        queue.put_nowait(json.dumps({"type": "log", "reset": True, "rows": list(self.rows)}, ensure_ascii=False))
        self.subscribers.add(queue)
        return queue

    def new_rows(self):
        """The rows appended to the log since the last read. A log shorter than what was read has
        been replaced: the reading starts again from the top, and the clients are told so."""
        try:
            with open(self.paths.log, "rb") as log:
                size = log.seek(0, 2)
                reset = size < self.offset
                if reset:
                    self.offset, self.remainder = 0, b""
                    self.rows.clear()
                log.seek(self.offset)
                data = log.read()
        except FileNotFoundError:
            return [], False
        self.offset += len(data)
        blocks = (self.remainder + data).split(b"\n")
        self.remainder = blocks.pop()
        rows = []
        for block in blocks:
            if block.strip():
                try:
                    rows.append(json.loads(block))
                except json.JSONDecodeError:
                    continue
        return rows, reset

    def tick(self):
        rows, reset = self.new_rows()
        if rows or reset:
            self.rows.extend(rows)
            self.publish({"type": "log", "reset": reset, "rows": list(self.rows) if reset else rows})
        text = json.dumps(snapshot(self.paths), ensure_ascii=False, sort_keys=True)
        if text != self.state_text:
            self.state_text = text
            self.publish({"type": "state", "state": json.loads(text)})

    async def follow(self):
        while True:
            self.tick()
            await asyncio.sleep(POLL_S)


async def forward(connection, queue):
    try:
        while True:
            await connection.send(await queue.get())
    except ConnectionClosed:
        pass


async def serve_client(live, connection):
    queue = live.subscribe()
    sending = asyncio.create_task(forward(connection, queue))
    try:
        await connection.wait_closed()
    finally:
        live.subscribers.discard(queue)
        sending.cancel()
# </editor-fold>


# <editor-fold defaultstate="collapsed" desc="HTTP on the same port: page, origin, switch">
def json_response(connection, status, payload):
    return with_type(connection.respond(status, json.dumps(payload, ensure_ascii=False) + "\n"),
                     "application/json; charset=utf-8")


def with_type(response, kind):
    del response.headers["Content-Type"]
    response.headers["Content-Type"] = kind
    response.headers["Cache-Control"] = "no-store"
    return response


def switch_parameters(query):
    """(position, on) from the query of a switch POST, or (None, reason)."""
    values = parse_qs(query, keep_blank_values=True)
    positions, states = values.get("position", []), values.get("on", [])
    if len(positions) != 1 or not POSITION_PATTERN.fullmatch(positions[0]):
        return None, "position: one value, an id of the grant schema"
    if len(states) != 1 or states[0] not in ("true", "false"):
        return None, "on: one value, true or false"
    return (positions[0], states[0] == "true"), None


async def switch(paths, connection, query):
    parameters, reason = switch_parameters(query)
    if parameters is None:
        return json_response(connection, HTTPStatus.BAD_REQUEST, {"error": reason})
    position, on = parameters
    request_id = libstate.deposit_request(paths, libstate.HUMAN, "switch", position=position, on=on)
    state = await asyncio.to_thread(libstate.wait_for_decision, paths, request_id, DECISION_WAIT_S)
    return json_response(connection, HTTPStatus.OK, {"request": request_id, "position": position, "on": on,
                                                     "decision": state, "text": libstate.describe_decision(state)})


def http_response(paths, channel):
    async def reply(connection, request):
        parts = urlsplit(request.path)
        origin = request.headers.get("Origin")
        allowed = {f"http://{ADDRESS}:{channel['port']}", f"http://localhost:{channel['port']}"}
        if parts.path in (EVENTS_PATH, SWITCH_PATH) and origin is not None and origin not in allowed:
            return connection.respond(HTTPStatus.FORBIDDEN, f"origin not allowed: {origin}\n")
        if parts.path == SWITCH_PATH:
            if request.method != "POST":
                return connection.respond(HTTPStatus.METHOD_NOT_ALLOWED, "POST only\n")
            return await switch(paths, connection, parts.query)
        if request.method != "GET":
            return connection.respond(HTTPStatus.METHOD_NOT_ALLOWED, "GET only\n")
        if parts.path == EVENTS_PATH:
            return None
        if parts.path in VIEW_FILES:
            name, kind = VIEW_FILES[parts.path]
            try:
                text = (VIEW_DIR / name).read_text(encoding="utf-8")
            except OSError:
                return connection.respond(HTTPStatus.NOT_FOUND, f"view missing: {name}\n")
            return with_type(connection.respond(HTTPStatus.OK, text), kind)
        return connection.respond(HTTPStatus.NOT_FOUND, "not found\n")
    return reply
# </editor-fold>


async def run(port):
    paths = libstate.Paths(STATE_ROOT)
    live = Live(paths)
    live.tick()
    channel = {"port": port}

    async def handle(connection):
        await serve_client(live, connection)

    async with serve(handle, ADDRESS, port, process_request=http_response(paths, channel)) as server:
        channel["port"] = server.sockets[0].getsockname()[1]
        print(f"loop view · http://{ADDRESS}:{channel['port']}/ · state {paths.state}", flush=True)
        follower = asyncio.create_task(live.follow())
        stopped = asyncio.get_running_loop().create_future()
        for signum in (signal.SIGINT, signal.SIGTERM):
            asyncio.get_running_loop().add_signal_handler(signum, lambda: stopped.done() or stopped.set_result(None))
        await stopped
        follower.cancel()
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()
    return asyncio.run(run(args.port))


if __name__ == "__main__":
    sys.exit(main())
