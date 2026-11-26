#!/usr/bin/env python3
"""The loop of antfarm: one process per project that launches sessions on the positions of an org
chart, wakes them, and executes their requests.

    ANTFARM_STATE_DIR=<project> antfarm-core/bin/loop.py [run] [--cycle <s>] [--concurrency <n>] [--no-notify]
        the loop process: it runs in a terminal window and prints its log there
    ANTFARM_STATE_DIR=<project> antfarm-core/bin/loop.py switch <position> on|off
    ANTFARM_STATE_DIR=<project> antfarm-core/bin/loop.py spawn <position> [name=value...] [text...]
        a request of the human, as the view sends it: the loop decides it, the command prints the outcome

The loop reads folders of files under the state root (bin/loopstate.py says which) and writes its
log in registry/loop.jsonl. It starts in neutral: every switch off, no session starts by itself.
Switching a position on means the loop may start and wake sessions on it; switching the root on
also starts its first session, because the parent of the root is the human. Every other session
starts when someone asks a spawn on its position: the human on any declared position, a session
only on a child position of its own.

A position can declare parameters in its grant, each with a closed list of values. Whoever asks a
spawn on it passes every one of them as `name=value`, and no free text: the loop rejects a value
outside the list (`value-not-allowed`), a missing parameter (`missing-parameter`), one the grant does
not declare (`unknown-parameter`) and a text (`text-not-allowed`), before it looks at switch and cap.
The values replace {name} in the prompt, the permission rules and the command of the new session, and
the session carries them in the registry and in its `launching` and `spawn` rows. An escalation
toward a position with parameters and no live session waits in the mailbox (`held`, reason
`parameters`): the loop does not choose the values of a session in place of whoever spawns.

A session of type llm is a Claude Code session in the background (`claude --bg`), launched with
the flags of the launch template of its grant. The loop wakes it when a message waits for it in
its mailbox and the session has ended its turn: `claude stop`, then `claude --bg --resume` without
flags, which continues the same session with the options saved at launch. A session of type script
is a local process, for the tests.

A launch, a wake and a stop are operations: commands that wait on Claude Code or on a process. They
run in worker threads, at most --concurrency at once (CONCURRENCY when the argument is absent), so
that the cycle — decisions, delivery, budget, status — never waits for them; a stop starts before
the launches and the wakes that wait, and `claude agents --json`, which tells whether a session has
ended its turn, runs as an operation too. The workers only run the commands: the main thread
applies their outcomes at the next cycle, and it alone writes the registry, the switches, the
requests and the log. One operation per session at a time: a stop asked while a launch or a wake
runs follows it, and a wake does not resume if the stop arrives before the resume. A session decided
and not yet launched is in the registry with status `launching`: it holds a place of the cap of its
position and can receive a message, and the request that asked for it stays among the pending ones
with a `launching` mark until the launch ends — `request spawn` answers on the mark, so a session
that asks four spawns one after the other gets four launches that run together. A session with an operation queued or running carries
it in the `operation` field of its entry: `launch`, `wake` or `stop`, and since when. The log writes a
row when an operation is handed over (`launching`, `waking`, `stopping`) and one when it ends
(`spawn`, `wake`, `closed`), and the second one carries `started_ts`, the time the operation began.

An LLM session carries the environment of the Claude Code background service, not the loop's: the
loop launches and wakes with HOME and PATH only (match.PASSED_VARIABLES), and the service keeps the
environment of the launch that started it, the loop's or anyone else's. The loop does not prevent a
foreign environment, it records it: the `spawn` and `wake` rows of an LLM session carry
`foreign_variables`, the names in the environment of the session process outside
match.EXPECTED_VARIABLES — names only, an empty list when there are none (README, The node). The
expected names are measured on one version of Claude Code: before its first launch or wake the loop
compares `claude --version` with match.EXPECTED_VARIABLES_CLAUDE_VERSION, and when they differ it writes
an `expected-variables-stale` row that says the list is to be measured again.

An escalation goes to the mailbox of the parent position, and the loop delivers it by rule: to the
session that launched whoever escalates, if it is alive; else to another live session of the
position; else to a new session it spawns. From the root, whose parent is the human, an escalation
opens no session: it becomes an interpellation, a file under loop/interpellations/ and a desktop
notification, and it needs alternatives, recommendation and default.

A position has a cap of live sessions and a budget of tokens. A spawn over the cap waits in the
queue until a session of the position closes. A position whose sessions, alive and closed, have
spent more than its budget goes back to neutral: switch off, live sessions stopped at once. It goes
back on only after someone raises the budget in its grant; otherwise the loop switches it off again.
On a position switched off — by the human or by the budget — the launches and the wakes not yet
started are withdrawn, and a withdrawn spawn waits in the queue again.

The loop dies without killing its sessions, and a restart takes them back. An LLM session is a
child of the Claude Code background service, not of the loop; a script session runs in a process
session of its own. At every start the loop is in neutral again — it does not remember which
switches were on —, adopts every session of its registry whose process is still alive, with a
`recovered` row and no new spawn, marks closed the ones that died in the meantime, and rejects the
switch requests deposited while it was down. It takes up the operations the death interrupted: a
session left `launching` is looked for by its name, unique for every launch, and adopted with a
`recovered` row if it was born, otherwise its request is decided again; a session left in a wake is
looked for by its short id, which the resume does not change; a stop is repeated. A second loop on
the same state root does not start. On SIGTERM or SIGINT the loop starts no new operation, withdraws
the ones not started — a spawn request stays pending, a stop stays written on its session for the
next start —, waits for the running ones and applies their outcomes.

The loop rereads the grants at every cycle: a changed directory that validate-grants.py accepts
takes effect without a restart; an invalid one does not stop the loop, which keeps the last valid
set and writes the reason in the log. At start, invalid grants stop the loop with exit 2.

Standard library only. Exits 0 on SIGTERM or SIGINT, 2 without a state root, with invalid grants,
with a --concurrency that is not an integer of at least 1, or when another loop already runs on the
same state root.
"""

import argparse
import fcntl
import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import graph as libgraph  # noqa: E402
import grants as libgrants  # noqa: E402
import loopstate as libstate  # noqa: E402
import match as libmatch  # noqa: E402
import observe as libobserve  # noqa: E402

CODE_ROOT = libgraph.code_root()
STATE_ROOT = libmatch.STATE_ROOT
REQUEST = CODE_ROOT / "bin" / "request"
VALIDATOR = CODE_ROOT / "bin" / "validate-grants.py"
HUMAN = libstate.HUMAN
CYCLE_S = 1.0
CONCURRENCY = 4
# The statuses of a session that holds a place of the cap of its position: decided and being
# launched, or with a process. The third status is `closed`.
LIVE_STATUSES = ("launching", "alive")
# The marks of a request still pending: waiting in the queue, or accepted with its session being
# launched. A decided request carries neither.
PENDING_MARKS = ("queued", "launching")
BASH_TIMEOUT_MS = 900000
LAUNCH_TIMEOUT_S = 120
STOP_WAIT_S = 30
# After a restart, how long a launch that left no output is looked for by its name among the sessions
# of `claude agents --json`, before the loop takes it as never born and decides its request again.
RECOVERY_LOOKUP_S = 10
# The variable that carries the launch id in the environment of a script session.
LAUNCH_ID_VARIABLE = "ANTFARM_LAUNCH_ID"
DECISION_WAIT_S = 5
# The one line the loop writes into a session it wakes; it reaches an LLM, so it is in Italian.
WAKE_TEXT = "Il loop ti sveglia: nella tua casella c'è un messaggio per te. Leggilo con `{request} recv`."
# The package of an escalation; from the root, the first three are required (the interpellation of
# the vision of factory: «senza il default è un blocco mascherato»).
REQUIRED_PACKAGE = ("alternatives", "recommendation", "default")
PACKAGE_FIELDS = ("blocked", "alternatives", "evidence", "recommendation", "waiting_cost", "default", "deadline")
SCRIPT_TOKENS = "tokens.txt"
HALT_WAIT_S = 5


class LaunchError(Exception):
    pass


