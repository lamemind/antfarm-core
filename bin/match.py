"""Match lifecycle: directory, process registration, node launch, close.

Library of the director. It writes the two files that bind a session to the match without asking
the agent anything: nodes.json — the (pid, starttime) pair that the bus compares with the
caller's ancestors, plus sessionId, parent and task — and meta.json, the match parameters.

At the end of the match it writes into each node's entry `closed`, the time to the millisecond,
`actions`, the list of the transcript's tool calls that are not a bus Bash command — a command is
one only if each of its segments calls the bus and nothing inside runs anything else —, and
`off_bus_actions`, its length. For a node without a transcript, such as a script node, both
fields are null. The harness reads these fields from nodes.json and never opens the transcripts:
the Claude Code format is read only here.

The graph of a match can change while the match is open, and only the director writes it.
`graph.json` is the current version, the one the bus rereads at every command; `graphs/v<N>.json`
keeps every version, the first included, with the time it took effect. A new version takes effect
only if it carries the current number + 1 and `validate-graph.py` exits 0: otherwise nothing
changes.

Matches are created under `matches/` of the state root, read from ANTFARM_STATE_DIR only once at
import and passed to the children as an absolute path.
"""

import fcntl
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import graph as libgraph

CODE_ROOT = libgraph.code_root()
STATE_ROOT = libgraph.state_root()
# The director's children inherit the state root as an absolute path: the director's bus
# commands and the script nodes from the environment, the LLM nodes from the `env` block of
# --settings. A relative root, read from a node's working directory, would point inside the match.
os.environ[libgraph.STATE_VARIABLE] = str(STATE_ROOT)
MATCHES = STATE_ROOT / "matches"
BUS = CODE_ROOT / "bin" / "bus"
GRAPH_VALIDATOR = CODE_ROOT / "bin" / "validate-graph.py"
CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"
BASH_TIMEOUT_MS = 900000
VARIABLES_NOT_PASSED = ("LOOM_TASK", "PROJECT_ROOT", "PTYXIS_PROFILE")
LAUNCH_PATTERN = re.compile(r"backgrounded · ([0-9a-f]{8}) · ")
SERVICE_WAIT_S = 60
TYPES_WITHOUT_PROCESS = ("human", "source")
ACTION_OUTCOME_LENGTH = 1000
DENIAL_PREFIX = "Permission to use Bash has been denied"
DENIED_BUS_CALL = "denied bus call"
OFF_CHANNEL_ACTION = "off-channel action"
ASSIGNMENT = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=(.*)", re.DOTALL)
VARIABLE_REFERENCE = re.compile(r"\$(?:([A-Za-z_][A-Za-z0-9_]*)|\{([A-Za-z_][A-Za-z0-9_]*)\})")
HEREDOC_OPENING = re.compile(
    r"<<(?P<dash>-?)[ \t]*(?:(?P<quote>['\"])(?P<quoted_word>[^'\"]*)(?P=quote)"
    r"|\\(?P<escaped_word>\S+)|(?P<bare_word>[^\s;|&<>()'\"\\]+))")


def now(precision="seconds"):
    return datetime.now(timezone.utc).astimezone().isoformat(timespec=precision)


def process_start_time(pid):
    with open(f"/proc/{pid}/stat", encoding="utf-8") as stat_file:
        content = stat_file.read()
    return int(content[content.rindex(")") + 2:].split()[19])


def write_json(path, data):
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


class GraphRejected(Exception):
    """A new graph version that cannot take effect: wrong number or invalid graph."""


