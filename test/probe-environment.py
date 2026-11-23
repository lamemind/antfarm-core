#!/usr/bin/env python3
"""Probe of the environment of Claude Code background sessions: what the `env` block of --settings
does against the environment of the background service, and what a session launched by the engine
carries when someone else started the service.

A `claude --bg` session is a child of the background service (`claude daemon run --origin
transient`), and its process carries the environment of whoever started the service, not of whoever
launches it (test/probe-resume.py, measure 5). The probe has two parts:

    settings  the `env` block of --settings, on bare `claude --bg` launches
              (a) a service started with a marker, and a session that declares the same name in the
                  block: which value a command of the session sees
              (b) an empty value in the block, on a name the service has and on a name it lacks:
                  variable absent, or present and empty
              (c) the names that a session process carries under a service started with HOME and
                  PATH only: the expected names of the engine (EXPECTED_VARIABLES in bin/match.py)
              (d) `claude stop`, then a resume under a service that another process restarted: the
                  environment of the new process, and the block
    engine    launches through the engine code — `Match.launch_node` of bin/match.py, `Loop.spawn`
              and `Loop.wake` of bin/loop.py — in two cases: the launch starts the service, and the
              service is already alive, started by a process with a marker. For every launch: the
              names in /proc/<pid>/environ of the session process, the values of the markers, what a
              Bash command of the session sees, and the `foreign_variables` field that the engine
              writes, if it writes one. `--engine <dir>` takes the engine from another directory,
              such as a `git archive` of an earlier commit

A session runs one command, the probe itself — `<probe> report <label>`, or `<probe> recv` after a
wake of the loop — which prints one JSON line: the names of its environment and the values of the
probe's own markers, never the value of another variable. The launch flags allow that command only:
in the engine part the probe points the bus path of bin/match.py and the request path of bin/loop.py
to itself, and changes nothing else of the launch.

The probe does not stop a background service that it did not start: if one is alive at the start,
it exits 3 and says so (`claude daemon stop` also terminates the sessions of the service). It runs on
a temporary state root and stops every session it launched. Every session is one short haiku turn.

Usage:  antfarm-core/test/probe-environment.py settings
        antfarm-core/test/probe-environment.py engine [--engine <dir>]
It prints the report as JSON on stdout. Exits 0 when every measure produced a value, 1 otherwise,
3 when a background service is alive at the start.
"""

import argparse
import contextlib
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PROBE = Path(__file__).resolve()
OWN_ENGINE = PROBE.parent.parent
MODEL = "haiku"
TURN_WAIT_S = 180
SERVICE_WAIT_S = 60
RESUME_WAIT_S = 30
MINIMAL_ENVIRONMENT = ("HOME", "PATH")
PREFIX = "ANTFARM_PROBE_"
MARKER = f"{PREFIX}MARKER"
EMPTY_ON_SERVICE_NAME = f"{PREFIX}EMPTY_ON_SERVICE_NAME"
EMPTY_ON_NEW_NAME = f"{PREFIX}EMPTY_ON_NEW_NAME"
SERVICE_MARKER = f"{PREFIX}SERVICE"
LAUNCHER_MARKER = f"{PREFIX}LAUNCHER"
REPORT_TAG = "antfarm-probe-environment"
EXIT_SERVICE_ALIVE = 3

STATE = None
libmatch = None
launched = []


# <editor-fold defaultstate="collapsed" desc="The command of the sessions">
def report(label):
    """The one line a session prints: the names of the environment of the command, and the values
    of the probe's markers. A marker that is not set is not in `markers`."""
    print(json.dumps({"probe": REPORT_TAG, "label": label, "names": sorted(os.environ),
                      "markers": {name: value for name, value in os.environ.items() if name.startswith(PREFIX)}},
                     ensure_ascii=False))
    return 0
# </editor-fold>


# <editor-fold defaultstate="collapsed" desc="Processes">
def log(text):
    print(text, file=sys.stderr, flush=True)


def command_line(pid):
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().decode(errors="replace").split("\0")[:-1]
    except OSError:
        return None


