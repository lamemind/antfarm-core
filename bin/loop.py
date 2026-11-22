#!/usr/bin/env python3
"""The loop of antfarm: one process per project that launches sessions on the positions of an org
chart, wakes them, and executes their requests.

    ANTFARM_STATE_DIR=<project> antfarm-core/bin/loop.py [run] [--cycle <s>]
        the loop process: it runs in a terminal window and prints its log there
    ANTFARM_STATE_DIR=<project> antfarm-core/bin/loop.py switch <position> on|off
    ANTFARM_STATE_DIR=<project> antfarm-core/bin/loop.py spawn <position> [text...]
        a request of the human, as the view sends it: the loop decides it, the command prints the outcome

The loop reads folders of files under the state root (bin/loopstate.py says which) and writes its
log in registry/loop.jsonl. It starts in neutral: every switch off, no session starts by itself.
Switching a position on means the loop may start and wake sessions on it; switching the root on
also starts its first session, because the parent of the root is the human. Every other session
starts when someone asks a spawn on its position: the human on any declared position, a session
only on a child position of its own.

A session of type llm is a Claude Code session in the background (`claude --bg`), launched with
the flags of the launch template of its grant. The loop wakes it when a message waits for it in
its mailbox and the session has ended its turn: `claude stop`, then `claude --bg --resume` without
flags, which continues the same session with the options saved at launch. A session of type script
is a local process, for the tests.

The loop rereads the grants at every cycle: a changed directory that validate-grants.py accepts
takes effect without a restart; an invalid one does not stop the loop, which keeps the last valid
set and writes the reason in the log. At start, invalid grants stop the loop with exit 2.

Standard library only. Exits 0 on SIGTERM or SIGINT, 2 without a state root or with invalid grants.
"""

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import graph as libgraph  # noqa: E402
import grants as libgrants  # noqa: E402
import loopstate as libstate  # noqa: E402
import match as libmatch  # noqa: E402

CODE_ROOT = libgraph.code_root()
STATE_ROOT = libmatch.STATE_ROOT
REQUEST = CODE_ROOT / "bin" / "request"
VALIDATOR = CODE_ROOT / "bin" / "validate-grants.py"
HUMAN = libstate.HUMAN
CYCLE_S = 1.0
# The environment of a launch: only these variables pass. A session in the background carries
# the environment of the Claude Code background service, which the first launch starts; what
# always reaches the session is the `env` block of --settings (test/probe-resume.py, measure 5).
PASSED_VARIABLES = ("HOME", "PATH")
BASH_TIMEOUT_MS = 900000
# `claude --bg` prints `backgrounded · <short id>`, followed by ` · <name>` only when the launch
# passes --name: a resume does not.
SHORT_ID = re.compile(r"backgrounded · ([0-9a-f]{8})")
LAUNCH_TIMEOUT_S = 120
STOP_WAIT_S = 30
DECISION_WAIT_S = 5
# The one line the loop writes into a session it wakes; it reaches an LLM, so it is in Italian.
WAKE_TEXT = "Il loop ti sveglia: nella tua casella c'è un messaggio per te. Leggilo con `{request} recv`."


class LaunchError(Exception):
    pass


def launch_environment():
    return {name: os.environ[name] for name in PASSED_VARIABLES if name in os.environ}