class Match:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.id = self.directory.name
        self.script_processes = {}

    @property
    def nodes_file(self):
        return self.directory / "nodes.json"

    @property
    def meta_file(self):
        return self.directory / "meta.json"

    def nodes(self):
        return json.loads(self.nodes_file.read_text(encoding="utf-8"))

    def meta(self):
        return json.loads(self.meta_file.read_text(encoding="utf-8"))

    def update_meta(self, **fields):
        data = self.meta()
        data.update(fields)
        write_json(self.meta_file, data)

    def graph(self):
        return libgraph.load(self.directory / "graph.json")

    @classmethod
    def create(cls, name, game, game_graph, meta):
        """New directory under matches/, graph copied and already validated by the caller, source
        files created empty, director registered."""
        stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
        directory = MATCHES / f"{stamp}-{name}"
        (directory / "mailboxes").mkdir(parents=True)
        (directory / "nodes").mkdir()
        shutil.copyfile(game_graph, directory / "graph.json")
        match = cls(directory)
        match.prepare_sources()
        match.keep_version(match.graph(), now("milliseconds"))
        write_json(match.meta_file, {"match": match.id, "game": game, "start": now(), **meta})
        write_json(match.nodes_file, {
            "match": match.id,
            "status": "open",
            "nodes": {
                "director": {
                    "type": "script",
                    "pid": os.getpid(),
                    "starttime": process_start_time(os.getpid()),
                    "launched_by_session": os.environ.get("CLAUDE_CODE_SESSION_ID"),
                },
            },
        })
        return match

    def keep_version(self, graph, effective):
        """`graphs/v<N>.json`: the graph of this version and the time it took effect. The replay
        needs it, because once the match is closed graph.json is already the last version."""
        versions_dir = self.directory / "graphs"
        versions_dir.mkdir(exist_ok=True)
        write_json(versions_dir / f"v{graph['version']}.json", {"effective": effective, "graph": graph})

    def write_version(self, new_graph):
        """Replaces the match graph with a new version and returns the time it took effect.
        Raises GraphRejected, and changes nothing, if the number is not the current + 1 or if
        `validate-graph.py` does not exit 0 on the new graph.

        The replacement is atomic: the bus reads either the old version or the new one, never a
        half-written file. The exclusive lock holds the time and the replacement together. The
        version enters `graphs/` before `graph.json`: whoever follows the match finds the file of
        a version as soon as a message carries it."""
        expected = self.graph()["version"] + 1
        if new_graph.get("version") != expected:
            raise GraphRejected(f"version {new_graph.get('version')} instead of {expected}")
        candidate = self.directory / ".graph-candidate.json"
        write_json(candidate, new_graph)
        try:
            outcome = subprocess.run([str(GRAPH_VALIDATOR), str(candidate)], capture_output=True, text=True)
        finally:
            candidate.unlink()
        if outcome.returncode != 0:
            raise GraphRejected(f"validate-graph.py exits {outcome.returncode}: {outcome.stderr.strip()}")
        with open(self.directory / libgraph.GRAPH_LOCK, "a", encoding="utf-8") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            effective = now("milliseconds")
            self.keep_version(new_graph, effective)
            write_json(self.directory / "graph.json", new_graph)
        return effective

    def source_file(self, source_id):
        """The file of a graph source, at its path relative to the match directory."""
        return self.directory / libgraph.node(self.graph(), source_id)["path"]

    def prepare_sources(self):
        """Creates the file of every graph source, empty: it exists before any write, so the
        start sha256 and the oracle that reads it always have a file to work on."""
        for node in self.graph()["nodes"]:
            if node["type"] == "source":
                path = self.source_file(node["id"])
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch()

    def register(self, node_id, data):
        """Writes the node's entry in nodes.json. A `human` or `source` node of the graph has no
        process: registering one would mean that the bus mistakes that process for the human, or
        for a file, and stamps it as sender. A director that tries is a bug, and stops here."""
        graph_entry = libgraph.node(self.graph(), node_id)
        if graph_entry is not None and graph_entry["type"] in TYPES_WITHOUT_PROCESS:
            raise ValueError(f"{node_id} is a {graph_entry['type']} node of the graph: it has no process to register")
        nodes = self.nodes()
        nodes["nodes"][node_id] = data
        write_json(self.nodes_file, nodes)

    def mark_closed(self, node_id, actions):
        """`closed`, `actions` and the `off_bus_actions` count in the node's entry; a node already
        closed stays as it is. The count comes from the list, so the two fields do not diverge.
        `actions` None means no transcript to read: the two fields stay null."""
        nodes = self.nodes()
        entry = nodes["nodes"][node_id]
        if "closed" in entry:
            return
        entry["closed"] = now("milliseconds")
        entry["actions"] = actions
        entry["off_bus_actions"] = None if actions is None else len(actions)
        write_json(self.nodes_file, nodes)

    def rewrite_actions(self):
        """Rewrites `actions` from the transcripts in the entries that already carry a count, and
        only in those: an entry without a count does not emit node-closed in the replay, or is a
        script node.

        The count written at close is the original measure and does not change. An entry whose
        transcript is gone, or whose list has a length other than the count, stays as it is and
        ends up among the problems. Returns node → number of actions written, node → problem."""
        nodes = self.nodes()
        written_actions, problems = {}, {}
        for node_id, entry in nodes["nodes"].items():
            count = entry.get("off_bus_actions")
            if count is None:
                continue
            path = transcript(entry["sessionId"])
            if path is None:
                problems[node_id] = f"transcript {entry['sessionId']} missing"
                continue
            actions = transcript_actions(path)
            if len(actions) != count:
                problems[node_id] = f"{len(actions)} actions in the transcript, {count} in the count"
                continue
            entry["actions"] = actions
            written_actions[node_id] = len(actions)
        write_json(self.nodes_file, nodes)
        return written_actions, problems

    def close(self, outcome):
        nodes = self.nodes()
        nodes["status"] = "closed"
        write_json(self.nodes_file, nodes)
        self.update_meta(end=now(), outcome=outcome)

    def launch_node(self, node_id, prompt, task, model=None):
        """Isolated `claude --bg` session in the node's empty directory; returns the registered data."""
        cwd = self.directory / "nodes" / node_id
        cwd.mkdir(parents=True)
        prompt_file = self.directory / f"prompt-{node_id}.txt"
        prompt_file.write_text(prompt, encoding="utf-8")
        name = f"antfarm · {self.id} · {node_id}"
        command = [
            "claude", "--bg", "--safe-mode", "--name", name,
            "--tools", "Bash",
            "--allowedTools", f"Bash({BUS} *)",
            "--permission-mode", "dontAsk",
            "--settings", json.dumps({"env": {
                "BASH_DEFAULT_TIMEOUT_MS": str(BASH_TIMEOUT_MS),
                "BASH_MAX_TIMEOUT_MS": str(BASH_TIMEOUT_MS),
                libgraph.STATE_VARIABLE: str(STATE_ROOT),
            }}),
        ]
        if model:
            command += ["--model", model]
        command.append(prompt)
        environment = {key: value for key, value in os.environ.items() if key not in VARIABLES_NOT_PASSED}
        launch = subprocess.run(command, cwd=cwd, env=environment, capture_output=True, text=True, timeout=120)
        hit = LAUNCH_PATTERN.search(launch.stdout + launch.stderr)
        if launch.returncode != 0 or not hit:
            raise RuntimeError(f"launch of {node_id} failed (exit {launch.returncode}): {launch.stdout}{launch.stderr}")
        session = wait_for_session(hit.group(1))
        data = {
            "type": "llm",
            "pid": session["pid"],
            "starttime": process_start_time(session["pid"]),
            "sessionId": session["sessionId"],
            "short_id": session["id"],
            "name": name,
            "cwd": str(cwd),
            "parent": {"node": "director", "pid": os.getpid()},
            "task": task,
            "prompt": prompt_file.name,
            "launched": now("milliseconds"),
            "command": [part if part != prompt else f"<{prompt_file.name}>" for part in command],
        }
        self.register(node_id, data)
        return data

    def launch_script_node(self, node_id, command, task):
        """Local process, child of the director. It starts blocked on stdin and the director
        unblocks it only after registering it: before that, the bus would find the director among
        its ancestors and mistake it for the director."""
        cwd = self.directory / "nodes" / node_id
        cwd.mkdir(parents=True)
        output = open(self.directory / f"output-{node_id}.txt", "w", encoding="utf-8")
        process = subprocess.Popen(command, cwd=cwd, stdin=subprocess.PIPE, stdout=output,
                                    stderr=subprocess.STDOUT, text=True)
        self.script_processes[node_id] = process
        self.register(node_id, {
            "type": "script",
            "pid": process.pid,
            "starttime": process_start_time(process.pid),
            "cwd": str(cwd),
            "parent": {"node": "director", "pid": os.getpid()},
            "task": task,
            "launched": now("milliseconds"),
            "command": [str(part) for part in command],
        })
        process.stdin.write("via\n")
        process.stdin.close()
        return process

    def sessions(self):
        return {node_id: data for node_id, data in self.nodes()["nodes"].items() if data.get("type") == "llm"}

    def wait_for_nodes_done(self, timeout_s):
        """Waits until every LLM node has ended its turn; returns the nodes still working at the timeout."""
        limit = time.monotonic() + timeout_s
        while True:
            states = session_states()
            working = [node_id for node_id, data in self.sessions().items()
                         if states.get(data["short_id"], {}).get("status") == "busy"]
            if not working or time.monotonic() >= limit:
                return working
            time.sleep(5)

    def wait_for_script_nodes(self, timeout_s):
        """Waits for every script node to end and marks it closed; raises TimeoutExpired at the timeout."""
        for node_id, process in self.script_processes.items():
            process.wait(timeout=timeout_s)
            self.mark_closed(node_id, None)

    def terminate_script_nodes(self):
        """Terminates the script nodes still alive — a match closed at the timeout — and marks them closed."""
        for node_id, process in self.script_processes.items():
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            self.mark_closed(node_id, None)

    def stop_nodes(self):
        """Stops every session, then marks it closed with the off-bus actions read from the transcript."""
        for node_id, data in self.sessions().items():
            subprocess.run(["claude", "stop", data["short_id"]], capture_output=True, text=True, timeout=60)
            path = transcript(data["sessionId"])
            self.mark_closed(node_id, transcript_actions(path) if path else None)

    def terminate_leftover_buses(self):
        """Terminates the bus processes still alive whose working directory is under the match's
        nodes/; returns their pids. A session stopped while it waits in `bus recv` can leave the bus
        orphaned for the length of its wait, and the orphaned bus would read a message after the
        match is closed."""
        root = (self.directory / "nodes").resolve()
        terminated = []
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                working_dir = Path(os.readlink(entry / "cwd"))
                args = (entry / "cmdline").read_bytes().decode(errors="replace").split("\0")
            except OSError:
                continue
            if str(BUS) in args and root in working_dir.parents:
                try:
                    os.kill(int(entry.name), signal.SIGTERM)
                    terminated.append(int(entry.name))
                except ProcessLookupError:
                    continue
        return terminated