def parent_pid(pid):
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as stat_file:
            content = stat_file.read()
    except OSError:
        return None
    return int(content[content.rindex(")") + 2:].split()[1])


def process_environment(pid):
    """Every name in /proc/<pid>/environ, and the values of the probe's markers only; None if the
    file cannot be read. The file is the environment the process was born with."""
    try:
        entries = Path(f"/proc/{pid}/environ").read_bytes().decode(errors="replace").split("\0")
    except OSError:
        return None
    values = dict(entry.split("=", 1) for entry in entries if "=" in entry)
    return {"names": sorted(values), "markers": {name: value for name, value in values.items() if name.startswith(PREFIX)}}


def service_pids():
    """The live background services of Claude Code: processes whose command line carries
    `daemon run` after a `claude` word."""
    pids = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        args = command_line(int(entry.name)) or []
        if "daemon" in args and "run" in args and any("claude" in Path(word).name for word in args[:2]):
            pids.append(int(entry.name))
    return pids


def service_alive():
    status = subprocess.run(["claude", "daemon", "status"], capture_output=True, text=True, timeout=60)
    return bool(service_pids()) or not status.stdout.strip().startswith("not running")


def service_of(pid):
    """The background service among the ancestors of a process, or None."""
    services = set(service_pids())
    current = pid
    while current and current > 1:
        current = parent_pid(current)
        if current in services:
            return current
    return None


def wait_service_gone():
    limit = time.monotonic() + SERVICE_WAIT_S
    while service_alive() and time.monotonic() < limit:
        time.sleep(1)
    return not service_alive()
# </editor-fold>


# <editor-fold defaultstate="collapsed" desc="Sessions">
def prompt(label):
    return f"Esegui con Bash il comando `{PROBE} report {label}`. Poi rispondi solo: fatto."


def minimal_environment(**markers):
    return {**{name: os.environ[name] for name in MINIMAL_ENVIRONMENT}, **markers}


def stop(short_id):
    subprocess.run(["claude", "stop", short_id], capture_output=True, text=True, timeout=60)


def stop_all():
    for short_id in launched:
        stop(short_id)


def wait_turn_end(short_id):
    """The status of the session at the end of the wait: anything other than `busy`, or `busy` at the timeout."""
    limit = time.monotonic() + TURN_WAIT_S
    time.sleep(3)
    while True:
        status = libmatch.session_states().get(short_id, {}).get("status")
        if status != "busy" or time.monotonic() >= limit:
            return status
        time.sleep(3)


def observed(pid):
    """What the probe reads of a session process as soon as it exists: its environment, and the
    environment of the service it is a child of."""
    service = service_of(pid)
    return {"pid": pid, "process": process_environment(pid), "service_pid": service,
            "service": process_environment(service) if service else None}


def command_reports(session_id):
    """What the commands of a session printed: the probe's JSON line for each call that ran it, the
    command and the outcome for each call that did not; None without a transcript."""
    path = libmatch.transcript(session_id)
    if path is None:
        return None
    reports = []
    for name, command, outcome, error in libmatch.bash_calls(path):
        line = next((data for data in map(json_line, outcome.splitlines()) if data and data.get("probe") == REPORT_TAG), None)
        reports.append(line if line else {"tool": name, "command": command, "outcome": outcome[:300], "error": error})
    return reports


