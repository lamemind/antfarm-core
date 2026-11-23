"""State of the loop on disk, shared by the loop process, the request command and the view.

Everything lives under the state root (ANTFARM_STATE_DIR):

    grants/<position>.json        the org chart; whoever configures the project writes it
    loop/requests/                requests waiting for the loop, one JSON file each
    loop/requests/done/           requests the loop has decided, with outcome and reason
    loop/mailboxes/<position>/    messages for the sessions of a position; received/ once read
    loop/sessions.json            the registry of the sessions the loop decided: launching, alive, closed
    loop/switches.json            the switch of every position
    loop/status.json              the loop pid, its heartbeat and a summary per position
    loop/interpellations/         the questions that climb from the root to the human
    loop/work/<session>/          the working directory of a session, empty at launch
    loop/records/<session>/       the launch prompt or the output of a session
    registry/loop.jsonl           the log of the loop, one JSON row per event

Only the loop writes the registry, the switches, the status and the log. A request is a file that
someone deposits and the loop decides: the request command deposits for the sessions, with the
sender derived from the process ancestry; the human deposits with sender `human`, from the loop's
command line or from the view. Every write goes through a temporary file and a rename, so a reader
never sees half a file and a `kill -9` leaves either the old version or the new one.

Standard library only: the request command imports it on every call.
"""

import json
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

HUMAN = "human"
REGISTRATION_WAIT_S = 20
POLLING_STEP_S = 0.25


class Paths:
    def __init__(self, state_root):
        self.state = Path(state_root)
        self.grants = self.state / "grants"
        self.loop = self.state / "loop"
        self.requests = self.loop / "requests"
        self.requests_done = self.requests / "done"
        self.mailboxes = self.loop / "mailboxes"
        self.sessions = self.loop / "sessions.json"
        self.switches = self.loop / "switches.json"
        self.status = self.loop / "status.json"
        self.interpellations = self.loop / "interpellations"
        self.work = self.loop / "work"
        self.records = self.loop / "records"
        self.lock = self.loop / "loop.lock"
        self.log = self.state / "registry" / "loop.jsonl"

    def mailbox(self, position):
        return self.mailboxes / position

    def received(self, position):
        return self.mailboxes / position / "received"


def now():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")