def session_states():
    """short id → entry of `claude agents --json`, for the live sessions."""
    output = subprocess.run(["claude", "agents", "--json"], capture_output=True, text=True, timeout=60)
    try:
        return {entry["id"]: entry for entry in json.loads(output.stdout) if entry.get("id")}
    except json.JSONDecodeError:
        return {}


def wait_for_session(short_id):
    limit = time.monotonic() + SERVICE_WAIT_S
    while time.monotonic() < limit:
        entry = session_states().get(short_id)
        if entry and entry.get("pid") and entry.get("sessionId"):
            return entry
        time.sleep(1)
    raise RuntimeError(f"session {short_id} does not appear in `claude agents --json` within {SERVICE_WAIT_S} s")


def transcript(session_id):
    """A session's transcript, looked up by id among the Claude Code projects."""
    found = sorted(CLAUDE_PROJECTS.glob(f"*/{session_id}.jsonl"))
    return found[0] if found else None


def bash_calls(transcript_path):
    """(tool name, command, text outcome, is_error) for every tool call of the transcript, in order."""
    uses, results = [], {}
    with open(transcript_path, encoding="utf-8") as transcript_file:
        for record in map(json.loads, transcript_file):
            content = (record.get("message") or {}).get("content")
            if record.get("type") not in ("assistant", "user") or not isinstance(content, list):
                continue
            for block in content:
                if block.get("type") == "tool_use":
                    uses.append((block["id"], block["name"], block.get("input", {}).get("command", "")))
                elif block.get("type") == "tool_result":
                    text = block.get("content")
                    if isinstance(text, list):
                        text = "\n".join(part.get("text", "") for part in text)
                    results[block["tool_use_id"]] = (text or "", bool(block.get("is_error")))
    return [(name, command, *results.get(use_id, ("", None))) for use_id, name, command in uses]