def json_line(line):
    try:
        data = json.loads(line)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def bare_launch(label, environment, with_report=True, settings=None):
    """`claude --bg` without the engine. With `with_report` the session may run the probe and is
    asked to; without it, no tool and a one-word answer: a session that only keeps the service alive."""
    cwd = STATE / "sessions" / label
    cwd.mkdir(parents=True)
    arguments = ["--safe-mode", "--name", f"antfarm probe · environment · {label}", "--model", MODEL]
    if with_report:
        arguments += ["--tools", "Bash", "--allowedTools", f"Bash({PROBE} *)", "--permission-mode", "dontAsk"]
    else:
        arguments += ["--tools", ""]
    if settings is not None:
        arguments += ["--settings", json.dumps({"env": settings})]
    text = prompt(label) if with_report else "Rispondi solo: ok."
    outcome = subprocess.run(["claude", "--bg", *arguments, text], cwd=cwd, env=environment,
                             capture_output=True, text=True, timeout=120)
    output = (outcome.stdout + outcome.stderr).strip()
    hit = libmatch.LAUNCH_PATTERN.search(output)
    result = {"label": label, "variables": sorted(environment), "settings_env": settings, "exit": outcome.returncode,
              "output": output[:300], "short_id": hit.group(1) if hit else None}
    if not hit:
        return result, None
    entry = libmatch.wait_for_session(hit.group(1))
    launched.append(entry["id"])
    result.update(session_id=entry["sessionId"], **observed(entry["pid"]))
    return result, entry


def finish_turn(result, short_id, session_id):
    result["status_after_turn"] = wait_turn_end(short_id)
    result["commands"] = command_reports(session_id)


def resume(session_id, short_id, old_pid, label, environment, cwd):
    """`claude --bg --resume <sessionId> <text>` without flags, as the loop wakes a session; the
    new process is the one whose pid differs from the old one."""
    outcome = subprocess.run(["claude", "--bg", "--resume", session_id, prompt(label)], cwd=cwd, env=environment,
                             capture_output=True, text=True, timeout=120)
    output = (outcome.stdout + outcome.stderr).strip()
    hit = libmatch.LAUNCH_PATTERN.search(output)
    result = {"label": label, "variables": sorted(environment), "exit": outcome.returncode, "output": output[:300],
              "short_id": hit.group(1) if hit else None}
    if not hit:
        return result
    result["same_short_id"] = hit.group(1) == short_id
    limit = time.monotonic() + RESUME_WAIT_S
    entry = libmatch.wait_for_session(hit.group(1))
    while entry.get("pid") == old_pid and time.monotonic() < limit:
        time.sleep(1)
        entry = libmatch.session_states().get(hit.group(1), entry)
    if entry["id"] not in launched:
        launched.append(entry["id"])
    result.update(same_session_id=entry["sessionId"] == session_id, **observed(entry["pid"]))
    finish_turn(result, entry["id"], entry["sessionId"])
    return result
# </editor-fold>


# <editor-fold defaultstate="collapsed" desc="Part settings">
def part_settings():
    """Measures (c), then (a) and (b) on one session, then (d) on the same session."""
    result = {}

    log("settings · (c) service started with HOME and PATH only: the launch that starts it, then a second one")
    for label in ("c1", "c2"):
        measure_c, entry = bare_launch(label, minimal_environment())
        if entry:
            finish_turn(measure_c, entry["id"], entry["sessionId"])
        result[f"{label}_minimal_service"] = measure_c
    stop_all()
    result["c_service_gone"] = wait_service_gone()

    log("settings · (a) (b) service started with a marker, same name and empty values in the env block")
    keeper, keeper_entry = bare_launch("ab-service", minimal_environment(**{MARKER: "service", EMPTY_ON_SERVICE_NAME: "service"}),
                                       with_report=False)
    result["ab_service_starter"] = keeper
    settings = {MARKER: "settings", EMPTY_ON_SERVICE_NAME: "", EMPTY_ON_NEW_NAME: ""}
    measure_ab, session = bare_launch("ab", minimal_environment(), settings=settings)
    if session:
        finish_turn(measure_ab, session["id"], session["sessionId"])
    result["ab_settings_against_service"] = measure_ab
    if not (keeper_entry and session):
        return result

    log("settings · (d) stop, service restarted by another process, resume")
    stop_all()
    result["d_service_gone"] = wait_service_gone()
    restarter, _ = bare_launch("d-service", minimal_environment(**{MARKER: "service-d", f"{PREFIX}SERVICE_D": "1"}),
                               with_report=False)
    result["d_service_starter"] = restarter
    result["d_resume"] = resume(session["sessionId"], session["id"], session["pid"], "d", minimal_environment(),
                                STATE / "sessions" / "ab")
    return result