class Loop:
    def __init__(self, paths, cycle_s):
        self.paths = paths
        self.cycle_s = cycle_s
        self.grants = {}
        self.fingerprint = None
        self.rejected_fingerprint = None
        self.switches = {}
        self.registry = {"sessions": {}}
        self.children = {}
        self.queued = {}
        self.received_logged = set()
        self.stopping = False

    # <editor-fold defaultstate="collapsed" desc="Log">
    def log(self, event, position=None, session=None, request=None, sender=None, reason=None, detail=None):
        """One row of registry/loop.jsonl, and the same row in clear on the terminal."""
        row = {"ts": libstate.now(), "event": event, "position": position, "session": session,
               "request": request, "from": sender, "reason": reason, "detail": detail}
        self.paths.log.parent.mkdir(parents=True, exist_ok=True)
        with open(self.paths.log, "a", encoding="utf-8") as log_file:
            log_file.write(json.dumps(row, ensure_ascii=False) + "\n")
        words = [row["ts"][11:23], f"{event:<17}", position or "-", session or "", reason or "", detail or ""]
        print(" ".join(word for word in words if word), flush=True)
    # </editor-fold>

    # <editor-fold defaultstate="collapsed" desc="Grants">
    def validated_grants(self):
        """(grants by position, None) if validate-grants.py accepts the directory, else (None, reason)."""
        try:
            outcome = subprocess.run([str(VALIDATOR), str(self.paths.grants)], capture_output=True, text=True, timeout=120)
        except (OSError, subprocess.TimeoutExpired) as error:
            return None, f"validate-grants.py does not run: {error}"
        if outcome.returncode != 0:
            return None, outcome.stderr.strip() or f"validate-grants.py exits {outcome.returncode}"
        contents = libgrants.read_directory(self.paths.grants)
        if any(grant is None for grant in contents.values()):
            return None, "a grant file changed while it was being validated"
        return libgrants.by_position(contents), None

    def load_grants_at_start(self):
        """At start there is no last valid set: invalid grants stop the loop with exit 2."""
        self.fingerprint = libgrants.fingerprint(self.paths.grants)
        grants, reason = self.validated_grants()
        if grants is None:
            self.log("grants-rejected", reason="invalid-at-start", detail=reason)
            print(f"invalid grants in {self.paths.grants}: the loop does not start\n{reason}", file=sys.stderr)
            raise SystemExit(2)
        self.grants = grants
        self.log("grants-loaded", detail=f"{len(grants)} positions, root {libgrants.root(grants)}: {', '.join(sorted(grants))}")

    def reload_grants(self):
        """Rereads the grants when the directory changes. A set that is not valid, or that removes a
        position with live sessions, does not take effect: the loop keeps the last valid set and
        writes the reason once per version of the directory."""
        fingerprint = libgrants.fingerprint(self.paths.grants)
        if fingerprint in (self.fingerprint, self.rejected_fingerprint):
            return
        grants, reason = self.validated_grants()
        if grants is not None:
            occupied = sorted(position for position in self.grants
                              if position not in grants and self.alive_sessions(position))
            if occupied:
                grants, reason = None, f"removes positions with live sessions: {', '.join(occupied)}"
        if grants is None:
            self.rejected_fingerprint = fingerprint
            self.log("grants-rejected", reason="invalid", detail=f"{reason} — the loop keeps the last valid grants")
            return
        added = sorted(set(grants) - set(self.grants))
        removed = sorted(set(self.grants) - set(grants))
        self.grants = grants
        self.fingerprint = fingerprint
        self.rejected_fingerprint = None
        for position in removed:
            self.switches.pop(position, None)
        for position in added:
            self.switches[position] = {"on": False, "since": libstate.now(), "reason": "new-position"}
        self.save_switches()
        self.log("grants-loaded", detail=f"{len(grants)} positions, root {libgrants.root(grants)}; "
                                         f"added {added or '-'}, removed {removed or '-'}")
    # </editor-fold>

    # <editor-fold defaultstate="collapsed" desc="Registry and switches">
    def save_registry(self):
        libstate.write_json(self.paths.sessions, self.registry)

    def save_switches(self):
        libstate.write_json(self.paths.switches, self.switches)

    def sessions(self):
        return self.registry["sessions"]

    def alive_sessions(self, position):
        """The live sessions of a position, in launch order."""
        return [session_id for session_id, entry in self.sessions().items()
                if entry["position"] == position and entry["status"] == "alive"]

    def is_on(self, position):
        return self.switches.get(position, {}).get("on", False)

    def set_switch(self, position, on, reason):
        self.switches[position] = {"on": on, "since": libstate.now(), "reason": reason}
        self.save_switches()

    def next_session_id(self, position):
        numbers = [int(session_id.rsplit("-", 1)[1]) for session_id, entry in self.sessions().items()
                   if entry["position"] == position and session_id.rsplit("-", 1)[1].isdigit()]
        return f"{position}-{max(numbers, default=0) + 1}"
    # </editor-fold>

    # <editor-fold defaultstate="collapsed" desc="Spawn">
    def spawn(self, position, launched_by, request_id, reason, text=None):
        """Launches a session on the position and registers it; returns its id, or None if the
        launch fails. A text becomes the first message of the session, in the position mailbox,
        before the launch: the session finds it at its first `request recv`."""
        session_id = self.next_session_id(position)
        values = {"request": REQUEST, "code": CODE_ROOT, "state": self.paths.state,
                  "session": session_id, "position": position}
        launch = libgrants.resolved_launch(self.grants[position], values)
        Path(launch["cwd"]).mkdir(parents=True, exist_ok=True)
        records = self.paths.records / session_id
        records.mkdir(parents=True, exist_ok=True)
        message = None
        if text:
            message = libstate.deposit_message(self.paths, position, launched_by.get("session") or HUMAN, text,
                                               to_session=session_id)
        try:
            launched = self.launch_llm(session_id, position, launch, records) if launch["type"] == "llm" \
                else self.launch_script(session_id, launch, records)
        except LaunchError as error:
            if message is not None:
                for path in self.paths.mailbox(position).glob(f"[0-9]*-{message['id']}.json"):
                    path.unlink(missing_ok=True)
            self.log("spawn-failed", position=position, session=session_id, request=request_id,
                     sender=launched_by.get("session") or launched_by["kind"], reason=reason, detail=str(error))
            return None
        self.sessions()[session_id] = {
            "id": session_id, "position": position, **launched, "cwd": launch["cwd"],
            "launched": libstate.now(), "launched_by": launched_by, "request": request_id, "reason": reason,
            "status": "alive", "wakes": 0,
        }
        self.save_registry()
        self.log("spawn", position=position, session=session_id, request=request_id,
                 sender=launched_by.get("session") or launched_by["kind"], reason=reason,
                 detail=f"{launched['type']} pid {launched['pid']}")
        return session_id

    def launch_llm(self, session_id, position, launch, records):
        """A Claude Code session in the background, with the flags of the launch template. The state
        root reaches it through the `env` block of --settings, the only environment that always
        arrives (test/probe-resume.py, measure 5)."""
        (records / "prompt.txt").write_text(launch["prompt"], encoding="utf-8")
        name = f"antfarm loop · {position} · {session_id}"
        command = ["claude", "--bg"]
        if launch["safe_mode"]:
            command.append("--safe-mode")
        command += ["--name", name, "--tools", ",".join(launch["tools"])]
        if launch["allowed_tools"]:
            command += ["--allowedTools", *launch["allowed_tools"]]
        command += ["--permission-mode", launch["permission_mode"],
                    "--settings", json.dumps({"env": {
                        "BASH_DEFAULT_TIMEOUT_MS": str(BASH_TIMEOUT_MS),
                        "BASH_MAX_TIMEOUT_MS": str(BASH_TIMEOUT_MS),
                        libgraph.STATE_VARIABLE: str(self.paths.state),
                    }})]
        if launch["model"]:
            command += ["--model", launch["model"]]
        command.append(launch["prompt"])
        try:
            outcome = subprocess.run(command, cwd=launch["cwd"], env=launch_environment(),
                                     capture_output=True, text=True, timeout=LAUNCH_TIMEOUT_S)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise LaunchError(f"claude --bg does not run: {error}") from error
        hit = SHORT_ID.search(outcome.stdout + outcome.stderr)
        if outcome.returncode != 0 or not hit:
            raise LaunchError(f"claude --bg exits {outcome.returncode}: {(outcome.stdout + outcome.stderr).strip()[:300]}")
        try:
            entry = libmatch.wait_for_session(hit.group(1))
        except RuntimeError as error:
            raise LaunchError(str(error)) from error
        return {
            "type": "llm", "pid": entry["pid"], "starttime": libstate.start_time(entry["pid"]),
            "sessionId": entry["sessionId"], "short_id": entry["id"], "name": name,
            "command": [part if part != launch["prompt"] else "<prompt.txt>" for part in command],
        }

    def launch_script(self, session_id, launch, records):
        """A local process in a session of its own: a Ctrl-C in the loop's terminal does not reach
        it, and the death of the loop does not end it."""
        environment = {**launch_environment(), libgraph.STATE_VARIABLE: str(self.paths.state)}
        try:
            with open(records / "output.txt", "w", encoding="utf-8") as output:
                process = subprocess.Popen(launch["command"], cwd=launch["cwd"], env=environment,
                                           stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                                           start_new_session=True)
        except OSError as error:
            raise LaunchError(f"{launch['command'][0]} does not start: {error}") from error
        self.children[session_id] = process
        return {"type": "script", "pid": process.pid, "starttime": libstate.start_time(process.pid),
                "command": launch["command"]}
    # </editor-fold>

    # <editor-fold defaultstate="collapsed" desc="Requests">
    def finish(self, path, request, outcome, **fields):
        """Moves a request among the decided ones, with its outcome."""
        decided = {**request, "outcome": outcome, "decided": libstate.now(), **fields}
        decided.pop("queued", None)
        libstate.write_json(self.paths.requests_done / path.name, decided)
        path.unlink(missing_ok=True)
        self.queued.pop(request.get("id"), None)

    def reject(self, path, request, reason, detail):
        """Moves the request among the decided ones as rejected, with the reason. The detail goes
        back to whoever asked, an LLM session too: it is in Italian."""
        self.log("rejected", position=request.get("position"), request=request.get("id"),
                 sender=request.get("from"), reason=reason, detail=f"{request.get('kind')}: {detail}")
        self.finish(path, request, "rejected", reason=reason, detail=detail)

    def queue(self, path, request, reason, detail):
        """Leaves the request among the pending ones; the loop retries it at every cycle. The row
        in the log and the mark in the file are written only when the reason changes."""
        if self.queued.get(request["id"]) == reason:
            return
        self.queued[request["id"]] = reason
        libstate.write_json(path, {**request, "queued": {"reason": reason, "detail": detail, "since": libstate.now()}})
        self.log("queued", position=request.get("position"), request=request["id"], sender=request.get("from"),
                 reason=reason, detail=f"{request.get('kind')}: {detail}")

    def process_requests(self):
        for path in libstate.entry_files(self.paths.requests):
            request = libstate.read_json(path)
            if request is None:
                self.reject(path, {"id": path.stem.split("-", 1)[-1]}, "unreadable", "il file non è JSON leggibile")
                continue
            self.decide(path, request)

    def decide(self, path, request):
        sender = request.get("from")
        sender_entry = None
        if sender != HUMAN:
            sender_entry = self.sessions().get(sender)
            if sender_entry is None or sender_entry["status"] != "alive":
                self.reject(path, request, "unknown-sender", f"{sender} non è una sessione viva del loop")
                return
        handlers = {"switch": self.request_switch, "spawn": self.request_spawn}
        handler = handlers.get(request.get("kind"))
        if handler is None:
            self.reject(path, request, "unknown-kind", f"il loop non conosce il tipo di richiesta {request.get('kind')!r}")
            return
        handler(path, request, sender_entry)

    def request_switch(self, path, request, sender_entry):
        """Only the human switches. Switching the root on also starts its first session, if none
        of its sessions is alive: the parent of the root is the human, and the click is its request."""
        position = request.get("position")
        if sender_entry is not None:
            self.reject(path, request, "human-only", "solo l'umano accende e spegne una posizione")
            return
        if position not in self.grants:
            self.reject(path, request, "undeclared-position", f"{position} non è una posizione dell'organigramma")
            return
        on = bool(request.get("on"))
        self.set_switch(position, on, "human")
        self.log("switch-on" if on else "switch-off", position=position, request=request["id"], sender=HUMAN)
        spawned = None
        if on and self.grants[position]["parent"] == HUMAN and not self.alive_sessions(position):
            spawned = self.spawn(position, {"kind": HUMAN, "session": None}, request["id"], "switch")
        self.finish(path, request, "applied", session=spawned)

    def request_spawn(self, path, request, sender_entry):
        """The human asks a spawn on any declared position, a session only on a child position of
        its own. A spawn on a position switched off waits in the queue."""
        position = request.get("position")
        if position not in self.grants:
            self.reject(path, request, "undeclared-position", f"{position} non è una posizione dell'organigramma")
            return
        if sender_entry is not None and position not in libgrants.children(self.grants, sender_entry["position"]):
            self.reject(path, request, "not-a-child",
                        f"{position} non è una posizione figlia di {sender_entry['position']}")
            return
        if not self.is_on(position):
            self.queue(path, request, "switched-off", f"l'interruttore di {position} è spento")
            return
        launched_by = {"kind": "session", "session": request["from"]} if sender_entry else {"kind": HUMAN, "session": None}
        spawned = self.spawn(position, launched_by, request["id"], "request", text=request.get("text"))
        if spawned is None:
            self.finish(path, request, "failed", reason="launch-failed", detail="il lancio è fallito: il motivo è nel log del loop")
            return
        self.finish(path, request, "spawned", session=spawned)
    # </editor-fold>

    # <editor-fold defaultstate="collapsed" desc="Sessions: liveness, receipts, wake">
    def reap(self):
        """Marks closed the sessions whose process no longer exists. The script sessions launched
        by this process are its children: poll() collects their exit, or they would stay zombies."""
        changed = False
        for session_id, entry in self.sessions().items():
            if entry["status"] != "alive":
                continue
            child = self.children.get(session_id)
            code = child.poll() if child else None
            if libstate.alive(entry["pid"], entry["starttime"]):
                continue
            entry.update(status="closed", closed=libstate.now(), close_reason="exited")
            self.children.pop(session_id, None)
            changed = True
            self.log("closed", position=entry["position"], session=session_id, reason="exited",
                     detail=f"pid {entry['pid']}" + (f", exit {code}" if code is not None else ""))
        if changed:
            self.save_registry()

    def note_received(self):
        """A row for every message that a session has read since the last cycle."""
        for path in sorted(self.paths.mailboxes.glob("*/received/[0-9]*.json")):
            if path.name in self.received_logged:
                continue
            message = libstate.read_json(path)
            if message is None:
                continue
            self.received_logged.add(path.name)
            self.log("received", position=message.get("position"), session=message.get("to_session"),
                     sender=message.get("from"), detail=message["id"])

    def unread(self, session_id, position):
        return [message for _, message in libstate.pending_messages(self.paths, position)
                if message.get("to_session") == session_id]

    def wake_sessions(self):
        """Wakes the LLM sessions that have a message waiting and have ended their turn, on the
        positions switched on. A script session reads its mailbox by itself and needs no wake."""
        candidates = [(session_id, entry) for session_id, entry in self.sessions().items()
                      if entry["status"] == "alive" and entry["type"] == "llm" and self.is_on(entry["position"])
                      and self.unread(session_id, entry["position"])]
        if not candidates:
            return
        states = libmatch.session_states()
        for session_id, entry in candidates:
            if states.get(entry["short_id"], {}).get("status") == "idle":
                self.wake(session_id, entry)

    def wake(self, session_id, entry):
        """`claude stop`, then `claude --bg --resume <sessionId>` without flags: the same session,
        with the options saved at launch, and a new process. A resume with flags, or on a session
        whose process is still alive, would start a copy (test/probe-resume.py, measures 1 and 2)."""
        subprocess.run(["claude", "stop", entry["short_id"]], capture_output=True, text=True, timeout=60)
        limit = time.monotonic() + STOP_WAIT_S
        while libstate.alive(entry["pid"], entry["starttime"]) and time.monotonic() < limit:
            time.sleep(0.5)
        text = WAKE_TEXT.replace("{request}", str(REQUEST))
        try:
            outcome = subprocess.run(["claude", "--bg", "--resume", entry["sessionId"], text], cwd=entry["cwd"],
                                     env=launch_environment(), capture_output=True, text=True, timeout=LAUNCH_TIMEOUT_S)
            hit = SHORT_ID.search(outcome.stdout + outcome.stderr)
            if outcome.returncode != 0 or not hit:
                raise LaunchError(f"claude --bg --resume exits {outcome.returncode}: {(outcome.stdout + outcome.stderr).strip()[:300]}")
            if hit.group(1) != entry["short_id"]:
                subprocess.run(["claude", "stop", hit.group(1)], capture_output=True, text=True, timeout=60)
                raise LaunchError(f"the resume started a copy, {hit.group(1)}, instead of continuing {entry['short_id']}")
            resumed = libmatch.wait_for_session(hit.group(1))
        except (OSError, subprocess.TimeoutExpired, RuntimeError, LaunchError) as error:
            entry.update(status="closed", closed=libstate.now(), close_reason="wake-failed")
            self.save_registry()
            self.log("wake-failed", position=entry["position"], session=session_id, detail=str(error))
            return
        entry.update(pid=resumed["pid"], starttime=libstate.start_time(resumed["pid"]),
                     wakes=entry.get("wakes", 0) + 1, last_wake=libstate.now())
        self.save_registry()
        self.log("wake", position=entry["position"], session=session_id, detail=f"pid {resumed['pid']}")
    # </editor-fold>

    # <editor-fold defaultstate="collapsed" desc="Status">
    def write_status(self):
        pending = [libstate.read_json(path) or {} for path in libstate.entry_files(self.paths.requests)]
        positions = {}
        for position, grant in sorted(self.grants.items()):
            positions[position] = {
                "parent": grant["parent"],
                "on": self.is_on(position),
                "alive": len(self.alive_sessions(position)),
                "max_sessions": grant["max_sessions"],
                "budget_tokens": grant["budget_tokens"],
                "queued": sum(1 for request in pending if request.get("position") == position and request.get("queued")),
                "mailbox": len(libstate.pending_messages(self.paths, position)),
            }
        libstate.write_json(self.paths.status, {
            "pid": os.getpid(), "starttime": libstate.start_time(os.getpid()), "started": self.started,
            "heartbeat": libstate.now(), "cycle_s": self.cycle_s, "code_root": str(CODE_ROOT),
            "positions": positions,
        })
    # </editor-fold>

    def start(self):
        for directory in (self.paths.requests_done, self.paths.mailboxes, self.paths.work, self.paths.records):
            directory.mkdir(parents=True, exist_ok=True)
        if not self.paths.grants.is_dir():
            print(f"no grants directory: {self.paths.grants}", file=sys.stderr)
            raise SystemExit(2)
        self.started = libstate.now()
        self.log("start", detail=f"pid {os.getpid()}, code {CODE_ROOT}, state {self.paths.state}")
        self.load_grants_at_start()
        self.registry = libstate.read_json(self.paths.sessions, {"sessions": {}})
        self.switches = {position: {"on": False, "since": self.started, "reason": "start"} for position in self.grants}
        self.save_switches()
        self.received_logged = {path.name for path in self.paths.mailboxes.glob("*/received/[0-9]*.json")}
        self.reap()

    def cycle(self):
        self.reload_grants()
        self.reap()
        self.process_requests()
        self.wake_sessions()
        self.note_received()
        self.write_status()

    def run(self):
        def stop(signal_number, _frame):
            self.stopping = signal.Signals(signal_number).name

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        self.start()
        while not self.stopping:
            self.cycle()
            time.sleep(self.cycle_s)
        self.log("stop", reason=self.stopping.lower(), detail="the sessions stay as they are")
        return 0