def transcript_actions(transcript_path):
    """The tool calls that are not a bus Bash command, that is everything a node does outside the
    channel, as a list of entries in transcript order. A Bash command stays out only if it passes
    `bus_command`: one segment that does not call the bus, or one command substitution, is enough
    for the whole command to go in.

    The command stays whole; the outcome is truncated to ACTION_OUTCOME_LENGTH characters, because
    it can contain the text of a message. `error` is null if the call got no response, such as a
    command interrupted when the session was stopped."""
    off_bus = [call for call in bash_calls(transcript_path)
                 if call[0] != "Bash" or not bus_command(call[1])]
    return [{"order": order, "tool": name, "command": command, "outcome": outcome[:ACTION_OUTCOME_LENGTH], "error": error}
            for order, (name, command, outcome, error) in enumerate(off_bus, start=1)]


def bus_command(command):
    """True if the command does nothing but call the bus: every segment starts with the bus path
    and nothing in the text runs another command. It is the filter of `actions`: a command that
    does not pass goes into the list whole, as written. An assignment `B=<bus>` or a `$B send …`
    is not the path: the command goes in, and `action_class` is what tells that it meant the bus."""
    if runs_other_command(command):
        return False
    segments = command_segments(command)
    return bool(segments) and all(bus_invocation(segment) for segment in segments)