def settings_complete(result):
    keys = ("c1_minimal_service", "c2_minimal_service", "ab_settings_against_service", "d_resume")
    return all(result.get(key, {}).get("commands") for key in keys) and result.get("c_service_gone") \
        and result.get("d_service_gone")
# </editor-fold>


# <editor-fold defaultstate="collapsed" desc="Part engine">
def engine_identity(engine):
    """The engine the launches went through: the sha256 of the two files that launch, and whether
    they carry the names of the change (D1, D2)."""
    files = {name: hashlib.sha256((engine / "bin" / name).read_bytes()).hexdigest()[:12] for name in ("match.py", "loop.py")}
    return {"directory": str(engine), "sha256": files,
            "PASSED_VARIABLES": list(getattr(libmatch, "PASSED_VARIABLES", ())) or None,
            "VARIABLES_NOT_PASSED": list(getattr(libmatch, "VARIABLES_NOT_PASSED", ())) or None,
            "EXPECTED_VARIABLES": list(getattr(libmatch, "EXPECTED_VARIABLES", ())) or None}


def match_launch(case, graph_file):
    """One node through `Match.launch_node`, and the `foreign_variables` field of its entry in
    nodes.json: the string `absent` if the engine does not write it."""
    match = libmatch.Match.create(f"probe-environment-{case}", "probe", graph_file, {"probe": case})
    data = match.launch_node("A", prompt(f"match-{case}"), "probe of the environment", model=MODEL)
    launched.append(data["short_id"])
    result = {"match": match.id, "short_id": data["short_id"], **observed(data["pid"]),
              "engine_field": match.nodes()["nodes"]["A"].get("foreign_variables", "absent")}
    finish_turn(result, data["short_id"], data["sessionId"])
    return result


def loop_row(loop_module, event, session_id=None):
    """The last row of registry/loop.jsonl with this event, and with this session if one is given."""
    path = loop_module.libstate.Paths(STATE).log
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return next((row for row in reversed(rows)
                 if row.get("event") == event and session_id in (None, row.get("session"))), None)


def loop_launch(loop_module, loop, case, wake):
    """One session through `Loop.spawn`, then, with `wake`, through `Loop.wake`; for each step the
    `foreign_variables` field of its row in loop.jsonl, the string `absent` if the engine does not
    write it."""
    session_id = loop.spawn("root", {"kind": "human", "session": None}, None, f"probe-{case}")
    if session_id is None:
        return {"error": f"spawn failed: {(loop_row(loop_module, 'spawn-failed') or {}).get('detail')}"}
    entry = loop.sessions()[session_id]
    launched.append(entry["short_id"])
    spawn_row = loop_row(loop_module, "spawn", session_id)
    result = {"session": session_id, "short_id": entry["short_id"], **observed(entry["pid"]),
              "engine_field": spawn_row.get("foreign_variables", "absent")}
    finish_turn(result, entry["short_id"], entry["sessionId"])
    if not wake:
        return result
    old_pid = entry["pid"]
    loop.wake(session_id, entry)
    wake_row = loop_row(loop_module, "wake", session_id)
    if wake_row is None:
        result["wake"] = {"error": (loop_row(loop_module, "wake-failed", session_id) or {}).get("detail")}
        return result
    result["wake"] = {"pid_changed": entry["pid"] != old_pid, **observed(entry["pid"]),
                      "engine_field": wake_row.get("foreign_variables", "absent")}
    finish_turn(result["wake"], entry["short_id"], entry["sessionId"])
    return result


