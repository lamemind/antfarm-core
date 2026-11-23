#!/usr/bin/env python3
"""Probe of the five behaviours of Claude Code background sessions on which the loop rests.

The loop wakes a session with `claude stop` followed by `claude --bg --resume <sessionId> <text>`,
takes a session as alive while its (pid, starttime) pair exists in /proc, and outlives its sessions'
launcher. The help of Claude Code describes these behaviours; this probe runs them on real haiku
sessions and prints what it measures:

    1. after `claude stop`, a resume keeps the sessionId; whether the launch flags must be passed again
       (the permission mode of the resumed turn, and the command line of the resumed process)
    2. a resume on a session that has ended its turn and whose process is still alive: does it continue
       the session or start a copy
    3. the `origin.kind` that the transcript gives to the text of a resume
    4. the session outlives a `kill -9` of the process that launched it: pid and starttime unchanged
    5. whether `claude --bg` launches with HOME and PATH only, and whose environment the session
       process carries: the launcher's, or the background service's

It runs on a temporary state root and an empty working directory per session; it stops every session
it launched before exiting. It spends a few thousand haiku tokens.

Usage:  antfarm-core/test/probe-resume.py [--json]
Exits 0 when every measure produced a value, 1 otherwise.
"""

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ENGINE = Path(__file__).resolve().parent.parent
STATE = Path(tempfile.mkdtemp(prefix="antfarm-probe-"))
os.environ["ANTFARM_STATE_DIR"] = str(STATE)
sys.path.insert(0, str(ENGINE / "bin"))
import match as libmatch  # noqa: E402

MODEL = "haiku"
TURN_WAIT_S = 180
FLAGS = [
    "--safe-mode", "--tools", "Bash", "--allowedTools", "Bash(echo *)",
    "--permission-mode", "dontAsk", "--model", MODEL,
]
MINIMAL_ENVIRONMENT = ("HOME", "PATH")
ENVIRONMENT_CANDIDATES = ("USER", "LOGNAME", "LANG", "SHELL", "TERM")
MARKER = "ANTFARM_PROBE_MARKER"

launched = []


def log(text):
    print(text, file=sys.stderr, flush=True)


def working_directory(name):
    directory = STATE / "sessions" / name
    directory.mkdir(parents=True)
    return directory


def launch(arguments, cwd, environment):
    """`claude --bg <arguments>`; returns (exit code, launch output, short id or None)."""
    outcome = subprocess.run(["claude", "--bg", *arguments], cwd=cwd, env=environment,
                             capture_output=True, text=True, timeout=120)
    output = outcome.stdout + outcome.stderr
    hit = libmatch.LAUNCH_PATTERN.search(output)
    return outcome.returncode, output.strip(), hit.group(1) if hit else None


def session(short_id):
    entry = libmatch.wait_for_session(short_id)
    launched.append(short_id)
    return entry


def wait_turn_end(short_id):
    """The status of the session at the end of the wait: anything other than `busy`, or `busy` at the timeout."""
    limit = time.monotonic() + TURN_WAIT_S
    time.sleep(3)
    while True:
        entry = libmatch.session_states().get(short_id, {})
        status = entry.get("status")
        if status != "busy" or time.monotonic() >= limit:
            return status, entry
        time.sleep(3)


def alive(pid, starttime):
    try:
        return libmatch.process_start_time(pid) == starttime
    except (FileNotFoundError, ProcessLookupError):
        return False


def parent_pid(pid):
    with open(f"/proc/{pid}/stat", encoding="utf-8") as stat_file:
        content = stat_file.read()
    return int(content[content.rindex(")") + 2:].split()[1])


def command_line(pid):
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().decode(errors="replace").split("\0")[:-1]
    except OSError:
        return None


def environment_names(pid):
    try:
        entries = Path(f"/proc/{pid}/environ").read_bytes().decode(errors="replace").split("\0")
    except OSError:
        return None
    return sorted({entry.split("=", 1)[0] for entry in entries if "=" in entry})


def stop(short_id):
    subprocess.run(["claude", "stop", short_id], capture_output=True, text=True, timeout=60)


def wait_gone(pid, starttime, timeout_s=30):
    limit = time.monotonic() + timeout_s
    while alive(pid, starttime) and time.monotonic() < limit:
        time.sleep(1)
    return not alive(pid, starttime)


