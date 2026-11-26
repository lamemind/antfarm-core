#!/usr/bin/env python3
"""A fake `claude` for the loop tests: background sessions without a model, with the delays the test
chooses, so that the concurrency of the loop is measured without tokens.

The test copies this file as `claude` into a directory of its own, puts the directory first on the
PATH of the loop, and writes config.json next to it:

    {"launch_s": 5, "resume_s": 5, "stop_s": 5, "agents_s": 0, "version": "2.1.332",
     "request": "<engine>/bin/request", "tokens": 600, "ignore_stops": 0}

It answers the commands the loop runs, as Claude Code does (antfarm-core/test/probe-resume.py):

    claude --version                      `<version> (Claude Code)`
    claude --bg [flags] --name <n> <p>    after launch_s, a session: `backgrounded · <id> · <n>`
    claude --bg --resume <sessionId> <t>  after resume_s, the same session in a new process, same
                                          short id: `backgrounded · <id>`; a copy with a new id if
                                          the process of the session is still alive
    claude stop <id>                      after stop_s, the process of the session ends; the first
                                          `ignore_stops` stops of a session exit 0 and end nothing
    claude agents --json                  after agents_s, the live sessions, all `idle`

A session is a process of this file started in a process session of its own, with the environment
block of --settings: at every start, launch or resume, it spends `tokens` — one assistant record
with that usage in its transcript, ~/.claude/projects/fake-claude/<sessionId>.jsonl, where the loop
counts the tokens of an LLM session —, runs `request recv` once, as a session of the loop does when
its prompt or its wake text says so, writes sessions/<id>.ready, and then waits for ever: a message
deposited after the .ready file waits for a wake. The test gives the loop a HOME of its
own, so the transcripts never land in the user's. The state of a session is sessions/<id>.json next
to this file. The delays run before the command acts, so a `claude --bg` killed during its delay
leaves no session.

Standard library only.
"""

import json
import os
import secrets
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

HOME = Path(__file__).resolve().parent
CONFIG = json.loads((HOME / "config.json").read_text(encoding="utf-8"))
SESSIONS = HOME / "sessions"
# The flags of `claude --bg` that take a value; --allowedTools takes values up to the next flag.
VALUE_FLAGS = ("--name", "--tools", "--permission-mode", "--settings", "--model", "--resume")


def start_time(pid):
    content = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    return int(content[content.rindex(")") + 2:].split()[19])


def alive(state):
    try:
        content = Path(f"/proc/{state['pid']}/stat").read_text(encoding="utf-8")
    except OSError:
        return False
    fields = content[content.rindex(")") + 2:].split()
    return fields[0] != "Z" and int(fields[19]) == state["starttime"]


def states():
    found = []
    for path in sorted(SESSIONS.glob("*.json")):
        try:
            found.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return found


def save(state):
    SESSIONS.mkdir(exist_ok=True)
    temporary = SESSIONS / f".{state['id']}.{os.getpid()}.tmp"
    temporary.write_text(json.dumps(state), encoding="utf-8")
    temporary.replace(SESSIONS / f"{state['id']}.json")


def parse_bg(arguments):
    """The flags and the last positional argument of a `claude --bg` command line."""
    flags, positional, index = {}, [], 0
    while index < len(arguments):
        word = arguments[index]
        if word in VALUE_FLAGS:
            flags[word] = arguments[index + 1]
            index += 2
        elif word == "--allowedTools":
            index += 1
            while index < len(arguments) and not arguments[index].startswith("--"):
                index += 1
        else:
            if not word.startswith("--"):
                positional.append(word)
            index += 1
    return flags, positional


def start_session(state, environment):
    """The process of a session, in a process session of its own; its pid and start time in the state."""
    process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "__session__"],
                               env={**environment, "FAKE_CLAUDE_SESSION_ID": state["sessionId"],
                                    "FAKE_CLAUDE_SHORT_ID": state["id"]},
                               cwd=state["cwd"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, start_new_session=True)
    state.update(pid=process.pid, starttime=start_time(process.pid))
    save(state)


def session_environment(settings):
    environment = {name: os.environ[name] for name in ("HOME", "PATH") if name in os.environ}
    environment.update((settings or {}).get("env", {}))
    return environment