def new_id(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def write_json(path, data):
    """Atomic write: a temporary file next to the target, then a rename. The temporary name carries
    the pid, so two processes writing different files of the same directory never collide."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def entry_files(directory):
    """The entry files of a request or mailbox directory, oldest first: `<time_ns>-<id>.json`."""
    try:
        return sorted(path for path in Path(directory).glob("[0-9]*.json"))
    except OSError:
        return []


def deposit(directory, entry):
    """A new entry file, `<time_ns>-<id>.json`: name order is arrival order."""
    path = Path(directory) / f"{time.time_ns()}-{entry['id']}.json"
    write_json(path, entry)
    return path


# <editor-fold defaultstate="collapsed" desc="Requests">
def deposit_request(paths, sender, kind, **fields):
    """Deposits a request and returns its id. The sender is never an argument of the request
    command: whoever calls this already knows it, from the process ancestry or because it is the
    human."""
    request = {"id": new_id("r"), "ts": now(), "from": sender, "kind": kind, **fields}
    deposit(paths.requests, request)
    return request["id"]


def request_state(paths, request_id):
    """The request as the loop left it: decided (in done/, with `outcome`), queued (pending, with
    `queued`), accepted with its session being launched (pending, with `launching`), or pending and
    not yet seen (no `outcome`). None if it does not exist."""
    for directory in (paths.requests_done, paths.requests):
        for path in Path(directory).glob(f"[0-9]*-{request_id}.json"):
            return read_json(path)
    return None


def wait_for_decision(paths, request_id, wait_s):
    """The request once the loop has decided, queued or accepted it, or as it is at the end of the
    wait. An accepted spawn answers before its launch ends: whoever asks four spawns one after the
    other gets four launches that run together."""
    limit = time.monotonic() + wait_s
    while True:
        state = request_state(paths, request_id)
        if state is None or any(state.get(field) for field in ("outcome", "queued", "launching")) \
                or time.monotonic() >= limit:
            return state
        time.sleep(POLLING_STEP_S)


# What a decided request means for whoever deposited it, in Italian: these texts reach the LLM sessions.
OUTCOME_TEXTS = {
    "applied": "applicata",
    "spawned": "accettata: sessione {session} lanciata",
    "deposited": "consegnata alla casella di {position}: il loop la recapita a una sessione per regola",
    "interpellation": "salita all'umano come interpellazione {interpellation}",
    "halted": "applicata, ma la posizione ha superato il budget ed è tornata in folle",
    "failed": "fallita: {detail}",
}


def describe_decision(state):
    """One line in Italian for whoever deposited the request."""
    if state is None:
        return "richiesta sparita: nessun file fra le pendenti né fra le decise"
    if state.get("outcome") == "rejected":
        return f"rifiutata: {state['reason']} — {state.get('detail') or ''}".rstrip(" —")
    if state.get("outcome"):
        template = OUTCOME_TEXTS.get(state["outcome"], state["outcome"])
        return template.format_map({key: state.get(key) or "" for key in ("session", "position", "interpellation", "detail")})
    if state.get("launching"):
        return f"accettata: sessione {state['launching']['session']} in lancio; l'esito sarà nel log del loop"
    if state.get("queued"):
        return f"in coda: {state['queued']['reason']} — {state['queued']['detail']}; il loop la esegue quando può"
    return "depositata: il loop non l'ha ancora letta; l'esito sarà nel log del loop"


def exit_code(state):
    return 1 if state is not None and state.get("outcome") == "rejected" else 0
# </editor-fold>


# <editor-fold defaultstate="collapsed" desc="Mailboxes">
def deposit_message(paths, position, sender, text, to_session=None, **fields):
    """A message in the mailbox of a position. `to_session` is the session it is addressed to;
    None until the loop assigns it."""
    message = {"id": new_id("m"), "ts": now(), "from": sender, "position": position,
               "to_session": to_session, "text": text, **fields}
    deposit(paths.mailbox(position), message)
    return message


def take_message(paths, position, session_id):
    """The oldest message of the position addressed to the session, moved to received/ with the
    time it was read, `received`; None if there is none. The time is the reader's: the loop
    notices the read only at its next cycle, and carries this time in the `read_ts` of its
    `received` row. The rename claims the message, the rewrite adds the time: in between the file
    in received/ has no `received`, and the loop waits for it."""
    received = paths.received(position)
    received.mkdir(parents=True, exist_ok=True)
    for path in entry_files(paths.mailbox(position)):
        message = read_json(path)
        if not message or message.get("to_session") != session_id:
            continue
        try:
            os.rename(path, received / path.name)
        except FileNotFoundError:
            continue
        message["received"] = now()
        write_json(received / path.name, message)
        return message
    return None


def pending_messages(paths, position):
    """(path, message) for every message still in the mailbox of a position, oldest first."""
    messages = []
    for path in entry_files(paths.mailbox(position)):
        message = read_json(path)
        if message:
            messages.append((path, message))
    return messages
# </editor-fold>


# <editor-fold defaultstate="collapsed" desc="Processes and identity">
def stat_fields(pid):
    """The fields of /proc/<pid>/stat from the third onward: the command name can contain spaces."""
    with open(f"/proc/{pid}/stat", encoding="utf-8") as stat_file:
        content = stat_file.read()
    return content[content.rindex(")") + 2:].split()


def start_time(pid):
    return int(stat_fields(pid)[19])


def alive(pid, starttime):
    """True if the process exists, has the same start time, and is not a zombie."""
    try:
        fields = stat_fields(pid)
    except (OSError, ValueError):
        return False
    return fields[0] != "Z" and int(fields[19]) == starttime


def ancestors():
    """Pairs (pid, starttime) from the parent of the caller up to init, excluded."""
    chain = []
    pid = os.getppid()
    while pid > 1:
        try:
            fields = stat_fields(pid)
        except (OSError, ValueError):
            break
        chain.append((pid, int(fields[19])))
        pid = int(fields[1])
    return chain


def command_line(pid):
    try:
        return [part.decode(errors="replace") for part in Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")]
    except OSError:
        return []


def processes():
    """The pids of /proc, this process excluded."""
    return [int(entry.name) for entry in Path("/proc").iterdir() if entry.name.isdigit() and int(entry.name) != os.getpid()]


def processes_with_arguments(words):
    """The pids of the live processes whose command line carries all the words among its arguments."""
    found = []
    for pid in processes():
        arguments = command_line(pid)[1:]
        if arguments and all(word in arguments for word in words):
            found.append(pid)
    return found


def session_leader_with(variable, value):
    """(pid, starttime) of the live process that leads its own process session and whose environment
    carries variable=value; None if there is none. The children of that process inherit the
    environment, and do not lead the session."""
    wanted = f"{variable}={value}".encode()
    for pid in processes():
        try:
            fields = stat_fields(pid)
            if fields[0] == "Z" or int(fields[3]) != pid:
                continue
            if wanted in Path(f"/proc/{pid}/environ").read_bytes().split(b"\0"):
                return pid, int(fields[19])
        except (OSError, ValueError):
            continue
    return None


def registry(paths):
    return read_json(paths.sessions, {"sessions": {}})


def alive_registrations(paths):
    """(pid, starttime) → session id, for every session the registry marks alive."""
    return {(entry["pid"], entry["starttime"]): session_id
            for session_id, entry in registry(paths)["sessions"].items()
            if entry.get("status") == "alive" and entry.get("pid") and entry.get("starttime")}


def identify(paths, wait_s=REGISTRATION_WAIT_S):
    """The calling session as (session id, registry entry), from the process ancestry: the first
    ancestor whose (pid, starttime) the registry marks alive. Waits for the registration up to
    `wait_s`, because the loop registers a session only after its process has started.
    None if no ancestor is a registered session."""
    chain = ancestors()
    limit = time.monotonic() + wait_s
    while True:
        registrations = alive_registrations(paths)
        for pair in chain:
            if pair in registrations:
                session_id = registrations[pair]
                return session_id, registry(paths)["sessions"][session_id]
        if time.monotonic() >= limit:
            return None
        time.sleep(POLLING_STEP_S)
# </editor-fold>