# <editor-fold defaultstate="collapsed" desc="Operations and workers">
class Operation:
    """One command that waits on Claude Code or on a process: the launch, the wake or the stop of a
    session, the recovery of a launch or of a wake that a restart interrupted, the read of the
    session states. It carries a copy of the data it needs, never the registry: running it touches
    only the processes and loop/records/<session>/. The loop applies its outcome to the registry
    and the log, because loopstate.write_json holds one writer per file in a process.

    `stop_reason` is a stop asked while the operation runs. The loop writes it and the worker reads
    it: a wake checks it between `claude stop` and the resume, and does not resume."""

    def __init__(self, kind, session_id, **data):
        self.kind = kind
        self.session_id = session_id
        self.data = data
        self.started_ts = None
        self.outcome = None
        self.stop_reason = None

    def run(self):
        """Runs the operation and keeps its outcome: a dict, with `error` when it failed. An
        operation that raises still gets an outcome, otherwise its session would stay in the
        operation for ever."""
        self.started_ts = libstate.now()
        try:
            self.outcome = RUNNERS[self.kind](self)
        except Exception as error:  # noqa: BLE001 — see the docstring
            self.outcome = {"error": f"{type(error).__name__}: {error}"}
        return self


class Workers:
    """`size` threads that run the operations, one each: at most `size` operations at once. The
    ones waiting start in order, the stops before the launches and the wakes, so that a stop for
    the budget does not wait behind the launches. An operation ended goes on the queue of the
    outcomes, which the loop empties at every cycle."""

    def __init__(self, size):
        self.condition = threading.Condition()
        self.waiting = []
        self.running = set()
        self.closed = False
        self.outcomes = queue.SimpleQueue()
        self.threads = [threading.Thread(target=self.serve, name=f"worker-{number}", daemon=True)
                        for number in range(1, size + 1)]
        for thread in self.threads:
            thread.start()

    def submit(self, operation):
        """Queues the operation; False once the workers are closed, and then it never runs."""
        with self.condition:
            if self.closed:
                return False
            stops = sum(1 for waiting in self.waiting if waiting.kind == "stop")
            self.waiting.insert(stops if operation.kind == "stop" else len(self.waiting), operation)
            self.condition.notify()
            return True

    def withdraw(self, operation):
        """True if the operation had not started: it leaves the queue and never runs."""
        with self.condition:
            if operation in self.waiting:
                self.waiting.remove(operation)
                return True
            return False

    def serve(self):
        while True:
            with self.condition:
                while not self.waiting and not self.closed:
                    self.condition.wait()
                if not self.waiting:
                    return
                operation = self.waiting.pop(0)
                self.running.add(operation)
            operation.run()
            self.outcomes.put(operation)
            with self.condition:
                self.running.discard(operation)

    def ended(self):
        """The operations ended since the last call, in the order they ended."""
        found = []
        while True:
            try:
                found.append(self.outcomes.get_nowait())
            except queue.Empty:
                return found

    def counts(self):
        with self.condition:
            return {"waiting": len(self.waiting), "running": len(self.running)}

    def close(self):
        """No operation starts from now on: returns the ones that had not started. The running ones
        go on to the end."""
        with self.condition:
            self.closed = True
            withdrawn, self.waiting = self.waiting, []
            self.condition.notify_all()
            return withdrawn


def run_logged(command, cwd, output_path, timeout_s):
    """(exit code, output) of a `claude --bg` command whose output goes to a file of the records,
    in a process session of its own. The file is not a pipe to the loop, so a `kill -9` of the loop
    does not break the command, and a Ctrl-C in the loop's terminal does not reach it: the command
    reaches its end, and the restarted loop reads the file (test/probe-launch-recovery.py)."""
    with open(output_path, "w", encoding="utf-8") as output:
        process = subprocess.Popen(command, cwd=cwd, env=libmatch.launch_environment(), stdin=subprocess.DEVNULL,
                                   stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
    try:
        code = process.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        raise LaunchError(f"{' '.join(command[:2])} does not end within {timeout_s} s") from None
    return code, Path(output_path).read_text(encoding="utf-8", errors="replace")


def claude(*arguments):
    """A `claude` command that is not a launch, in a process session of its own, like the launches."""
    return subprocess.run(["claude", *arguments], capture_output=True, text=True, timeout=60, start_new_session=True)


def run_launch(operation):
    launch, records, state = operation.data["launch"], operation.data["records"], operation.data["state"]
    if launch["type"] == "script":
        return launch_script(launch, records, state, operation.data["launch_id"])
    return launch_llm(operation.data["name"], launch, records, state)


def launch_llm(name, launch, records, state):
    """A Claude Code session in the background, with the flags of the launch template. The state
    root reaches it through the `env` block of --settings, the only environment that always
    arrives (test/probe-environment.py). The output of `claude --bg` stays in launch.out of the
    records. The outcome carries `foreign_variables`, for the spawn row and not for the registry.
    `claude --version` runs here, before the first launch of the process, and its answer stays
    cached for the loop (match.stale_expected_variables)."""
    libmatch.stale_expected_variables()
    (records / "prompt.txt").write_text(launch["prompt"], encoding="utf-8")
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
                    libgraph.STATE_VARIABLE: str(state),
                }})]
    if launch["model"]:
        command += ["--model", launch["model"]]
    command.append(launch["prompt"])
    shown = [part if part != launch["prompt"] else "<prompt.txt>" for part in command]
    libstate.write_json(records / "command.json", shown)
    try:
        code, output = run_logged(command, launch["cwd"], records / "launch.out", LAUNCH_TIMEOUT_S)
    except OSError as error:
        raise LaunchError(f"claude --bg does not run: {error}") from error
    hit = libmatch.LAUNCH_PATTERN.search(output)
    if code != 0 or not hit:
        raise LaunchError(f"claude --bg exits {code}: {output.strip()[:300]}")
    try:
        entry = libmatch.wait_for_session(hit.group(1))
    except RuntimeError as error:
        raise LaunchError(str(error)) from error
    return {**session_fields(entry, name, shown), "foreign_variables": libmatch.foreign_variables(entry["pid"])}


def session_fields(entry, name, command):
    """The fields of the registry for an LLM session, from its entry of `claude agents --json`."""
    return {"type": "llm", "pid": entry["pid"], "starttime": libstate.start_time(entry["pid"]),
            "sessionId": entry["sessionId"], "short_id": entry["id"], "name": name, "command": command}


def launch_script(launch, records, state, launch_id):
    """A local process in a session of its own: a Ctrl-C in the loop's terminal does not reach
    it, and the death of the loop does not end it. Its environment carries the launch id, which
    finds it again after a restart. The outcome carries the Popen object, which the loop keeps to
    collect the exit."""
    environment = {**libmatch.launch_environment(), libgraph.STATE_VARIABLE: str(state), LAUNCH_ID_VARIABLE: launch_id}
    try:
        with open(records / "output.txt", "w", encoding="utf-8") as output:
            process = subprocess.Popen(launch["command"], cwd=launch["cwd"], env=environment,
                                       stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                                       start_new_session=True)
    except OSError as error:
        raise LaunchError(f"{launch['command'][0]} does not start: {error}") from error
    return {"type": "script", "pid": process.pid, "starttime": libstate.start_time(process.pid),
            "command": launch["command"], "process": process}


def run_wake(operation):
    """`claude stop`, then `claude --bg --resume <sessionId>` without flags: the same session,
    with the options saved at launch, and a new process. A resume with flags, or on a session
    whose process is still alive, would start a copy (test/probe-resume.py, measures 1 and 2).
    A stop asked meanwhile is checked between the two: the session stays stopped, `stopped` in
    the outcome."""
    entry = operation.data["entry"]
    libmatch.stale_expected_variables()
    claude("stop", entry["short_id"])
    limit = time.monotonic() + STOP_WAIT_S
    while libstate.alive(entry["pid"], entry["starttime"]) and time.monotonic() < limit:
        time.sleep(0.5)
    if operation.stop_reason:
        return {"stopped": True}
    text = WAKE_TEXT.replace("{request}", str(REQUEST))
    records = Path(operation.data["records"])
    records.mkdir(parents=True, exist_ok=True)
    code, output = run_logged(["claude", "--bg", "--resume", entry["sessionId"], text], entry["cwd"],
                              records / "wake.out", LAUNCH_TIMEOUT_S)
    hit = libmatch.LAUNCH_PATTERN.search(output)
    if code != 0 or not hit:
        raise LaunchError(f"claude --bg --resume exits {code}: {output.strip()[:300]}")
    if hit.group(1) != entry["short_id"]:
        claude("stop", hit.group(1))
        raise LaunchError(f"the resume started a copy, {hit.group(1)}, instead of continuing {entry['short_id']}")
    resumed = libmatch.wait_for_session(hit.group(1))
    return {"pid": resumed["pid"], "starttime": libstate.start_time(resumed["pid"]),
            "foreign_variables": libmatch.foreign_variables(resumed["pid"])}