def command_bg(arguments):
    flags, positional = parse_bg(arguments)
    if "--resume" in flags:
        time.sleep(CONFIG.get("resume_s", 0))
        found = [state for state in states() if state["sessionId"] == flags["--resume"]]
        if not found:
            print(f"no session {flags['--resume']}", file=sys.stderr)
            return 1
        state = found[0]
        if alive(state):
            state = {**state, "id": secrets.token_hex(4), "sessionId": str(uuid.uuid4())}
        start_session(state, state["environment"])
        print(f"backgrounded · {state['id']}", flush=True)
        return 0
    time.sleep(CONFIG.get("launch_s", 0))
    settings = json.loads(flags.get("--settings", "{}"))
    environment = session_environment(settings)
    state = {"id": secrets.token_hex(4), "sessionId": str(uuid.uuid4()), "name": flags.get("--name"),
             "cwd": os.getcwd(), "environment": environment, "prompt": positional[-1] if positional else None}
    start_session(state, environment)
    print(f"backgrounded · {state['id']} · {state['name']}", flush=True)
    return 0


def command_stop(short_id):
    time.sleep(CONFIG.get("stop_s", 0))
    path = SESSIONS / f"{short_id}.json"
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        print(f"no session {short_id}", file=sys.stderr)
        return 1
    if state.get("stops_ignored", 0) < CONFIG.get("ignore_stops", 0):
        state["stops_ignored"] = state.get("stops_ignored", 0) + 1
        save(state)
        return 0
    if alive(state):
        try:
            os.killpg(state["pid"], signal.SIGTERM)
        except ProcessLookupError:
            pass
    return 0


def command_agents():
    time.sleep(CONFIG.get("agents_s", 0))
    print(json.dumps([{"id": state["id"], "pid": state["pid"], "sessionId": state["sessionId"], "name": state["name"],
                       "cwd": state["cwd"], "kind": "background", "status": "idle"}
                      for state in states() if alive(state)]))
    return 0


def spend(tokens):
    """One assistant record with `tokens` input tokens, appended to the transcript of the session."""
    transcript = Path.home() / ".claude" / "projects" / "fake-claude" / f"{os.environ['FAKE_CLAUDE_SESSION_ID']}.jsonl"
    transcript.parent.mkdir(parents=True, exist_ok=True)
    record = {"type": "assistant", "sessionId": os.environ["FAKE_CLAUDE_SESSION_ID"],
              "message": {"id": f"msg-{uuid.uuid4().hex}", "usage": {
                  "input_tokens": tokens, "output_tokens": 0, "cache_creation_input_tokens": 0,
                  "cache_read_input_tokens": 0}}}
    previous = transcript.read_text(encoding="utf-8") if transcript.exists() else ""
    temporary = transcript.with_name(f".{transcript.name}.{os.getpid()}.tmp")
    temporary.write_text(previous + json.dumps(record) + "\n", encoding="utf-8")
    temporary.replace(transcript)


def session():
    """The process of a session: its tokens, one `request recv`, then a wait for ever."""
    if CONFIG.get("tokens"):
        spend(CONFIG["tokens"])
    request = CONFIG.get("request")
    ready = SESSIONS / f"{os.environ['FAKE_CLAUDE_SHORT_ID']}.ready"
    ready.unlink(missing_ok=True)
    if request:
        subprocess.run([request, "recv"], stdin=subprocess.DEVNULL, capture_output=True)
    ready.write_text(str(os.getpid()), encoding="utf-8")
    signal.pause()
    return 0


def main():
    arguments = sys.argv[1:]
    if arguments[:1] == ["__session__"]:
        return session()
    if arguments[:1] == ["--version"]:
        print(f"{CONFIG.get('version', '0.0.0')} (Claude Code)")
        return 0
    if arguments[:1] == ["--bg"]:
        return command_bg(arguments[1:])
    if arguments[:1] == ["stop"] and len(arguments) == 2:
        return command_stop(arguments[1])
    if arguments[:2] == ["agents", "--json"]:
        return command_agents()
    print(f"fake claude: unknown command {arguments}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