def user_records(session_id):
    """(origin.kind, permissionMode, content kind, first characters) of every `user` record."""
    path = libmatch.transcript(session_id)
    if path is None:
        return None
    rows = []
    for record in map(json.loads, path.read_text(encoding="utf-8").splitlines()):
        if record.get("type") != "user":
            continue
        content = (record.get("message") or {}).get("content")
        if isinstance(content, str):
            kind, text = "text", content
        else:
            kinds = [block.get("type") for block in content or []]
            kind = ",".join(kinds)
            text = " ".join(block.get("text", "") for block in content or [] if block.get("type") == "text")
        rows.append({"origin": (record.get("origin") or {}).get("kind"), "permission_mode": record.get("permissionMode"),
                     "content": kind, "text": text[:80]})
    return rows


def bash_commands(session_id):
    path = libmatch.transcript(session_id)
    if path is None:
        return None
    return [{"command": command, "outcome": outcome[:120], "error": error}
            for name, command, outcome, error in libmatch.bash_calls(path) if name == "Bash"]


def base_environment():
    return {key: value for key, value in os.environ.items() if key in MINIMAL_ENVIRONMENT + ENVIRONMENT_CANDIDATES}


def measure_resume_and_survival():
    """Measures 1, 2, 3 and 4 on one session, launched by a launcher that dies by `kill -9`."""
    result = {}
    cwd = working_directory("resume")
    prompt = "Esegui con Bash il comando `echo alfa`. Poi rispondi solo: fatto."
    launcher_code = (
        "import subprocess, sys, time\n"
        f"out = subprocess.run(['claude', '--bg', '--name', 'antfarm probe · resume', *{FLAGS!r}, {prompt!r}], capture_output=True, text=True)\n"
        "print(out.stdout + out.stderr, flush=True)\n"
        "time.sleep(600)\n"
    )
    launcher = subprocess.Popen([sys.executable, "-c", launcher_code], cwd=cwd, env=base_environment(),
                                stdout=subprocess.PIPE, text=True)
    first_line = launcher.stdout.readline()
    hit = libmatch.LAUNCH_PATTERN.search(first_line)
    if not hit:
        launcher.kill()
        return {"error": f"launch through the launcher failed: {first_line!r}"}
    short_id = hit.group(1)
    entry = session(short_id)
    pid, session_id = entry["pid"], entry["sessionId"]
    starttime = libmatch.process_start_time(pid)
    result["4_survival"] = {
        "launcher_pid": launcher.pid, "session_pid": pid, "session_parent_pid": parent_pid(pid),
        "session_parent_command": command_line(parent_pid(pid)),
    }
    os.kill(launcher.pid, signal.SIGKILL)
    launcher.wait()
    time.sleep(5)
    result["4_survival"]["alive_5s_after_kill"] = alive(pid, starttime)
    status, _ = wait_turn_end(short_id)
    result["4_survival"]["status_after_turn"] = status
    result["4_survival"]["alive_after_turn"] = alive(pid, starttime)
    result["4_survival"]["commands_of_first_turn"] = bash_commands(session_id)

    # 2 · resume on a session at the end of its turn, process alive
    code, output, copy_id = launch(["--resume", session_id, *FLAGS, "Esegui con Bash il comando `echo gamma`. Poi rispondi solo: fatto."],
                                   cwd, base_environment())
    measure_2 = {"exit": code, "output": output, "short_id": copy_id, "original_alive": alive(pid, starttime)}
    if copy_id:
        copy = session(copy_id)
        measure_2["session_id"] = copy["sessionId"]
        measure_2["same_session_id"] = copy["sessionId"] == session_id
        measure_2["same_pid"] = copy["pid"] == pid
        measure_2["status_after_turn"] = wait_turn_end(copy_id)[0]
        measure_2["copy_commands"] = bash_commands(copy["sessionId"])
        stop(copy_id)
    measure_2["original_commands"] = bash_commands(session_id)
    result["2_resume_on_live_session"] = measure_2

    # 1 · stop, then resume without the launch flags
    stop(short_id)
    measure_1 = {"stopped": wait_gone(pid, starttime)}
    code, output, resumed_id = launch(["--resume", session_id, "Esegui con Bash il comando `echo beta`. Poi rispondi solo: fatto."],
                                      cwd, base_environment())
    measure_1["without_flags"] = {"exit": code, "output": output, "short_id": resumed_id}
    if resumed_id:
        resumed = session(resumed_id)
        resumed_pid = resumed["pid"]
        resumed_start = libmatch.process_start_time(resumed_pid)
        measure_1["without_flags"].update({
            "same_session_id": resumed["sessionId"] == session_id,
            "pid_changed": resumed_pid != pid,
            "command_line": command_line(resumed_pid),
            "status_after_turn": wait_turn_end(resumed_id)[0],
        })
        stop(resumed_id)
        wait_gone(resumed_pid, resumed_start)
        time.sleep(3)
    # 1 · resume with the launch flags passed again
    code, output, resumed_id = launch(["--resume", session_id, *FLAGS, "Esegui con Bash il comando `echo delta`. Poi rispondi solo: fatto."],
                                      cwd, base_environment())
    measure_1["with_flags"] = {"exit": code, "output": output, "short_id": resumed_id}
    if resumed_id:
        resumed = session(resumed_id)
        resumed_pid = resumed["pid"]
        resumed_start = libmatch.process_start_time(resumed_pid)
        measure_1["with_flags"].update({
            "same_session_id": resumed["sessionId"] == session_id,
            "pid_changed": resumed_pid != pid,
            "command_line": command_line(resumed_pid),
            "status_after_turn": wait_turn_end(resumed_id)[0],
            "alive_after_turn": alive(resumed_pid, resumed_start),
        })
        stop(resumed_id)
    measure_1["commands"] = bash_commands(session_id)
    result["1_resume_after_stop"] = measure_1

    # 3 · origin.kind and permission mode of every user record
    result["3_user_records"] = user_records(session_id)
    result["session_id"] = session_id
    return result