def run_stop(operation):
    """`claude stop` for an LLM session, SIGTERM to the process group of a script session — its
    `request recv` too, which would otherwise outlive it —, then the wait for the end of the
    process, up to HALT_WAIT_S. The outcome says whether the process is still alive."""
    entry, child = operation.data["entry"], operation.data.get("child")
    if entry["type"] == "llm":
        try:
            claude("stop", entry["short_id"])
        except (OSError, subprocess.TimeoutExpired):
            pass
    elif libstate.alive(entry["pid"], entry["starttime"]):
        try:
            os.killpg(entry["pid"], signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    limit = time.monotonic() + HALT_WAIT_S
    while libstate.alive(entry["pid"], entry["starttime"]) and time.monotonic() < limit:
        if child:
            child.poll()
        time.sleep(0.1)
    if child:
        child.poll()
    return {"alive": libstate.alive(entry["pid"], entry["starttime"])}


def run_states(_operation):
    """The states of the live sessions, from `claude agents --json`: which ones have ended their turn."""
    return {"states": libmatch.session_states()}


def wait_while_running(words, limit_s):
    """Waits until no process carries all the words among the arguments of its command line, up to
    limit_s; the ones still there at the end are killed, as a launch past LAUNCH_TIMEOUT_S is. A
    restarted loop waits so for a `claude --bg`, a resume or a `claude stop` that its previous
    process left running: the words name that command only — the unique name of a launch, `--resume`
    and a sessionId, `stop` and a short id."""
    limit = time.monotonic() + limit_s
    while libstate.processes_with_arguments(words):
        if time.monotonic() >= limit:
            for pid in libstate.processes_with_arguments(words):
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            return
        time.sleep(0.5)


def run_recover_launch(operation):
    """A session left `launching` by a previous loop: was it born? An LLM session: its `claude
    --bg` may still run, and the loop waits for it; then its short id from launch.out, or its
    name — unique, it carries the launch id — among the sessions of `claude agents --json`. A
    script session: the session leader whose environment carries the launch id. The outcome
    carries the fields of the registry, or `born` False."""
    entry, records = operation.data["entry"], Path(operation.data["records"])
    if entry["type"] == "script":
        found = libstate.session_leader_with(LAUNCH_ID_VARIABLE, entry["launch_id"]) if entry.get("launch_id") else None
        if found is None:
            return {"born": False}
        pid, starttime = found
        return {"born": True, "pid": pid, "starttime": starttime}
    name = entry.get("name")
    if not name:
        return {"born": False}
    wait_while_running(("--bg", name), LAUNCH_TIMEOUT_S)
    try:
        output = (records / "launch.out").read_text(encoding="utf-8", errors="replace")
    except OSError:
        output = ""
    command = libstate.read_json(records / "command.json")
    hit = libmatch.LAUNCH_PATTERN.search(output)
    candidate = None
    if hit and name in output:
        try:
            candidate = libmatch.wait_for_session(hit.group(1))
        except RuntimeError:
            candidate = None
    limit = time.monotonic() + RECOVERY_LOOKUP_S
    while candidate is None and time.monotonic() < limit:
        candidate = next((state for state in libmatch.session_states().values()
                          if state.get("name") == name and state.get("pid") and state.get("sessionId")), None)
        if candidate is None:
            time.sleep(1)
    if candidate is None or candidate.get("name") != name:
        return {"born": False}
    return {"born": True, **session_fields(candidate, name, command)}


def run_recover_wake(operation):
    """A session whose wake a previous loop left halfway: its `claude stop` or its resume may still
    run, and the loop waits for them; then the session by its short id, which the resume does not
    change. Found alive: the pid it has now, new if the resume happened. Not found: stopped and
    not resumed, `found` False."""
    entry = operation.data["entry"]
    wait_while_running(("stop", entry["short_id"]), LAUNCH_TIMEOUT_S)
    wait_while_running(("--resume", entry["sessionId"]), LAUNCH_TIMEOUT_S)
    state = libmatch.session_states().get(entry["short_id"])
    if not state or not state.get("pid"):
        return {"found": False}
    return {"found": True, "pid": state["pid"], "starttime": libstate.start_time(state["pid"])}


RUNNERS = {"launch": run_launch, "wake": run_wake, "stop": run_stop, "states": run_states,
           "recover-launch": run_recover_launch, "recover-wake": run_recover_wake}
# </editor-fold>


class Loop:
    def __init__(self, paths, cycle_s, notify=True, concurrency=CONCURRENCY):
        self.paths = paths
        self.cycle_s = cycle_s
        self.notify = notify
        self.concurrency = concurrency
        self.held = {}
        self.transcripts = {}
        self.transcript_missed = {}
        self.token_cache = {}
        self.cycles = 0
        self.grants = {}
        self.fingerprint = None
        self.rejected_fingerprint = None
        self.switches = {}
        self.registry = {"sessions": {}}
        self.children = {}
        self.queued = {}
        self.received_logged = set()
        self.stopping = False
        self.started = None
        self.lock_file = None
        self.expected_variables_checked = False
        self.workers = Workers(concurrency)
        # The operation of every session that has one, queued or running, until its outcome is
        # applied: one per session at a time.
        self.in_flight = {}
        self.states_operation = None
        self.states_error = None

    # <editor-fold defaultstate="collapsed" desc="Log">
    def log(self, event, position=None, session=None, request=None, sender=None, reason=None, detail=None,
            read_ts=None, foreign_variables=None, started_ts=None, parameters=None):
        """One row of registry/loop.jsonl, and the same row in clear on the terminal. `ts` is the time
        the loop writes the row, so the file stays in write order; a `received` row also carries
        `read_ts`, the time the session read the message, which can be earlier than rows written
        before it — the `closed` row of the same session included. The row that ends an operation
        carries `started_ts`, the time the operation began: from `started_ts` to `ts` runs the
        operation, plus the wait for the loop to apply its outcome. The `spawn` and `wake` rows of
        an LLM session also carry `foreign_variables` (match.foreign_variables). The rows of a spawn with
        parameters — `queued`, `launching`, `spawn`, `spawn-failed` — carry `parameters`."""
        row = {"ts": libstate.now(), "event": event, "position": position, "session": session,
               "request": request, "from": sender, "reason": reason, "detail": detail}
        if read_ts is not None:
            row["read_ts"] = read_ts
        if started_ts is not None:
            row["started_ts"] = started_ts
        if foreign_variables is not None:
            row["foreign_variables"] = foreign_variables
        if parameters:
            row["parameters"] = parameters
        self.paths.log.parent.mkdir(parents=True, exist_ok=True)
        with open(self.paths.log, "a", encoding="utf-8") as log_file:
            log_file.write(json.dumps(row, ensure_ascii=False) + "\n")
        words = [row["ts"][11:23], f"{event:<17}", position or "-", session or "",
                 " ".join(f"{name}={value}" for name, value in (parameters or {}).items()), reason or "", detail or "",
                 f"read {read_ts[11:23]}" if read_ts else "",
                 f"foreign variables {', '.join(foreign_variables)}" if foreign_variables else ""]
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
                              if position not in grants and self.live_sessions(position))
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

    def live_sessions(self, position):
        """The sessions of a position that hold a place of its cap — alive, or decided and being
        launched —, in decision order."""
        return [session_id for session_id, entry in self.sessions().items()
                if entry["position"] == position and entry["status"] in LIVE_STATUSES]

    def count(self, position, status):
        return sum(1 for entry in self.sessions().values() if entry["position"] == position and entry["status"] == status)

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

    # <editor-fold defaultstate="collapsed" desc="Spawn and operations">
    def reserve(self, position, launched_by, request_id, reason, text=None, waiting=None, parameters=None):
        """Decides a session on the position and returns (its id, the operation that launches it),
        for the caller to submit. The session enters the registry now, with status `launching`: from
        here it holds a place of the cap, and it can receive a message. A text becomes the first
        message of the session, in the position mailbox, and `waiting` — (path, message) of a
        message already in the mailbox — is addressed to it: both before the launch, so that the
        session finds them at its first `request recv`. The launch id makes the name of an LLM
        session unique, and finds the session again after a restart (run_recover_launch). The
        parameters, already checked against the grant, replace their names in the launch template,
        and the session carries them."""
        parameters = dict(parameters or {})
        session_id = self.next_session_id(position)
        values = {"request": REQUEST, "code": CODE_ROOT, "state": self.paths.state,
                  "session": session_id, "position": position}
        launch = libgrants.resolved_launch(self.grants[position], values, parameters)
        Path(launch["cwd"]).mkdir(parents=True, exist_ok=True)
        records = self.paths.records / session_id
        records.mkdir(parents=True, exist_ok=True)
        first_message = None
        if text:
            first_message = libstate.deposit_message(self.paths, position, launched_by.get("session") or HUMAN, text,
                                                     to_session=session_id)["id"]
        if waiting is not None:
            waiting[1].update(to_session=session_id, delivered=libstate.now(), rule="spawn")
            libstate.write_json(waiting[0], waiting[1])
        decided = libstate.now()
        launch_id = libstate.new_id("l")
        entry = {"id": session_id, "position": position, "type": launch["type"], "status": "launching",
                 "launch_id": launch_id, "cwd": launch["cwd"], "decided": decided, "launched_by": launched_by,
                 "request": request_id, "reason": reason, "parameters": parameters, "first_message": first_message,
                 "wakes": 0, "operation": {"kind": "launch", "since": decided}}
        if launch["type"] == "llm":
            entry["name"] = f"antfarm loop · {position} · {session_id} · {launch_id}"
        else:
            entry["command"] = launch["command"]
        self.sessions()[session_id] = entry
        self.save_registry()
        self.log("launching", position=position, session=session_id, request=request_id,
                 sender=launched_by.get("session") or launched_by["kind"], reason=reason, detail=launch["type"],
                 parameters=parameters)
        return session_id, Operation("launch", session_id, launch=launch, records=records, state=self.paths.state,
                                     name=entry.get("name"), launch_id=launch_id)

    def unreserve(self, session_id):
        """Removes a session whose launch will not happen. Its first message disappears; the other
        messages addressed to it go back to the position unassigned, and are returned as they were.
        The mailbox first, the registry after: a `kill -9` in between leaves a reservation without
        its first message, never a first message without its session."""
        entry = self.sessions()[session_id]
        unassigned = []
        for path, message in libstate.pending_messages(self.paths, entry["position"]):
            if message.get("to_session") != session_id:
                continue
            if message["id"] == entry.get("first_message"):
                path.unlink(missing_ok=True)
                continue
            unassigned.append(dict(message))
            for field in ("delivered", "rule"):
                message.pop(field, None)
            message["to_session"] = None
            libstate.write_json(path, message)
        del self.sessions()[session_id]
        self.save_registry()
        return unassigned

    def submit(self, operation):
        """Hands the operation to the workers. The operation of a session stays in `in_flight`
        until its outcome is applied; the read of the states runs one at a time. Once the loop is
        shutting down the workers take nothing more, and the operation is withdrawn at once."""
        if not self.workers.submit(operation):
            self.withdrawn(operation, "shutdown")
            return
        if operation.kind == "states":
            self.states_operation = operation
        else:
            self.in_flight[operation.session_id] = operation

    def forget(self, operation):
        if operation.kind == "states":
            self.states_operation = None
        elif self.in_flight.get(operation.session_id) is operation:
            del self.in_flight[operation.session_id]

    def apply_outcomes(self):
        """The outcomes of the operations ended since the last cycle, in the order they ended,
        written into the registry and the log: only this thread writes them."""
        for operation in self.workers.ended():
            self.forget(operation)
            handlers = {"launch": self.launched, "wake": self.woken, "stop": self.stopped,
                        "states": self.states_read, "recover-launch": self.launch_recovered,
                        "recover-wake": self.wake_recovered}
            handlers[operation.kind](operation)

    def settle(self):
        """Waits until every operation handed over has ended, and applies the outcomes; a running
        loop keeps its heartbeat meanwhile. For the shutdown, and for a probe that drives a Loop."""
        while self.in_flight or self.states_operation is not None:
            self.apply_outcomes()
            if self.in_flight or self.states_operation is not None:
                if self.started:
                    self.write_status()
                time.sleep(min(self.cycle_s, 0.2))

    def withdrawn(self, operation, reason):
        """An operation that will not run: its position was switched off (`switched-off`), or the
        loop is shutting down (`shutdown`). A launch gives back its reservation, and its spawn
        request returns among the pending ones — queued for a switch, without marks for the next
        start; a wake leaves the session as it was; a stop and a recovery stay written on the
        session, and the next start runs them again."""
        self.forget(operation)
        entry = self.sessions().get(operation.session_id) if operation.session_id else None
        if entry is None:
            return
        detail = {"launch": "launch: the reservation is given back", "wake": "wake: the session stays as it is",
                  "stop": "stop: the next start repeats it"}.get(operation.kind, f"{operation.kind}: the next start repeats it")
        if operation.kind == "launch":
            self.unreserve(operation.session_id)
        elif operation.kind == "wake":
            entry.pop("operation", None)
            self.save_registry()
        self.log("withdrawn", position=entry["position"], session=operation.session_id, request=entry.get("request"),
                 reason=reason, detail=detail)
        if operation.kind != "launch" or entry["reason"] != "request":
            return
        found = self.pending_request(entry["request"])
        if found is None:
            return
        path, request = found
        if reason == "switched-off":
            self.queue(path, request, "switched-off", f"l'interruttore di {entry['position']} è spento")
        else:
            libstate.write_json(path, {field: value for field, value in request.items() if field not in PENDING_MARKS})

    def withdraw_position(self, position):
        """On a position switched off the loop stops launching and waking: the launches and the
        wakes not yet started are withdrawn. The running ones reach their end."""
        for session_id in self.live_sessions(position):
            operation = self.in_flight.get(session_id)
            if operation is not None and operation.kind in ("launch", "wake") and self.workers.withdraw(operation):
                self.withdrawn(operation, "switched-off")

    def launched(self, operation):
        """The reservation becomes a live session, or disappears if the launch failed: then a
        spawn request is decided as failed, and the message of an escalation stays held in the
        mailbox, for the delivery to try again. A stop asked during the launch follows it."""
        session_id = operation.session_id
        entry = self.sessions()[session_id]
        outcome = dict(operation.outcome)
        sender = entry["launched_by"].get("session") or entry["launched_by"]["kind"]
        if entry["type"] == "llm":
            self.note_stale_expected_variables()
        if "error" in outcome:
            unassigned = self.unreserve(session_id)
            self.log("spawn-failed", position=entry["position"], session=session_id, request=entry["request"],
                     sender=sender, reason=entry["reason"], detail=outcome["error"], started_ts=operation.started_ts,
                     parameters=entry.get("parameters"))
            if entry["reason"] == "request":
                self.finish_pending(entry["request"], "failed", reason="launch-failed",
                                    detail="il lancio è fallito: il motivo è nel log del loop")
            for message in unassigned:
                if message.get("rule") == "spawn":
                    self.hold(message, entry["position"], "launch-failed", "the launch of a session for the message failed")
            return
        process = outcome.pop("process", None)
        if process is not None:
            self.children[session_id] = process
        foreign = outcome.pop("foreign_variables", None)
        self.make_alive(entry, outcome)
        self.log("spawn", position=entry["position"], session=session_id, request=entry["request"], sender=sender,
                 reason=entry["reason"], detail=f"{entry['type']} pid {entry['pid']}", foreign_variables=foreign,
                 started_ts=operation.started_ts, parameters=entry.get("parameters"))
        if entry["reason"] == "request":
            self.finish_pending(entry["request"], "spawned", session=session_id)
        if operation.stop_reason:
            self.stop_session(session_id, operation.stop_reason)

    def make_alive(self, entry, fields):
        entry.update(fields, status="alive", launched=libstate.now())
        for field in ("operation", "first_message"):
            entry.pop(field, None)
        self.save_registry()

    def note_stale_expected_variables(self):
        """Once per process, at the first launch or wake of an LLM session: when `claude --version`
        is not the version of match.EXPECTED_VARIABLES, an `expected-variables-stale` row in the log
        and on the terminal. The launch goes on. The operation has already run `claude --version`,
        and match.stale_expected_variables keeps the answer."""
        if self.expected_variables_checked:
            return
        self.expected_variables_checked = True
        line = libmatch.stale_expected_variables()
        if line:
            self.log("expected-variables-stale", detail=line)

    def shut_down(self):
        """On SIGTERM or SIGINT: no operation starts from now on, the ones not started are
        withdrawn, the running ones reach their end and their outcomes are applied."""
        for operation in self.workers.close():
            self.withdrawn(operation, "shutdown")
        self.settle()
    # </editor-fold>

    # <editor-fold defaultstate="collapsed" desc="Requests">
    def finish(self, path, request, outcome, **fields):
        """Moves a request among the decided ones, with its outcome."""
        decided = {**request, "outcome": outcome, "decided": libstate.now(), **fields}
        for mark in PENDING_MARKS:
            decided.pop(mark, None)
        libstate.write_json(self.paths.requests_done / path.name, decided)
        path.unlink(missing_ok=True)
        self.queued.pop(request.get("id"), None)

    def pending_request(self, request_id):
        """(path, request) of a request still among the pending ones, None if there is none."""
        for path in self.paths.requests.glob(f"[0-9]*-{request_id}.json"):
            request = libstate.read_json(path)
            if request is not None:
                return path, request
        return None

    def finish_pending(self, request_id, outcome, **fields):
        """Decides a request that waited for an operation, if it is still pending."""
        found = self.pending_request(request_id)
        if found is not None:
            self.finish(*found, outcome, **fields)

    def mark_launching(self, path, request, session_id):
        """The request accepted and its session being launched: it stays among the pending ones,
        with the mark, until the launch ends. `request spawn` answers on the mark."""
        self.queued.pop(request["id"], None)
        marked = {field: value for field, value in request.items() if field not in PENDING_MARKS}
        libstate.write_json(path, {**marked, "launching": {"session": session_id, "since": libstate.now()}})

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
        marked = {field: value for field, value in request.items() if field not in PENDING_MARKS}
        libstate.write_json(path, {**marked, "queued": {"reason": reason, "detail": detail, "since": libstate.now()}})
        self.log("queued", position=request.get("position"), request=request["id"], sender=request.get("from"),
                 reason=reason, detail=f"{request.get('kind')}: {detail}",
                 parameters=request.get("parameters") if isinstance(request.get("parameters"), dict) else None)

    def process_requests(self):
        """Decides every pending request, except the ones whose session is being launched: their
        outcome is the outcome of the launch."""
        launching = {entry["request"] for entry in self.sessions().values()
                     if entry["status"] == "launching" and entry["reason"] == "request"}
        for path in libstate.entry_files(self.paths.requests):
            request = libstate.read_json(path)
            if request is None:
                self.reject(path, {"id": path.stem.split("-", 1)[-1]}, "unreadable", "il file non è JSON leggibile")
                continue
            if request.get("id") in launching:
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
        handlers = {"switch": self.request_switch, "spawn": self.request_spawn, "escalate": self.request_escalate}
        handler = handlers.get(request.get("kind"))
        if handler is None:
            self.reject(path, request, "unknown-kind", f"il loop non conosce il tipo di richiesta {request.get('kind')!r}")
            return
        handler(path, request, sender_entry)

    def request_switch(self, path, request, sender_entry):
        """Only the human switches. Switching the root on also starts its first session, if none
        of its sessions is live: the parent of the root is the human, and the click is its request.
        The request is applied as soon as the session is decided; the launch runs after."""
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
        if not on:
            self.withdraw_position(position)
        if on and self.over_budget(position):
            self.finish(path, request, "halted")
            return
        spawned, operation = None, None
        if on and self.grants[position]["parent"] == HUMAN and not self.live_sessions(position):
            spawned, operation = self.reserve(position, {"kind": HUMAN, "session": None}, request["id"], "switch")
        self.finish(path, request, "applied", session=spawned)
        if operation is not None:
            self.submit(operation)

    def request_spawn(self, path, request, sender_entry):
        """The human asks a spawn on any declared position, a session only on a child position of
        its own. The parameters and the text must fit the grant of the position
        (grants.parameter_errors), or the spawn is rejected with the reason. A spawn on a position
        switched off, or with as many live sessions as its cap, waits in the queue: the loop retries
        it at every cycle. An accepted spawn stays pending with the `launching` mark, and the outcome
        of the launch decides it."""
        position = request.get("position")
        if position not in self.grants:
            self.reject(path, request, "undeclared-position", f"{position} non è una posizione dell'organigramma")
            return
        if sender_entry is not None and position not in libgrants.children(self.grants, sender_entry["position"]):
            self.reject(path, request, "not-a-child",
                        f"{position} non è una posizione figlia di {sender_entry['position']}")
            return
        parameters = request.get("parameters") or {}
        if not isinstance(parameters, dict) or not all(isinstance(value, str) for value in parameters.values()):
            self.reject(path, request, "unreadable-parameters", "i parametri devono essere coppie nome=valore di testo")
            return
        problem = libgrants.parameter_errors(self.grants[position], parameters, str(request.get("text") or "").strip())
        if problem is not None:
            self.reject(path, request, *problem)
            return
        if not self.is_on(position):
            self.queue(path, request, "switched-off", f"l'interruttore di {position} è spento")
            return
        live = len(self.live_sessions(position))
        if live >= self.grants[position]["max_sessions"]:
            self.queue(path, request, "cap", f"{position} ha già {live} sessioni vive, il tetto è {self.grants[position]['max_sessions']}")
            return
        launched_by = {"kind": "session", "session": request["from"]} if sender_entry else {"kind": HUMAN, "session": None}
        session_id, operation = self.reserve(position, launched_by, request["id"], "request", text=request.get("text"),
                                             parameters=parameters)
        self.mark_launching(path, request, session_id)
        self.submit(operation)

    def request_escalate(self, path, request, sender_entry):
        """The text goes to the mailbox of the parent position, unassigned: `deliver` picks the
        session. From the root it becomes an interpellation of the human."""
        if sender_entry is None:
            self.reject(path, request, "not-a-session", "l'umano non scala: è il genitore della radice")
            return
        text = (request.get("text") or "").strip()
        package = {field: value for field, value in (request.get("package") or {}).items() if value}
        if not text and not package:
            self.reject(path, request, "empty", "niente da scalare: né testo né pacchetto")
            return
        position = sender_entry["position"]
        parent = self.grants[position]["parent"]
        if parent == HUMAN:
            self.interpellate(path, request, sender_entry, text, package)
            return
        message = libstate.deposit_message(self.paths, parent, request["from"], text, kind="escalation",
                                           launcher=sender_entry["launched_by"].get("session"),
                                           request=request["id"], package=package or None)
        self.log("escalate", position=parent, session=request["from"], request=request["id"], sender=request["from"],
                 detail=f"{message['id']} from {position}")
        self.finish(path, request, "deposited", position=parent, message=message["id"])

    def interpellate(self, path, request, sender_entry, text, package):
        """An escalation from the root opens no session: it becomes a file for the human, with a
        desktop notification. Without alternatives, recommendation and default it is rejected: an
        interpellation without a default is a block in disguise. Default and deadline stay written
        in the file; nobody executes them."""
        missing = [field for field in REQUIRED_PACKAGE if not package.get(field)]
        if missing:
            self.reject(path, request, "incomplete-package",
                        "dalla radice un'escalation sale all'umano solo con alternative, raccomandazione e default: "
                        f"mancano {', '.join(missing)}")
            return
        interpellation = {"id": libstate.new_id("i"), "ts": libstate.now(), "from": request["from"],
                          "position": sender_entry["position"], "request": request["id"], "text": text,
                          **{field: package.get(field) for field in PACKAGE_FIELDS}, "status": "open"}
        libstate.deposit(self.paths.interpellations, interpellation)
        notified = self.notify_human(interpellation)
        self.log("interpellation", position=sender_entry["position"], session=request["from"], request=request["id"],
                 sender=request["from"], detail=f"{interpellation['id']} · notify {notified}")
        self.finish(path, request, "interpellation", interpellation=interpellation["id"])

    def notify_human(self, interpellation):
        """A desktop notification outside the view (`notify-send`); returns how it went."""
        if not self.notify:
            return "off"
        body = interpellation["blocked"] or interpellation["text"] or ""
        body += f"\nraccomandazione: {interpellation['recommendation']} · default: {interpellation['default']}"
        try:
            outcome = subprocess.run(["notify-send", "--app-name=antfarm",
                                      f"antfarm · interpellazione da {interpellation['position']}", body],
                                     capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired) as error:
            return f"failed: {error}"
        return "sent" if outcome.returncode == 0 else f"failed: exit {outcome.returncode} {outcome.stderr.strip()}"
    # </editor-fold>

    # <editor-fold defaultstate="collapsed" desc="Delivery">
    def deliver(self):
        """Assigns every unassigned message of every mailbox, oldest first. A message addressed to a
        session that closed without reading it goes back to the position and is assigned again."""
        changed = False
        for position in sorted(self.grants):
            for path, message in libstate.pending_messages(self.paths, position):
                target = message.get("to_session")
                if target is not None:
                    entry = self.sessions().get(target)
                    if entry is not None and entry["status"] in LIVE_STATUSES:
                        continue
                    message["to_session"] = None
                    message["undelivered"] = [*message.get("undelivered", []), target]
                    libstate.write_json(path, message)
                    self.log("undelivered", position=position, session=target, sender=message.get("from"),
                             reason="session-closed", detail=message["id"])
                changed = self.assign(path, message, position) or changed
        return changed

    def assign(self, path, message, position):
        """The rule of delivery: to the session that launched whoever escalates, if it is live;
        else to the first live session of the position — a session being launched counts —; else
        to a new session. On a position switched off the message stays in the mailbox, and so it
        does when the launch fails, and when a new session would need parameters."""
        if not self.is_on(position):
            self.hold(message, position, "switched-off", f"the switch of {position} is off")
            return False
        live = self.live_sessions(position)
        if not live and self.grants[position].get("parameters"):
            self.hold(message, position, "parameters",
                      f"a session of {position} needs its parameters, and the loop does not choose them for a message")
            return False
        launcher = message.get("launcher")
        operation = None
        if live:
            target, rule = (launcher, "launcher") if launcher in live else (live[0], "alive-session")
            message.update(to_session=target, delivered=libstate.now(), rule=rule)
            libstate.write_json(path, message)
        else:
            target, operation = self.reserve(position, {"kind": "loop", "session": None}, message.get("request"),
                                             "escalation", waiting=(path, message))
            rule = "spawn"
        self.held.pop(message["id"], None)
        self.log("deliver", position=position, session=target, sender=message.get("from"), reason=rule, detail=message["id"])
        if operation is not None:
            self.submit(operation)
        return True

    def hold(self, message, position, reason, detail):
        """The message stays in the mailbox; the row is written once per reason."""
        if self.held.get(message["id"]) == reason:
            return
        self.held[message["id"]] = reason
        self.log("held", position=position, sender=message.get("from"), reason=reason, detail=f"{message['id']}: {detail}")
    # </editor-fold>

    # <editor-fold defaultstate="collapsed" desc="Budget and neutral">
    def session_tokens(self, entry):
        """The tokens a session has spent: for an LLM session the four `usage` fields of its
        transcript, each `message.id` counted once (observe.py); for a script session the number
        it writes in tokens.txt of its working directory. A session being launched has spent nothing.
        The path of a transcript, once found, stays in memory; a transcript not found yet is looked
        for at most once per cycle, because the lookup scans every directory of ~/.claude/projects."""
        if entry["status"] == "launching":
            return 0
        if entry["type"] == "script":
            try:
                return int((Path(entry["cwd"]) / SCRIPT_TOKENS).read_text(encoding="utf-8").strip() or 0)
            except (OSError, ValueError):
                return 0
        session_id = entry["sessionId"]
        if session_id not in self.transcripts:
            if self.transcript_missed.get(session_id) == self.cycles:
                return 0
            path = libmatch.transcript(session_id)
            if path is None:
                self.transcript_missed[session_id] = self.cycles
                return 0
            self.transcript_missed.pop(session_id, None)
            self.transcripts[session_id] = path
        path = self.transcripts[session_id]
        try:
            status = path.stat()
        except OSError:
            return 0
        key = (status.st_size, status.st_mtime_ns)
        cached = self.token_cache.get(session_id)
        if cached is None or cached[0] != key:
            tokens = libobserve.transcript_tokens(path)
            self.token_cache[session_id] = cached = (key, tokens)
        return cached[1]

    def consumption(self, position):
        """The tokens of all the sessions of the position, alive and closed."""
        return sum(self.session_tokens(entry) for entry in self.sessions().values() if entry["position"] == position)

    def over_budget(self, position):
        """True, and the position back in neutral, if its sessions have spent more than its budget."""
        spent = self.consumption(position)
        budget = self.grants[position]["budget_tokens"]
        if spent <= budget:
            return False
        self.halt(position, "budget", f"{spent} tokens spent, budget {budget}")
        return True

    def account(self):
        for position in sorted(self.grants):
            if self.is_on(position):
                self.over_budget(position)

    def halt(self, position, reason, detail):
        """Back to neutral for one position: switch off, and its live sessions stopped at once. The
        one function for every trigger: today the budget, tomorrow an algedonic signal."""
        self.set_switch(position, False, reason)
        self.log("halt", position=position, reason=reason, detail=detail)
        for session_id in self.live_sessions(position):
            self.stop_session(session_id, f"halt-{reason}")

    def stop_session(self, session_id, reason):
        """Hands the stop of a live session to an operation (run_stop); the session is closed when
        its outcome is applied. One operation per session at a time: a launch or a wake not yet
        started is withdrawn, and a withdrawn launch leaves nothing to stop; an operation already
        running reaches its end, and the stop follows it — a wake does not resume, if the stop
        arrives before the resume. The stop that waits is written on the session too, for a
        restart."""
        entry = self.sessions()[session_id]
        operation = self.in_flight.get(session_id)
        if operation is not None and operation.kind == "stop":
            return
        if operation is not None:
            if operation.kind in ("launch", "wake") and self.workers.withdraw(operation):
                self.withdrawn(operation, "switched-off")
                if operation.kind == "launch":
                    return
            else:
                operation.stop_reason = reason
                entry["operation"]["stop"] = reason
                self.save_registry()
                self.log("stopping", position=entry["position"], session=session_id, reason=reason,
                         detail=f"after the {operation.kind} in progress")
                return
        entry["operation"] = {"kind": "stop", "since": libstate.now(), "reason": reason}
        self.save_registry()
        self.log("stopping", position=entry["position"], session=session_id, reason=reason)
        self.submit(Operation("stop", session_id, entry=dict(entry), child=self.children.get(session_id), reason=reason))

    def stopped(self, operation):
        """The session is closed once its process is gone. A process still alive after the stop keeps
        the session alive, with its stop written on it: a `stop-failed` row at the first failure, and
        the stop handed over again, ahead of the launches and the wakes that wait, until the pair
        pid and starttime no longer exists. A session closed while its process lives would be a live
        session that no budget looks at."""
        session_id = operation.session_id
        entry = self.sessions()[session_id]
        reason = operation.data["reason"]
        alive = operation.outcome["alive"] if "alive" in operation.outcome \
            else libstate.alive(entry["pid"], entry["starttime"])
        if alive:
            mark = entry.setdefault("operation", {"kind": "stop", "since": libstate.now(), "reason": reason})
            mark["attempts"] = mark.get("attempts", 1) + 1
            self.save_registry()
            if mark["attempts"] == 2:
                self.log("stop-failed", position=entry["position"], session=session_id, reason=reason,
                         detail=f"pid {entry['pid']} alive {HALT_WAIT_S} s after the stop: the stop is repeated",
                         started_ts=operation.started_ts)
            self.submit(Operation("stop", session_id, entry=dict(entry), child=self.children.get(session_id),
                                  reason=reason))
            return
        self.children.pop(session_id, None)
        entry.pop("operation", None)
        entry.update(status="closed", closed=libstate.now(), close_reason=reason)
        self.save_registry()
        self.log("closed", position=entry["position"], session=session_id, reason=reason,
                 detail=f"pid {entry['pid']}", started_ts=operation.started_ts)
    # </editor-fold>

    # <editor-fold defaultstate="collapsed" desc="Sessions: liveness, receipts, wake">
    def reap(self):
        """Marks closed the sessions whose process no longer exists. The script sessions launched
        by this process are its children: poll() collects their exit, or they would stay zombies.
        A session with an operation is the operation's: a wake ends its process on purpose."""
        changed = False
        for session_id, entry in self.sessions().items():
            if entry["status"] != "alive" or entry.get("operation"):
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
        """A row for every message that a session has read and that the log does not carry yet, with
        `read_ts`: the time of the read, which the reader writes in the message file
        (loopstate.take_message). The row comes at the cycle after the read, and so it can follow
        the `closed` row of a session that read and then ended. A read made while the loop was down
        gets its row at the first cycle after the restart, with reason `before-start`. A file that
        does not carry the time yet is one the reader has moved and not yet rewritten: it waits
        for the next cycle."""
        for path in sorted(self.paths.mailboxes.glob("*/received/[0-9]*.json")):
            message = libstate.read_json(path)
            if message is None or message.get("id") in self.received_logged or not message.get("received"):
                continue
            self.received_logged.add(message["id"])
            before_start = message["received"] < self.started
            self.log("received", position=message.get("position"), session=message.get("to_session"),
                     sender=message.get("from"), reason="before-start" if before_start else None, detail=message["id"],
                     read_ts=message["received"])

    def logged_receipts(self):
        """The ids of the messages whose read the log already carries: the log is the record."""
        receipts = set()
        try:
            with open(self.paths.log, encoding="utf-8") as log_file:
                for line in log_file:
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if row.get("event") == "received" and row.get("detail"):
                        receipts.add(row["detail"])
        except OSError:
            pass
        return receipts

    def unread(self, session_id, position):
        return [message for _, message in libstate.pending_messages(self.paths, position)
                if message.get("to_session") == session_id]

    def wake_candidates(self):
        """The LLM sessions to wake if they have ended their turn: alive, without an operation, on a
        position switched on, with a message waiting. A script session reads its mailbox by itself
        and needs no wake."""
        return [session_id for session_id, entry in self.sessions().items()
                if entry["status"] == "alive" and entry["type"] == "llm" and not entry.get("operation")
                and self.is_on(entry["position"]) and self.unread(session_id, entry["position"])]

    def wake_sessions(self):
        """Asks the states of the sessions to an operation (run_states), when a session waits for a
        wake and no read of the states is under way; states_read wakes the ones that have ended
        their turn."""
        if self.states_operation is None and self.wake_candidates():
            self.submit(Operation("states", None))

    def states_read(self, operation):
        if self.stopping:
            return
        if "error" in operation.outcome:
            if self.states_error != operation.outcome["error"]:
                self.states_error = operation.outcome["error"]
                self.log("states-failed", detail=f"claude agents --json: {operation.outcome['error']}")
            return
        self.states_error = None
        states = operation.outcome["states"]
        for session_id in self.wake_candidates():
            if states.get(self.sessions()[session_id]["short_id"], {}).get("status") == "idle":
                self.wake(session_id)

    def wake(self, session_id):
        """Hands the wake of a session to an operation (run_wake)."""
        entry = self.sessions()[session_id]
        entry["operation"] = {"kind": "wake", "since": libstate.now()}
        self.save_registry()
        self.log("waking", position=entry["position"], session=session_id)
        self.submit(Operation("wake", session_id, entry=dict(entry), records=self.paths.records / session_id))

    def woken(self, operation):
        """The session continues with a new process, or is closed if the wake failed, or if a stop
        arrived before the resume — closed only if `claude stop` ended its process, otherwise the
        stop goes on as an operation of its own. A stop asked after the resume follows the wake."""
        session_id = operation.session_id
        entry = self.sessions()[session_id]
        outcome = operation.outcome
        self.note_stale_expected_variables()
        entry.pop("operation", None)
        if outcome.get("stopped") and libstate.alive(entry["pid"], entry["starttime"]):
            self.stop_session(session_id, operation.stop_reason)
            return
        if outcome.get("stopped"):
            entry.update(status="closed", closed=libstate.now(), close_reason=operation.stop_reason)
            self.save_registry()
            self.log("closed", position=entry["position"], session=session_id, reason=operation.stop_reason,
                     detail=f"pid {entry['pid']}, stopped before the resume of its wake", started_ts=operation.started_ts)
            return
        if "error" in outcome:
            entry.update(status="closed", closed=libstate.now(), close_reason="wake-failed")
            self.save_registry()
            self.log("wake-failed", position=entry["position"], session=session_id, detail=outcome["error"],
                     started_ts=operation.started_ts)
            return
        entry.update(pid=outcome["pid"], starttime=outcome["starttime"], wakes=entry.get("wakes", 0) + 1,
                     last_wake=libstate.now())
        self.save_registry()
        self.log("wake", position=entry["position"], session=session_id, detail=f"pid {outcome['pid']}",
                 foreign_variables=outcome["foreign_variables"], started_ts=operation.started_ts)
        if operation.stop_reason:
            self.stop_session(session_id, operation.stop_reason)
    # </editor-fold>

    # <editor-fold defaultstate="collapsed" desc="Status">
    def write_status(self):
        pending = [libstate.read_json(path) or {} for path in libstate.entry_files(self.paths.requests)]
        positions = {}
        for position, grant in sorted(self.grants.items()):
            positions[position] = {
                "parent": grant["parent"],
                "on": self.is_on(position),
                "alive": self.count(position, "alive"),
                "launching": self.count(position, "launching"),
                "max_sessions": grant["max_sessions"],
                "budget_tokens": grant["budget_tokens"],
                "tokens": self.consumption(position),
                "queued": sum(1 for request in pending if request.get("position") == position and request.get("queued")),
                "mailbox": len(libstate.pending_messages(self.paths, position)),
            }
        libstate.write_json(self.paths.status, {
            "pid": os.getpid(), "starttime": libstate.start_time(os.getpid()), "started": self.started,
            "heartbeat": libstate.now(), "cycle_s": self.cycle_s, "concurrency": self.concurrency,
            "operations": self.workers.counts(), "code_root": str(CODE_ROOT),
            "positions": positions,
            "interpellations_open": sum(1 for path in libstate.entry_files(self.paths.interpellations)
                                        if (libstate.read_json(path) or {}).get("status") == "open"),
        })
    # </editor-fold>

    # <editor-fold defaultstate="collapsed" desc="Start and restart">
    def take_lock(self):
        """One loop per state root: an exclusive lock on loop/loop.lock, held for the whole life of
        the process and released by the kernel when it dies, `kill -9` included. The children do
        not inherit it: subprocess closes the descriptors it does not pass."""
        self.lock_file = open(self.paths.lock, "a", encoding="utf-8")
        try:
            fcntl.flock(self.lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            other = (libstate.read_json(self.paths.status) or {}).get("pid")
            print(f"another loop already runs on {self.paths.state} (pid {other}): this one does not start",
                  file=sys.stderr)
            raise SystemExit(2)

    def adopt(self):
        """The sessions of the registry that a previous loop left alive: a `recovered` row for each
        one whose process still exists, untouched; the ones that died meanwhile are marked closed.
        The ones it left in an operation take it up again: a launch and a wake are recovered by an
        operation that finds out what they left (launch_recovered, wake_recovered), a stop is
        repeated."""
        changed = False
        for session_id, entry in list(self.sessions().items()):
            operation = entry.get("operation") or {}
            if entry["status"] == "launching":
                self.submit(Operation("recover-launch", session_id, entry=dict(entry),
                                      records=self.paths.records / session_id))
                continue
            if entry["status"] != "alive":
                continue
            if operation.get("kind") == "wake":
                self.submit(Operation("recover-wake", session_id, entry=dict(entry)))
                continue
            if operation.get("kind") == "stop":
                del entry["operation"]
                self.stop_session(session_id, operation["reason"])
                continue
            if libstate.alive(entry["pid"], entry["starttime"]):
                self.log("recovered", position=entry["position"], session=session_id,
                         detail=f"{entry['type']} pid {entry['pid']}, launched {entry['launched']}")
                continue
            entry.update(status="closed", closed=libstate.now(), close_reason="dead-at-restart")
            changed = True
            self.log("closed", position=entry["position"], session=session_id, reason="dead-at-restart",
                     detail=f"pid {entry['pid']}")
        if changed:
            self.save_registry()

    def launch_recovered(self, operation):
        """A launch that a restart interrupted. Born: the session is adopted with a `recovered` row,
        and its request decided as spawned. Not born: the reservation is given back, and its request
        stays among the pending ones without marks, to be decided again — one launch more, never a
        second session for the same request, because the session would have been found by its
        name. An error of the recovery keeps the reservation and tries again."""
        session_id = operation.session_id
        entry = self.sessions()[session_id]
        outcome = dict(operation.outcome)
        stop = (entry.get("operation") or {}).get("stop") or operation.stop_reason
        if "error" in outcome:
            self.log("recover-failed", position=entry["position"], session=session_id, detail=outcome["error"],
                     started_ts=operation.started_ts)
            self.submit(Operation("recover-launch", session_id, entry=dict(entry), records=operation.data["records"]))
            return
        if outcome.pop("born"):
            self.make_alive(entry, outcome)
            self.log("recovered", position=entry["position"], session=session_id, request=entry["request"],
                     reason="launch-interrupted", detail=f"{entry['type']} pid {entry['pid']}",
                     started_ts=operation.started_ts)
            if entry["reason"] == "request":
                self.finish_pending(entry["request"], "spawned", session=session_id)
            if stop:
                self.stop_session(session_id, stop)
            return
        self.unreserve(session_id)
        self.log("withdrawn", position=entry["position"], session=session_id, request=entry["request"],
                 reason="not-born", detail="the launch interrupted by the restart left no session",
                 started_ts=operation.started_ts)
        found = self.pending_request(entry["request"]) if entry["reason"] == "request" else None
        if found is not None:
            path, request = found
            libstate.write_json(path, {field: value for field, value in request.items() if field not in PENDING_MARKS})

    def wake_recovered(self, operation):
        """A wake that a restart interrupted. The session found by its short id: adopted with the
        pid it has now, which is new if the resume happened. Not found: stopped and not resumed,
        it is closed, and its message goes back to the position."""
        session_id = operation.session_id
        entry = self.sessions()[session_id]
        outcome = operation.outcome
        stop = (entry.get("operation") or {}).get("stop") or operation.stop_reason
        if "error" in outcome:
            self.log("recover-failed", position=entry["position"], session=session_id, detail=outcome["error"],
                     started_ts=operation.started_ts)
            self.submit(Operation("recover-wake", session_id, entry=dict(entry)))
            return
        entry.pop("operation", None)
        if not outcome["found"]:
            entry.update(status="closed", closed=libstate.now(), close_reason="wake-interrupted")
            self.save_registry()
            self.log("closed", position=entry["position"], session=session_id, reason="wake-interrupted",
                     detail=f"pid {entry['pid']}, stopped and not resumed", started_ts=operation.started_ts)
            return
        resumed = (outcome["pid"], outcome["starttime"]) != (entry["pid"], entry["starttime"])
        entry.update(pid=outcome["pid"], starttime=outcome["starttime"])
        if resumed:
            entry.update(wakes=entry.get("wakes", 0) + 1, last_wake=libstate.now())
        self.save_registry()
        self.log("recovered", position=entry["position"], session=session_id, reason="wake-interrupted",
                 detail=f"llm pid {entry['pid']}, " + ("resumed" if resumed else "not stopped"),
                 started_ts=operation.started_ts)
        if stop:
            self.stop_session(session_id, stop)

    def close_launched_requests(self):
        """A request still marked `launching` whose session is alive: the previous loop died between
        the registration of the session and the decision of the request. It is decided now."""
        for path in libstate.entry_files(self.paths.requests):
            request = libstate.read_json(path) or {}
            session_id = (request.get("launching") or {}).get("session")
            entry = self.sessions().get(session_id)
            if entry is not None and entry["status"] == "alive" and entry.get("request") == request.get("id"):
                self.finish(path, request, "spawned", session=session_id)

    def reject_stale_switches(self):
        """A switch request deposited while the loop was down would turn a position on at restart:
        the loop starts in neutral, so it rejects them. The other pending requests stay: the
        switches are all off, and they wait in the queue."""
        for path in libstate.entry_files(self.paths.requests):
            request = libstate.read_json(path)
            if request is not None and request.get("kind") == "switch":
                self.reject(path, request, "stale", "depositata prima dell'avvio del loop, che riparte in folle")

    def start(self):
        for directory in (self.paths.requests_done, self.paths.mailboxes, self.paths.work, self.paths.records):
            directory.mkdir(parents=True, exist_ok=True)
        self.take_lock()
        if not self.paths.grants.is_dir():
            print(f"no grants directory: {self.paths.grants}", file=sys.stderr)
            raise SystemExit(2)
        self.started = libstate.now()
        self.log("start", detail=f"pid {os.getpid()}, code {CODE_ROOT}, state {self.paths.state}")
        self.load_grants_at_start()
        self.registry = libstate.read_json(self.paths.sessions, {"sessions": {}})
        self.switches = {position: {"on": False, "since": self.started, "reason": "start"} for position in self.grants}
        self.save_switches()
        self.received_logged = self.logged_receipts()
        self.adopt()
        self.close_launched_requests()
        self.reject_stale_switches()
    # </editor-fold>

    def cycle(self):
        self.cycles += 1
        self.apply_outcomes()
        self.reload_grants()
        self.reap()
        self.account()
        self.process_requests()
        self.deliver()
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
        self.shut_down()
        self.log("stop", reason=self.stopping.lower(), detail="the sessions stay as they are")
        return 0


def at_least_one(text):
    """An integer of at least 1, for --concurrency; argparse turns the error into exit 2."""
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an integer: {text!r}") from None
    if value < 1:
        raise argparse.ArgumentTypeError(f"at least 1: {value}")
    return value


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
    run_parser.add_argument("--concurrency", type=at_least_one, default=CONCURRENCY,
                            help=f"the most launches, wakes and stops running at once (default {CONCURRENCY})")
    run_parser.add_argument("--no-notify", action="store_true", help="no desktop notification for interpellations")
    switch_parser = subcommands.add_parser("switch", help="switches a position on or off, as the human")
    switch_parser.add_argument("position")
    switch_parser.add_argument("state", choices=("on", "off"))
    spawn_parser = subcommands.add_parser("spawn", help="asks a spawn on a position, as the human")
    spawn_parser.add_argument("position")
    spawn_parser.add_argument("words", nargs="*", metavar="name=value... text",
                              help="the parameters of the position, then the text")
    arguments = sys.argv[1:]
    if not arguments or arguments[0].startswith("-") and arguments[0] not in ("-h", "--help"):
        arguments = ["run", *arguments]
    args = parser.parse_args(arguments)
    paths = libstate.Paths(STATE_ROOT)
    if args.command == "switch":
        return human_request(paths, "switch", position=args.position, on=args.state == "on")
    if args.command == "spawn":
        try:
            parameters, text = libstate.spawn_arguments(args.words)
        except ValueError as error:
            parser.error(str(error))
        return human_request(paths, "spawn", position=args.position, parameters=parameters, text=text)
    return Loop(paths, args.cycle, notify=not args.no_notify, concurrency=args.concurrency).run()


if __name__ == "__main__":
    sys.exit(main())