def human_request(paths, kind, **fields):
    """A request of the human, deposited as the view deposits it; prints the outcome."""
    request_id = libstate.deposit_request(paths, HUMAN, kind, **fields)
    state = libstate.wait_for_decision(paths, request_id, DECISION_WAIT_S)
    print(f"richiesta {request_id} · {kind} {fields.get('position')}: {libstate.describe_decision(state)}")
    return libstate.exit_code(state)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    subcommands = parser.add_subparsers(dest="command")
    run_parser = subcommands.add_parser("run", help="the loop process (default)")
    run_parser.add_argument("--cycle", type=float, default=CYCLE_S)
    switch_parser = subcommands.add_parser("switch", help="switches a position on or off, as the human")
    switch_parser.add_argument("position")
    switch_parser.add_argument("state", choices=("on", "off"))
    spawn_parser = subcommands.add_parser("spawn", help="asks a spawn on a position, as the human")
    spawn_parser.add_argument("position")
    spawn_parser.add_argument("text", nargs="*")
    arguments = sys.argv[1:]
    if not arguments or arguments[0].startswith("-") and arguments[0] not in ("-h", "--help"):
        arguments = ["run", *arguments]
    args = parser.parse_args(arguments)
    paths = libstate.Paths(STATE_ROOT)
    if args.command == "switch":
        return human_request(paths, "switch", position=args.position, on=args.state == "on")
    if args.command == "spawn":
        return human_request(paths, "spawn", position=args.position, text=" ".join(args.text).strip())
    return Loop(paths, args.cycle).run()


if __name__ == "__main__":
    sys.exit(main())