def environment_values(pid, names):
    try:
        entries = Path(f"/proc/{pid}/environ").read_bytes().decode(errors="replace").split("\0")
    except OSError:
        return None
    values = dict(entry.split("=", 1) for entry in entries if "=" in entry)
    return {name: values.get(name) for name in names}


def daemon_running():
    return any("daemon" in (command_line(int(entry.name)) or [""])[1:2] and "claude" in (command_line(int(entry.name)) or [""])[0]
               for entry in Path("/proc").iterdir() if entry.name.isdigit())


def measure_environment():
    """Measure 5: two launches with HOME and PATH only plus a marker of their own, the second while
    the first is alive. The first can start the background service; the second finds it running.
    For each: whether the launch works, and which marker the session process carries."""
    attempts = []
    for label in ("first", "second"):
        environment = {key: os.environ[key] for key in MINIMAL_ENVIRONMENT}
        environment[MARKER] = label
        environment[f"{MARKER}_{label.upper()}"] = "1"
        cwd = working_directory(f"environment-{label}")
        service_before = daemon_running()
        code, output, short_id = launch(["--safe-mode", "--name", f"antfarm probe · environment {label}", "--tools", "",
                                         "--model", MODEL, "Rispondi solo: ok."], cwd, environment)
        attempt = {"variables": sorted(environment), "service_running_before": service_before, "exit": code,
                   "output": output[:300], "short_id": short_id}
        attempts.append(attempt)
        if not short_id:
            break
        entry = session(short_id)
        attempt["session_markers"] = environment_values(entry["pid"], [MARKER, f"{MARKER}_FIRST", f"{MARKER}_SECOND"])
        attempt["session_environment"] = environment_names(entry["pid"])
        attempt["status_after_turn"] = wait_turn_end(short_id)[0]
        attempt["transcript"] = libmatch.transcript(entry["sessionId"]) is not None
    for attempt in attempts:
        if attempt.get("short_id"):
            stop(attempt["short_id"])
    return {"attempts": attempts}


def main():
    report = {"state": str(STATE), "claude": subprocess.run(["claude", "--version"], capture_output=True, text=True).stdout.strip()}
    try:
        log("measure 5 · environment")
        report["5_environment"] = measure_environment()
        log("measures 1-4 · resume and survival")
        report["resume"] = measure_resume_and_survival()
    finally:
        for short_id in launched:
            stop(short_id)
        shutil.rmtree(STATE, ignore_errors=True)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    complete = "error" not in report["resume"] and all(attempt.get("short_id") for attempt in report["5_environment"]["attempts"])
    return 0 if complete else 1


if __name__ == "__main__":
    sys.exit(main())