def part_engine(engine):
    """Case 1, the launch starts the service: a match node, then — after the service is gone — a
    loop session. Case 2, the service already alive, started by a process with a marker: a match
    node, a loop session, a wake of that session. The probe carries a marker of its own as the
    launcher, so that a launch filter that lets it through shows it in the session."""
    import loop as loop_module  # noqa: E402 — after ANTFARM_STATE_DIR and the path of the engine
    libmatch.BUS = PROBE
    loop_module.REQUEST = PROBE
    os.environ[LAUNCHER_MARKER] = "launcher"
    graph_file = engine / "games" / "telephone" / "graph.json"
    grant = {"position": "root", "description": "probe of the environment", "parent": "human",
             "max_sessions": 5, "budget_tokens": 10_000_000,
             "launch": {"type": "llm", "model": MODEL,
                        "prompt": "Esegui con Bash il comando `{request} report {session}`. Poi rispondi solo: fatto."}}
    loop = loop_module.Loop(loop_module.libstate.Paths(STATE), 1.0, notify=False)
    loop.grants = {"root": grant}
    result = {"engine": engine_identity(engine)}

    log("engine · case 1 · the launch starts the service: match node")
    result["1_launch_starts_service"] = {"match": match_launch("starts", graph_file)}
    stop_all()
    result["1_launch_starts_service"]["service_gone_after_match"] = wait_service_gone()
    log("engine · case 1 · the launch starts the service: loop session")
    result["1_launch_starts_service"]["loop"] = loop_launch(loop_module, loop, "starts", wake=False)
    stop_all()
    result["1_launch_starts_service"]["service_gone_after_loop"] = wait_service_gone()

    log("engine · case 2 · service alive, started by a process with a marker")
    keeper, keeper_entry = bare_launch("engine-service", minimal_environment(**{SERVICE_MARKER: "service"}), with_report=False)
    result["2_service_alive"] = {"service_starter": keeper}
    if keeper_entry is None:
        return result
    result["2_service_alive"]["match"] = match_launch("alive", graph_file)
    log("engine · case 2 · loop session and wake")
    result["2_service_alive"]["loop"] = loop_launch(loop_module, loop, "alive", wake=True)
    return result


def engine_complete(result):
    first, second = result.get("1_launch_starts_service", {}), result.get("2_service_alive", {})
    measures = [first.get("match"), first.get("loop"), second.get("match"), second.get("loop"),
                (second.get("loop") or {}).get("wake")]
    return all(measure and measure.get("commands") for measure in measures)
# </editor-fold>


def main():
    if len(sys.argv) > 1 and sys.argv[1] in ("report", "recv"):
        return report(" ".join(sys.argv[2:]) or sys.argv[1])
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("part", choices=("settings", "engine"))
    parser.add_argument("--engine", type=Path, default=OWN_ENGINE, help="the engine directory (default: this one)")
    args = parser.parse_args()
    engine = args.engine.resolve()

    if service_alive():
        print(f"a background service of Claude Code is alive (pids {service_pids() or '?'}): the probe does not "
              "stop a service it did not start, because `claude daemon stop` terminates its sessions. "
              "Run it again when `claude daemon status` says `not running`.", file=sys.stderr)
        return EXIT_SERVICE_ALIVE

    global STATE, libmatch
    STATE = Path(tempfile.mkdtemp(prefix="antfarm-probe-environment-"))
    os.environ["ANTFARM_STATE_DIR"] = str(STATE)
    sys.path.insert(0, str(engine / "bin"))
    import match  # noqa: E402 — after ANTFARM_STATE_DIR, which match.py reads at import
    libmatch = match

    report_data = {"part": args.part, "state": str(STATE),
                   "claude": subprocess.run(["claude", "--version"], capture_output=True, text=True).stdout.strip()}
    try:
        if args.part == "settings":
            report_data["measures"] = part_settings()
            complete = settings_complete(report_data["measures"])
        else:
            with contextlib.redirect_stdout(sys.stderr):  # the loop prints its log rows on stdout
                report_data["measures"] = part_engine(engine)
            complete = engine_complete(report_data["measures"])
    finally:
        stop_all()
        report_data["service_gone_at_end"] = wait_service_gone()
        shutil.rmtree(STATE, ignore_errors=True)
    print(json.dumps(report_data, ensure_ascii=False, indent=2))
    return 0 if complete else 1


if __name__ == "__main__":
    sys.exit(main())