def bus_invocation(segment, bus_variables=frozenset()):
    """True if the first word of the segment is the bus path, or `$NAME` or `${NAME}` of one of
    the given variables. A segment with unbalanced quotes cannot be read: False."""
    try:
        words = shlex.split(segment)
    except ValueError:
        return False
    if not words:
        return False
    reference = VARIABLE_REFERENCE.fullmatch(words[0])
    return words[0] == str(BUS) or bool(reference and (reference.group(1) or reference.group(2)) in bus_variables)


def runs_other_command(command):
    """True if the command runs something inside itself, beyond its segments: `$(…)`, a backtick,
    `<(…)` or `>(…)` outside single quotes, in the text or in the body of an unquoted heredoc.
    `$((…))` is arithmetic, not a command. Inside single quotes and in the body of a quoted
    heredoc nothing expands, and there these characters are text."""
    text, bodies = without_heredoc_bodies(command)
    return _substitution(text, with_quotes=True) or any(_substitution(body, with_quotes=False) for body in bodies)


def _substitution(text, with_quotes):
    """The engine of `runs_other_command`. With `with_quotes` it reads quotes as the shell does:
    single quotes turn everything off, double quotes leave `$(` and the backtick active. Without
    it, the text is the body of an unquoted heredoc: `$(` and backtick active everywhere, quotes
    are plain characters."""
    quote = None
    index = 0
    while index < len(text):
        char = text[index]
        if char == "\\" and quote != "'":
            index += 2
            continue
        if quote == "'":
            if char == "'":
                quote = None
        elif with_quotes and char in "\"'" and (quote is None or char == quote):
            quote = None if quote else char
        elif char == "`":
            return True
        elif text.startswith("$(", index) and not text.startswith("$((", index):
            return True
        elif quote is None and with_quotes and char in "<>" and text.startswith("(", index + 1):
            return True
        index += 1
    return False


def without_heredoc_bodies(command):
    """(the command without the bodies of its heredocs, the bodies of the unquoted heredocs).

    `<<WORD`, `<<-WORD`, `<<'WORD'`, `<<"WORD"` and `<<\\WORD` outside quotes open a heredoc: the
    lines after the command's line, up to the line equal to WORD, are the text the command reads
    from stdin, not commands, and here they disappear together with the closing line. The body of
    an unquoted heredoc expands `$(…)` and backticks, so it is returned separately for someone to
    inspect; a quoted or escaped one is literal and is not returned. `<<<` is a here-string and
    opens nothing. Without a closing line the body runs to the end of the command."""
    text, bodies, pending = [], [], []
    quote = None
    index = 0
    while index < len(command):
        char = command[index]
        if char == "\\" and quote != "'" and index + 1 < len(command):
            text.append(command[index:index + 2])
            index += 2
            continue
        if quote:
            if char == quote:
                quote = None
        elif char in "\"'":
            quote = char
        elif command.startswith("<<", index) and not command.startswith("<<<", index) and command[index - 1:index] != "<":
            opening = HEREDOC_OPENING.match(command, index)
            if opening:
                word = opening.group("quoted_word") or opening.group("escaped_word") or opening.group("bare_word")
                literal = opening.group("bare_word") is None
                pending.append((word, literal, opening.group("dash") == "-"))
                text.append(opening.group(0))
                index = opening.end()
                continue
        elif char == "\n" and pending:
            text.append(char)
            index += 1
            for word, literal, dash in pending:
                body_lines = []
                while index < len(command):
                    line_end = command.find("\n", index)
                    line = command[index:] if line_end == -1 else command[index:line_end]
                    index = len(command) if line_end == -1 else line_end + 1
                    if (line.lstrip("\t") if dash else line) == word:
                        break
                    body_lines.append(line)
                if not literal:
                    bodies.append("\n".join(body_lines))
            pending = []
            continue
        text.append(char)
        index += 1
    return "".join(text), bodies


