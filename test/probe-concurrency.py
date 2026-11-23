#!/usr/bin/env python3
"""Proof of the concurrency of the loop on haiku: three launches that run together.

It starts the loop with --concurrency CONCURRENCY on a temporary state root with the grants of
test/grants/concurrency: a root whose prompt asks three spawns on lead in one Bash command, joined by
`&&`, and lead with cap 3. It switches the root on, waits for the three spawn rows on lead, and
measures, after switching lead on and then the root:

    1. overlap   the three launches, each from the `started_ts` to the `ts` of its spawn row, have an
                 instant in common
    2. together  the registry carries the three lead sessions alive at the same time, with their
                 processes
    3. accepted  every `request spawn` of the root was answered before its launch ended: the three
                 requests were decided as spawned, and each one was marked `launching` first

A `request spawn` answers when the loop accepts it, so the three requests reach the loop one cycle
apart: «in the same cycle» of the acceptance criterion reads «asked with one command». It stops every
session it launched and the loop before exiting, and spends a few thousand haiku tokens.

Usage:  antfarm-core/test/probe-concurrency.py [--json] [--keep]
Exits 0 when the three measures pass, 1 otherwise.
"""

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

ENGINE = Path(__file__).resolve().parent.parent
GRANTS = ENGINE / "test" / "grants" / "concurrency"
LOOP = ENGINE / "bin" / "loop.py"
CONCURRENCY = 3
CYCLE = "0.2"
WAIT_S = 300


def log(text):
    print(text, file=sys.stderr, flush=True)


def rows(state):
    path = state / "registry" / "loop.jsonl"
    try:
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except OSError:
        return []


def sessions(state):
    try:
        return json.loads((state / "loop" / "sessions.json").read_text(encoding="utf-8"))["sessions"]
    except (OSError, ValueError, KeyError):
        return {}


def wait_until(predicate, seconds):
    limit = time.monotonic() + seconds
    while time.monotonic() < limit:
        value = predicate()
        if value:
            return value
        time.sleep(0.5)
    return None


def alive(pid):
    try:
        content = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return False
    return content[content.rindex(")") + 2:].split()[0] != "Z"


def measure(state):
    at = lambda text: datetime.fromisoformat(text).timestamp()  # noqa: E731
    spawn_rows = [row for row in rows(state) if row["event"] == "spawn" and row["position"] == "lead"]
    intervals = [(at(row["started_ts"]), at(row["ts"])) for row in spawn_rows]
    common = max(start for start, _ in intervals) < min(end for _, end in intervals)
    lead = {session_id: entry for session_id, entry in sessions(state).items() if entry["position"] == "lead"}
    live = sorted(session_id for session_id, entry in lead.items()
                  if entry["status"] == "alive" and alive(entry["pid"]))
    decided = [json.loads(path.read_text(encoding="utf-8"))
               for path in (state / "loop" / "requests" / "done").glob("[0-9]*.json")]
    spawn_requests = [request for request in decided if request.get("kind") == "spawn"]
    launching_rows = {row["request"] for row in rows(state) if row["event"] == "launching" and row["position"] == "lead"}
    return {
        "launches": [{"session": row["session"], "started_ts": row["started_ts"], "ts": row["ts"],
                      "seconds": round(end - start, 2)} for row, (start, end) in zip(spawn_rows, intervals)],
        "1_overlap": {"common_instant": common,
                      "from": datetime.fromtimestamp(max(start for start, _ in intervals)).isoformat(timespec="milliseconds"),
                      "to": datetime.fromtimestamp(min(end for _, end in intervals)).isoformat(timespec="milliseconds")},
        "2_together": {"alive": live, "pass": len(live) == 3},
        "3_accepted": {"spawned": sorted(request["session"] for request in spawn_requests if request["outcome"] == "spawned"),
                       "marked_launching_first": all(request["id"] in launching_rows for request in spawn_requests),
                       "pass": len(spawn_requests) == 3 and all(request["outcome"] == "spawned" for request in spawn_requests)
                       and all(request["id"] in launching_rows for request in spawn_requests)},
    }


def main():
    as_json = "--json" in sys.argv[1:]
    keep = "--keep" in sys.argv[1:]
    folder = Path(tempfile.mkdtemp(prefix="antfarm-probe-concurrency-"))
    state = folder / "state"
    (state / "grants").mkdir(parents=True)
    for grant in GRANTS.glob("*.json"):
        shutil.copy(grant, state / "grants" / grant.name)
    environment = {**os.environ, "ANTFARM_STATE_DIR": str(state)}
    output = open(folder / "loop.out", "w", encoding="utf-8")
    loop = subprocess.Popen([str(LOOP), "--cycle", CYCLE, "--concurrency", str(CONCURRENCY), "--no-notify"],
                            env=environment, stdout=output, stderr=subprocess.STDOUT)
    result = {"state": str(state), "concurrency": CONCURRENCY}
    try:
        if not wait_until(lambda: (state / "loop" / "status.json").exists(), 15):
            raise SystemExit(f"the loop does not start: {(folder / 'loop.out').read_text()}")
        log("switch lead on, then root: the root asks three spawns on lead with one command")
        for position in ("lead", "root"):
            subprocess.run([str(LOOP), "switch", position, "on"], env=environment, capture_output=True, text=True,
                           timeout=30)
        found = wait_until(lambda: len([row for row in rows(state) if row["event"] == "spawn" and row["position"] == "lead"]) >= 3,
                           WAIT_S)
        if not found:
            result["error"] = "three spawn rows on lead did not come"
        else:
            result.update(measure(state))
    finally:
        loop.send_signal(signal.SIGTERM)
        try:
            loop.wait(timeout=180)
        except subprocess.TimeoutExpired:
            loop.kill()
        for entry in sessions(state).values():
            if entry.get("short_id"):
                subprocess.run(["claude", "stop", entry["short_id"]], capture_output=True, text=True, timeout=60)
        result["log"] = [f"{row['ts'][11:23]} {row['event']} {row.get('session') or ''} {row.get('detail') or ''}".strip()
                         for row in rows(state)]
        if not keep:
            shutil.rmtree(folder, ignore_errors=True)
    passed = "error" not in result and result["1_overlap"]["common_instant"] and result["2_together"]["pass"] \
        and result["3_accepted"]["pass"]
    result["pass"] = bool(passed)
    if as_json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        for key, value in result.items():
            print(f"{key}: {json.dumps(value, ensure_ascii=False)}")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
