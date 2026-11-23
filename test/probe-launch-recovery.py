#!/usr/bin/env python3
"""Probe of a launch whose launcher loses its parent: what the loop finds after its own `kill -9` in
the middle of a `claude --bg`.

The loop runs `claude --bg` in a worker thread; a `kill -9` of the loop leaves that command without
its parent. At restart the loop must know whether the session was born, or it would launch a second
one for the same request. The probe measures, on two haiku launches whose parent is killed with
SIGKILL KILL_AFTER_S after the launcher starts:

    1. stdout on a pipe to the dead parent, the launcher in the parent's process session, as
       subprocess.run leaves it: does the session start anyway
    2. stdout on a file, the launcher in a process session of its own: does the session start, and
       does the file carry `backgrounded · <short id> · <name>`
    3. whether the name passed to --name reads back from `claude agents --json` (field `name`) and
       from CLAUDE_CODE_SESSION_NAME in /proc/<pid>/environ of the session process
    4. whether the launcher shows the name on its command line in /proc while it runs, so that a
       restarted loop can tell a launch still in progress

It runs in a temporary directory, stops every session it launched before exiting, and spends a few
hundred haiku tokens.

Usage:  antfarm-core/test/probe-launch-recovery.py [--json]
Exits 0 when every measure produced a value, 1 otherwise.
"""

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

ENGINE = Path(__file__).resolve().parent.parent
FOLDER = Path(tempfile.mkdtemp(prefix="antfarm-probe-recovery-"))
os.environ.setdefault("ANTFARM_STATE_DIR", str(FOLDER))
sys.path.insert(0, str(ENGINE / "bin"))
import match as libmatch  # noqa: E402

MODEL = "haiku"
KILL_AFTER_S = 0.5
LAUNCHER_WAIT_S = 120
SESSION_WAIT_S = 60
PROMPT = "Rispondi solo: ok. Non usare strumenti."
FLAGS = ["--safe-mode", "--tools", "Bash", "--allowedTools", "Bash(echo *)", "--permission-mode", "dontAsk",
         "--model", MODEL]
# The parent: it starts the launcher, prints its pid, and waits to be killed.
PARENT = """
import json, subprocess, sys, time
command, output, own_session = json.loads(sys.argv[1]), sys.argv[2], sys.argv[3] == "1"
stdout = subprocess.PIPE if output == "pipe" else open(output, "w")
launcher = subprocess.Popen(command, stdout=stdout, stderr=subprocess.STDOUT, start_new_session=own_session)
print(launcher.pid, flush=True)
time.sleep(600)
"""

launched = []


def log(text):
    print(text, file=sys.stderr, flush=True)


def command_line(pid):
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    except OSError:
        return None


def launchers_with(name):
    """The pids whose command line carries the name as one argument."""
    wanted = name.encode()
    found = []
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit() and wanted in (command_line(entry.name) or []):
            found.append(int(entry.name))
    return found


def running(pid):
    """True while the process exists and is not a zombie."""
    try:
        content = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return False
    return content[content.rindex(")") + 2:].split()[0] != "Z"


def environment_value(pid, variable):
    try:
        entries = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
    except OSError:
        return None
    prefix = f"{variable}=".encode()
    return next((entry[len(prefix):].decode(errors="replace") for entry in entries if entry.startswith(prefix)), None)


def by_name(name):
    """The entry of `claude agents --json` whose name is the given one, waiting up to SESSION_WAIT_S."""
    limit = time.monotonic() + SESSION_WAIT_S
    while time.monotonic() < limit:
        for entry in libmatch.session_states().values():
            if entry.get("name") == name and entry.get("pid"):
                return entry
        time.sleep(1)
    return None


def orphan_launch(case, output, own_session):
    """One launch whose parent dies KILL_AFTER_S after the launcher starts."""
    name = f"antfarm probe · orphan launch · {case} · {uuid.uuid4().hex[:12]}"
    cwd = FOLDER / case
    cwd.mkdir()
    command = ["claude", "--bg", "--name", name, *FLAGS, PROMPT]
    parent = subprocess.Popen([sys.executable, "-c", PARENT, json.dumps(command), output, "1" if own_session else "0"],
                              cwd=cwd, env=libmatch.launch_environment(), stdout=subprocess.PIPE, text=True)
    launcher_pid = int(parent.stdout.readline())
    time.sleep(KILL_AFTER_S)
    parent.kill()
    parent.wait()
    result = {"name": name, "stdout": output if output == "pipe" else "file", "own_session": own_session,
              "launcher_found_by_name_after_parent_death": launcher_pid in launchers_with(name)}
    started = time.monotonic()
    while running(launcher_pid) and time.monotonic() < started + LAUNCHER_WAIT_S:
        time.sleep(0.2)
    result["launcher_ended"] = not running(launcher_pid)
    result["launcher_lasted_s"] = round(time.monotonic() - started + KILL_AFTER_S, 1)
    if output != "pipe":
        text = Path(output).read_text(encoding="utf-8", errors="replace")
        hit = libmatch.LAUNCH_PATTERN.search(text)
        result["file"] = text.strip()[-300:]
        result["file_short_id"] = hit.group(1) if hit else None
        result["file_carries_name"] = name in text
    entry = by_name(name)
    result["session_started"] = entry is not None
    if entry is not None:
        launched.append(entry["id"])
        result["short_id"] = entry["id"]
        result["agents_json_name"] = entry.get("name")
        result["environ_session_name"] = environment_value(entry["pid"], "CLAUDE_CODE_SESSION_NAME")
        result["environ_name_equal"] = result["environ_session_name"] == name
    return result


def main():
    as_json = "--json" in sys.argv[1:]
    result = {"claude_version": libmatch.claude_version(), "kill_after_s": KILL_AFTER_S}
    try:
        log("1 · stdout on a pipe to the parent, same process session")
        result["1_pipe"] = orphan_launch("pipe", "pipe", own_session=False)
        log("2 · stdout on a file, process session of its own")
        result["2_file"] = orphan_launch("file", str(FOLDER / "launch.out"), own_session=True)
    finally:
        for short_id in launched:
            subprocess.run(["claude", "stop", short_id], capture_output=True, text=True, timeout=60)
    complete = all(key in result for key in ("1_pipe", "2_file"))
    if as_json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        for key, value in result.items():
            print(f"{key}: {json.dumps(value, ensure_ascii=False)}")
    return 0 if complete else 1


if __name__ == "__main__":
    signal.signal(signal.SIGINT, signal.default_int_handler)
    sys.exit(main())