def command_segments(command):
    """The command split on `;`, `&&`, `||`, `|`, `&` and newline, outside quotes, without the
    empty segments. A backslash escapes the next character, except inside single quotes. A lone
    `&` separates like `;` — it sends the segment before it to the background — but `>&`, `<&` and
    `&>` are redirections and stay in the segment. A heredoc body is not a segment: it disappears
    before the split (`without_heredoc_bodies`), and the line that opens it stays."""
    text, _ = without_heredoc_bodies(command)
    segments, current, quote = [], [], None
    index = 0
    while index < len(text):
        char = text[index]
        if char == "\\" and quote != "'" and index + 1 < len(text):
            current.append(text[index:index + 2])
            index += 2
            continue
        if quote:
            if char == quote:
                quote = None
        elif char in "\"'":
            quote = char
        else:
            length = _separator_length(text, index)
            if length:
                segments.append("".join(current))
                current = []
                index += length
                continue
        current.append(char)
        index += 1
    segments.append("".join(current))
    return [segment.strip() for segment in segments if segment.strip()]


def _separator_length(text, index):
    """How many characters the segment separator starting at `index` takes, 0 if there is none
    there: 2 for `&&` and `||`, 1 for `;`, `|`, newline and a lone `&`. `>&`, `<&` and `&>` are
    redirections."""
    if text.startswith(("&&", "||"), index):
        return 2
    char = text[index]
    if char in ";|\n":
        return 1
    if char == "&":
        before = text[index - 1] if index else ""
        after = text[index + 1] if index + 1 < len(text) else ""
        return 0 if before in ("<", ">") or after == ">" else 1
    return 0


def action_class(action):
    """Whether an entry of `actions` is a denied attempt to use the bus or an off-channel action.

    It is a denied bus call when the outcome is the `dontAsk` denial, nothing in the command runs
    another command (`runs_other_command`) and every segment is an assignment `NAME=…` or a bus
    invocation — with the path, or with a variable assigned to the path earlier in the same
    command — and at least one is an invocation: a command made only of assignments calls nothing.
    A segment with unbalanced quotes cannot be read, and falls among the off-channel actions."""
    if not action["outcome"].startswith(DENIAL_PREFIX) or runs_other_command(action["command"]):
        return OFF_CHANNEL_ACTION
    bus_variables, invocations = set(), 0
    for segment in command_segments(action["command"]):
        try:
            words = shlex.split(segment)
        except ValueError:
            return OFF_CHANNEL_ACTION
        assignment = ASSIGNMENT.fullmatch(words[0]) if len(words) == 1 else None
        if assignment:
            if assignment.group(2) == str(BUS):
                bus_variables.add(assignment.group(1))
            else:
                bus_variables.discard(assignment.group(1))
            continue
        if not bus_invocation(segment, bus_variables):
            return OFF_CHANNEL_ACTION
        invocations += 1
    return DENIED_BUS_CALL if invocations else OFF_CHANNEL_ACTION


def send(text, recipient):
    """Send from the director: it goes through the bus like any other node, as the child of a process registered as director."""
    return subprocess.run([str(BUS), "send", recipient], input=text, capture_output=True, text=True)


def receive(wait_s):
    """The first message in the director's mailbox within wait_s, as (sender, id, text); None at the timeout."""
    limit = time.monotonic() + wait_s
    while time.monotonic() < limit:
        remaining = max(1, min(840, int(limit - time.monotonic())))
        output = subprocess.run([str(BUS), "recv", "--wait", str(remaining)], capture_output=True, text=True)
        if output.returncode == 0:
            header, _, text = output.stdout.partition("\n---\n")
            hit = re.match(r"messaggio (\S+) da (\S+) · grafo v(\d+)", header)
            return hit.group(2), hit.group(1), text.rstrip("\n")
        if output.returncode != 3:
            raise RuntimeError(f"bus recv of the director: exit {output.returncode}: {output.stderr}")
    return None
