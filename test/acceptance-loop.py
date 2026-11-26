#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["playwright==1.60.0"]
# ///
"""Acceptance test of the loop: in neutral on the fixture, the view from the DOM with headless Chromium
driven by Playwright, and on request one real session. Without --real-session no LLM: the sessions
are script sessions (test/loop-node.py, on the grants of test/grants/script).

    antfarm-core/test/acceptance-loop.py [--real-session] [--neutral-s <seconds>] [--screenshots <directory>] [--keep]

Every scenario runs on a temporary state root of its own with a copy of the grants. Every figure it
compares is read here from the files of the loop — loop/status.json, switches.json, sessions.json,
mailboxes/, requests/, interpellations/, registry/loop.jsonl — without going through the code of
the loop or of the view:

- in neutral: the loop on the LLM fixture (test/grants/fixture) runs NEUTRAL_S seconds with every
  switch off, with a `claude` first on its PATH that writes down every call and then runs the real
  one: 0 spawn and 0 launching rows, 0 calls to `claude`, and 0 `claude` processes with their
  working directory under the state root, looked for in /proc every quarter of a second

- in neutral: one [data-position] per position of the status, one switch each, all off, no live
  session, no message
- the port: a POST /switch from a page of another origin → 403 and no request file; a position
  that is not an id → 400; a GET on /switch → 405; the WebSocket from another origin → 403
- the switch: the click on the switch of the root gives one spawn row on root in the log of the
  loop, and the session in the DOM; the click that switches it off gives the switch-off row and no
  new spawn on root in the next 3 s, and the session stays alive
- the state: with live sessions on two positions, messages held in the mailbox of a position
  switched off and a queued request, positions, switches, live sessions, messages in mailbox and
  pending requests of the DOM are those of the files
- the interpellation: an escalation with package from the root appears in the panel, with
  alternatives, recommendation and default equal to the file, and the panel has no control
- the budget: a position over its budget has its switch off in the DOM, with reason budget, and
  the log of the DOM is registry/loop.jsonl row by row
- the loop dies: after its kill -9 the view stays up, says that the loop is down, disables the
  switches and still lists the live sessions
- a slow launch: on a second state root, with a fake `claude` (test/fake-claude.py) that takes
  LAUNCH_S for a launch, the session is in the DOM as launching, with its operation, while the
  registry says launching, and alive once the registry says alive
- the reporters: on a third state root, with the fake `claude`, the grants of test/grants/fake-gazette
  — a reporter position with the parameter `repo`, four values, cap 4 —, the human asks four spawns
  on reporter, one per value; under [data-position="reporter"] the DOM carries one alive session per
  value, each with its `repo` parameter equal to the registry, and the four values of the grant
- a real session, only with --real-session: on the LLM fixture, the switch of the root starts a
  Claude Code session on haiku; its prompt asks a spawn on lead, which is off, and the log carries
  the request queued; once the session has ended its turn the test deposits a message for it, the
  loop wakes it with the resume and the log carries its second action, the read of the mailbox. In
  its transcript, after the launch, every `user` entry that is not a tool_result is the fixed wake
  text of the loop (WAKE_TEXT of bin/loop.py), one per wake row. It spends the tokens of two short
  turns of haiku, and the session is stopped at the end

With --screenshots it saves one screenshot per step; with --keep the temporary folder stays, with
the log of the loop and of the view. Exits 1 if a check fails.
"""

import argparse
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

ANTFARM = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ANTFARM / "bin"))
import loopstate as libstate  # noqa: E402  only to deposit messages, as test/loop-tests.sh does

LOOP = ANTFARM / "bin" / "loop.py"
VIEW = ANTFARM / "bin" / "loop-view.py"
GRANTS = ANTFARM / "test" / "grants" / "script"
FIXTURE_GRANTS = ANTFARM / "test" / "grants" / "fixture"
FAKE_GRANTS = ANTFARM / "test" / "grants" / "fake"
GAZETTE_GRANTS = ANTFARM / "test" / "grants" / "fake-gazette"
REPOS = ("factory", "tooling", "knowledge-series", "antfarm-core")
FAKE_CLAUDE = ANTFARM / "test" / "fake-claude.py"
LAUNCH_S = 5
CYCLE = "0.2"
WAIT_S = 15
WAIT_MS = WAIT_S * 1000
FOREIGN_ORIGIN = "http://example.org"
NEUTRAL_S = 60
# A real session: how long the launch, and one turn of haiku, may take.
REAL_LAUNCH_S = 180
REAL_TURN_S = 300

failed = 0


def verify(description, outcome, evidence=""):
    global failed
    if not outcome:
        failed += 1
    print(f"  {'ok  ' if outcome else 'FAIL'} {description}" + (f" — {evidence}" if evidence and not outcome else ""))


def wait_until(predicate, seconds=WAIT_S):
    limit = time.monotonic() + seconds
    while True:
        value = predicate()
        if value or time.monotonic() >= limit:
            return value
        time.sleep(0.2)


# <editor-fold desc="The files of the loop, read here">
class Disk:
    def __init__(self, state):
        self.state = state
        self.loop = state / "loop"
        self.log = state / "registry" / "loop.jsonl"

    def read(self, path, default=None):
        try:
            return json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return default

    def ids(self, directory):
        found = []
        for path in sorted(Path(directory).glob("[0-9]*.json")):
            entry = self.read(path)
            if entry is not None:
                found.append(entry["id"])
        return sorted(found)

    def rows(self):
        try:
            return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines() if line.strip()]
        except OSError:
            return []

    def count(self, **fields):
        return sum(1 for row in self.rows() if all(row.get(key) == value for key, value in fields.items()))

    def sessions(self):
        return self.read(self.loop / "sessions.json", {"sessions": {}})["sessions"]

    def snapshot(self):
        """What the DOM must show, from the files: positions of the status, switches, live
        sessions and mailbox messages of each position, pending requests, interpellations."""
        positions = sorted((self.read(self.loop / "status.json", {}) or {}).get("positions", {}))
        switches = self.read(self.loop / "switches.json", {})
        sessions = self.sessions()
        return {
            "positions": positions,
            "switches": {position: bool((switches.get(position) or {}).get("on")) for position in positions},
            "alive": {position: sorted(session_id for session_id, entry in sessions.items()
                                       if entry["position"] == position and entry["status"] == "alive")
                      for position in positions},
            "mailbox": {position: self.ids(self.loop / "mailboxes" / position) for position in positions},
            "requests": self.ids(self.loop / "requests"),
            "interpellations": self.ids(self.loop / "interpellations"),
        }
# </editor-fold>


# <editor-fold desc="The DOM, measured">
def dom_snapshot(page):
    return page.evaluate("""() => {
        const positions = [...document.querySelectorAll('[data-position]')];
        const ids = (root, selector, field) => [...root.querySelectorAll(selector)].map(node => node.dataset[field]).sort();
        return {
            positions: positions.map(p => p.dataset.position).sort(),
            switches: Object.fromEntries(positions.map(p => [p.dataset.position, p.querySelector('[data-switch]')?.getAttribute('aria-checked') === 'true'])),
            alive: Object.fromEntries(positions.map(p => [p.dataset.position, ids(p, '[data-session][data-status=alive]', 'session')])),
            mailbox: Object.fromEntries(positions.map(p => [p.dataset.position, ids(p, '[data-message]', 'message')])),
            requests: ids(document, '[data-request]', 'request'),
            interpellations: ids(document, '[data-interpellation]', 'interpellation'),
        };
    }""")


def switch_count(page):
    return page.locator("[data-switch]").count()


def dom_log_events(page):
    """The events of the log rows of the DOM, oldest first: the page shows them newest first."""
    return list(reversed(page.evaluate("() => [...document.querySelectorAll('#log [data-log-row]')].map(li => li.dataset.event)")))


def same_as_disk(page, disk, label):
    """Waits until the DOM shows what the files say, then checks it field by field."""
    pair = {}

    def equal():
        pair["disk"], pair["dom"] = disk.snapshot(), dom_snapshot(page)
        return pair["disk"] == pair["dom"]

    wait_until(equal)
    for field in ("positions", "switches", "alive", "mailbox", "requests", "interpellations"):
        verify(f"{label}: {field} of the DOM = files of the loop", pair["dom"][field] == pair["disk"][field],
               f"DOM {pair['dom'][field]} · files {pair['disk'][field]}")
    return pair["disk"]
# </editor-fold>


# <editor-fold desc="Processes">
class Processes:
    def __init__(self, state, records, loop_environment=None):
        self.state = state
        self.records = records
        self.environment = {**os.environ, "ANTFARM_STATE_DIR": str(state)}
        self.loop_environment = {**self.environment, **(loop_environment or {})}
        self.loop = None
        self.view = None
        self.address = None

    def start_loop(self):
        output = open(self.records / "loop.out", "a", encoding="utf-8")
        self.loop = subprocess.Popen([str(LOOP), "--cycle", CYCLE, "--no-notify"], env=self.loop_environment,
                                     stdout=output, stderr=subprocess.STDOUT)
        if not wait_until(lambda: (self.state / "loop" / "status.json").exists()):
            raise SystemExit(f"the loop does not write its status: {(self.records / 'loop.out').read_text()}")

    def start_view(self):
        errors = open(self.records / "view.err", "w", encoding="utf-8")
        self.view = subprocess.Popen([str(VIEW), "--port", "0"], env=self.environment,
                                     stdout=subprocess.PIPE, stderr=errors, text=True)
        first_line = self.view.stdout.readline()
        hit = re.search(r"http://127\.0\.0\.1:\d+/", first_line)
        if not hit:
            self.view.kill()
            raise SystemExit(f"the view does not start: {first_line}{(self.records / 'view.err').read_text()}")
        self.address = hit.group(0)

    def human(self, *arguments):
        """A request of the human from the loop's command line."""
        return subprocess.run([str(LOOP), *arguments], env=self.environment, capture_output=True, text=True, timeout=30)

    def stop(self):
        """The view first, with SIGTERM: it runs under `uv run`, which forwards SIGTERM to the
        Python process and cannot forward a SIGKILL, that would leave the server orphaned."""
        for process in (self.view, self.loop):
            if process is None or process.poll() is not None:
                continue
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
        sessions = Disk(self.state).sessions()
        for entry in sessions.values():
            try:
                os.killpg(entry["pid"], signal.SIGKILL)
            except (ProcessLookupError, PermissionError, KeyError):
                pass


def http(address, method, path, origin=None):
    """(status, body) of a request to the view, with or without an Origin header."""
    request = urllib.request.Request(address.rstrip("/") + path, method=method, data=b"" if method == "POST" else None)
    if origin:
        request.add_header("Origin", origin)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode()


def websocket_status(address, origin):
    """The status of a WebSocket handshake on /events sent with the given Origin."""
    request = urllib.request.Request(address.rstrip("/") + "/events", headers={
        "Origin": origin, "Connection": "Upgrade", "Upgrade": "websocket", "Sec-WebSocket-Version": "13",
        "Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ==",
    })
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status
    except urllib.error.HTTPError as error:
        return error.code
# </editor-fold>


def message_for(state, position, session, text):
    libstate.deposit_message(libstate.Paths(state), position, "human", text, to_session=session)


def screenshot(page, screenshots, name):
    if screenshots:
        page.screenshot(path=str(screenshots / f"loop-{name}.png"), full_page=True)


def appears(page, selector):
    """True as soon as the selector matches an element of the DOM, False at the end of the wait."""
    try:
        page.wait_for_selector(selector, state="attached", timeout=WAIT_MS)
        return True
    except PlaywrightTimeout:
        return False


def switch_settled(page, position, on):
    """The switch of the position shows the state, and no click on it is waiting for its answer."""
    return appears(page, f'[data-switch="{position}"][aria-checked="{str(on).lower()}"]:not([aria-busy])')


def scenario(page, processes, disk, screenshots):
    state = processes.state
    page.goto(processes.address)
    page.wait_for_selector('body[data-connection="open"][data-loop="running"]', state="attached", timeout=WAIT_MS)

    print("in neutral")
    snapshot = same_as_disk(page, disk, "neutral")
    verify("three positions: root, lead, worker", snapshot["positions"] == ["lead", "root", "worker"], f"{snapshot['positions']}")
    verify("one switch per position", switch_count(page) == 3, f"{switch_count(page)} switches")
    verify("every switch off", not any(snapshot["switches"].values()))
    verify("no live session, no message", not any(snapshot["alive"].values()) and not any(snapshot["mailbox"].values()))
    screenshot(page, screenshots, "neutral")

    print("the port")
    before = len(list((state / "loop" / "requests").rglob("[0-9]*.json")))
    status, _ = http(processes.address, "POST", "/switch?position=root&on=true", origin=FOREIGN_ORIGIN)
    verify("POST /switch from another origin → 403", status == 403, f"{status}")
    verify("… and no request file", len(list((state / "loop" / "requests").rglob("[0-9]*.json"))) == before)
    status, _ = http(processes.address, "POST", "/switch?position=1-bad&on=true")
    verify("POST /switch with a position that is not an id → 400", status == 400, f"{status}")
    status, _ = http(processes.address, "POST", "/switch?position=root&on=maybe")
    verify("POST /switch with on neither true nor false → 400", status == 400, f"{status}")
    status, _ = http(processes.address, "GET", "/switch?position=root&on=true")
    verify("GET /switch → 405", status == 405, f"{status}")
    verify("WebSocket from another origin → 403", websocket_status(processes.address, FOREIGN_ORIGIN) == 403)
    verify("… still no request file", len(list((state / "loop" / "requests").rglob("[0-9]*.json"))) == before)

    print("the switch")
    page.click('[data-switch="root"]')
    verify("click on the switch of root → one spawn row on root, reason switch",
           wait_until(lambda: disk.count(event="spawn", position="root", reason="switch") == 1))
    verify("… a switch-on row from the human", disk.count(event="switch-on", position="root", **{"from": "human"}) == 1)
    verify("… the DOM shows the switch of root on", switch_settled(page, "root", True))
    verify("… and root-1 alive under root",
           appears(page, '[data-position="root"] [data-session="root-1"][data-status="alive"]'))
    verify("… the page reports the decision of the loop",
           "applicata" in (page.locator("#last-action").text_content() or ""), page.locator("#last-action").text_content())
    page.click('[data-switch="root"]')
    verify("click that switches it off → one switch-off row on root",
           wait_until(lambda: disk.count(event="switch-off", position="root") == 1))
    verify("… the DOM shows the switch of root off", switch_settled(page, "root", False))
    time.sleep(3)
    verify("… no new spawn on root in the next 3 s", disk.count(event="spawn", position="root") == 1,
           f"{disk.count(event='spawn', position='root')} spawn rows")
    verify("… root-1 stays alive: a click lets the sessions end their turn",
           disk.sessions()["root-1"]["status"] == "alive")
    same_as_disk(page, disk, "after the two clicks")

    print("the state")
    page.click('[data-switch="lead"]')
    verify("click on the switch of lead → on in the DOM", switch_settled(page, "lead", True))
    for _ in range(2):
        processes.human("spawn", "lead")
    verify("two sessions on lead", wait_until(lambda: disk.count(event="spawn", position="lead") == 2))
    for text in ("primo messaggio per worker", "secondo messaggio per worker"):
        libstate.deposit_message(libstate.Paths(state), "worker", "human", text)
    verify("worker is off: its two messages stay in the mailbox",
           wait_until(lambda: disk.count(event="held", position="worker") == 2))
    processes.human("spawn", "worker")
    verify("a spawn on worker switched off → queued",
           wait_until(lambda: disk.count(event="queued", position="worker", reason="switched-off") == 1))
    snapshot = same_as_disk(page, disk, "sessions, mailbox, queue")
    verify("… two live sessions on lead, one on root", snapshot["alive"]["lead"] == ["lead-1", "lead-2"]
           and snapshot["alive"]["root"] == ["root-1"], f"{snapshot['alive']}")
    verify("… two messages in the mailbox of worker, one pending request",
           len(snapshot["mailbox"]["worker"]) == 2 and len(snapshot["requests"]) == 1, f"{snapshot['mailbox']} {snapshot['requests']}")
    screenshot(page, screenshots, "state")

    print("the interpellation")
    message_for(state, "root", "root-1", "escalate --blocked 'manca una scelta' --alternative 'strada A' "
                                         "--alternative 'strada B' --recommendation 'strada A' --default 'strada A' "
                                         "--deadline domani scegliere fra A e B")
    verify("an escalation from the root with its package → interpellation row",
           wait_until(lambda: disk.count(event="interpellation", position="root") == 1))
    same_as_disk(page, disk, "interpellation")
    [path] = (state / "loop" / "interpellations").glob("[0-9]*.json")
    entry = json.loads(path.read_text(encoding="utf-8"))
    card = page.locator(f'[data-interpellation="{entry["id"]}"]')
    shown = {
        "alternatives": card.locator("[data-alternative]").all_text_contents(),
        "recommendation": card.locator('[data-field="recommendation"]').text_content(),
        "default": card.locator('[data-field="default"]').text_content(),
        "blocked": card.locator('[data-field="blocked"]').text_content(),
    }
    verify("… alternatives, recommendation, default and block equal to the file",
           shown == {field: entry[field] for field in shown}, f"{shown}")
    verify("… the panel is read only: no button, field or form",
           page.locator("#interpellations-panel").locator("button, input, textarea, select, form").count() == 0)

    print("the budget")
    message_for(state, "lead", "lead-1", "spend 600")
    message_for(state, "lead", "lead-2", "spend 600")
    verify("1200 tokens over a budget of 1000 → halt row on lead",
           wait_until(lambda: disk.count(event="halt", position="lead", reason="budget") == 1))
    verify("… the DOM shows the switch of lead off", switch_settled(page, "lead", False))
    verify("… with reason budget",
           page.locator('[data-position="lead"] [data-switch-reason="budget"]').count() == 1)
    same_as_disk(page, disk, "after the budget")
    events = [row["event"] for row in disk.rows()]
    verify("… the log of the DOM is registry/loop.jsonl row by row",
           wait_until(lambda: dom_log_events(page) == [row["event"] for row in disk.rows()]),
           f"DOM {len(dom_log_events(page))} rows · file {len(events)} rows")
    verify("… with the halt row", page.locator('#log [data-log-row][data-event="halt"]').count() == 1)
    screenshot(page, screenshots, "budget")

    print("the loop dies")
    processes.loop.kill()
    processes.loop.wait(timeout=10)
    verify("kill -9 of the loop: the page says down", appears(page, 'body[data-loop="down"]'))
    verify("… the view stays up", processes.view.poll() is None)
    verify("… every switch disabled", page.locator("[data-switch]:not([disabled])").count() == 0 and switch_count(page) == 3)
    verify("… root-1 still listed alive, with its process",
           page.locator('[data-session="root-1"][data-status="alive"][data-process="alive"]').count() == 1)
    screenshot(page, screenshots, "down")


def fake_claude(state):
    """test/fake-claude.py as `claude` in <state>/fake-bin, with LAUNCH_S for a launch; the environment
    of the loop: that directory first on PATH, and a HOME of its own for the transcripts."""
    directory = state / "fake-bin"
    directory.mkdir(parents=True)
    shutil.copy(FAKE_CLAUDE, directory / "claude")
    (directory / "claude").chmod(0o755)
    version = re.search(r'EXPECTED_VARIABLES_CLAUDE_VERSION = "([^"]+)"',
                        (ANTFARM / "bin" / "match.py").read_text(encoding="utf-8")).group(1)
    (directory / "config.json").write_text(json.dumps({
        "launch_s": LAUNCH_S, "resume_s": 1, "stop_s": 1, "agents_s": 0, "tokens": 0, "version": version,
        "request": str(ANTFARM / "bin" / "request")}), encoding="utf-8")
    (state / "home").mkdir()
    return {"PATH": f"{directory}:{os.environ['PATH']}", "HOME": str(state / "home")}


def launching_scenario(page, processes, disk, screenshots):
    page.goto(processes.address)
    page.wait_for_selector('body[data-connection="open"][data-loop="running"]', state="attached", timeout=WAIT_MS)
    print("a slow launch")
    page.click('[data-switch="lead"]')
    verify("click on the switch of lead → on in the DOM", switch_settled(page, "lead", True))
    answer = processes.human("spawn", "lead")
    verify("a spawn on lead → accepted at once, lead-1 launching", "lead-1 in lancio" in answer.stdout, answer.stdout)
    verify("… lead-1 launching in the registry",
           (disk.sessions().get("lead-1") or {}).get("status") == "launching", f"{disk.sessions().get('lead-1')}")
    selector = '[data-position="lead"] [data-session="lead-1"][data-status="launching"][data-operation="launch"]'
    verify("… and in the DOM, with its operation, among the live sessions of lead", appears(page, selector))
    verify("… while the registry still says launching",
           (disk.sessions().get("lead-1") or {}).get("status") == "launching", f"{disk.sessions().get('lead-1')}")
    screenshot(page, screenshots, "launching")
    verify("the launch ends: alive in the registry",
           wait_until(lambda: (disk.sessions().get("lead-1") or {}).get("status") == "alive", LAUNCH_S + WAIT_S))
    verify("… and alive in the DOM, without an operation",
           appears(page, '[data-position="lead"] [data-session="lead-1"][data-status="alive"]:not([data-operation])'))
    same_as_disk(page, disk, "after the launch")


def reporters_scenario(page, processes, disk, screenshots):
    page.goto(processes.address)
    page.wait_for_selector('body[data-connection="open"][data-loop="running"]', state="attached", timeout=WAIT_MS)
    print("the reporters, one per repo")
    page.click('[data-switch="reporter"]')
    verify("click on the switch of reporter → on in the DOM", switch_settled(page, "reporter", True))
    declared = page.evaluate("""() => [...document.querySelectorAll('[data-position="reporter"] [data-declared-parameter="repo"] dd')]
        .map(node => node.textContent)""")
    verify("the card of reporter shows the four values of its parameter", declared == [" · ".join(REPOS)], f"{declared}")
    answers = [processes.human("spawn", "reporter", f"repo={repo}") for repo in REPOS]
    verify("four spawns on reporter, one per repo → accepted",
           all("in lancio" in answer.stdout or "lanciata" in answer.stdout for answer in answers),
           " | ".join(answer.stdout.strip() for answer in answers))

    def alive_in_registry():
        return {entry["parameters"]["repo"]: session_id for session_id, entry in disk.sessions().items()
                if entry["position"] == "reporter" and entry["status"] == "alive"}

    verify("… four reporter sessions alive in the registry", wait_until(lambda: len(alive_in_registry()) == 4, LAUNCH_S + WAIT_S),
           f"{alive_in_registry()}")
    registry = alive_in_registry()
    selector = '[data-position="reporter"] [data-session][data-status="alive"] [data-parameter="repo"]'
    verify("… four alive sessions in the DOM under reporter, each with its repo",
           wait_until(lambda: page.locator(selector).count() == 4), f"{page.locator(selector).count()}")
    shown = page.evaluate("""() => Object.fromEntries([...document.querySelectorAll('[data-position="reporter"] [data-session][data-status="alive"]')]
        .map(item => [item.querySelector('[data-parameter="repo"]')?.dataset.value, item.dataset.session]))""")
    verify("… one per repo, the same session the registry carries for that repo", shown == registry,
           f"DOM {shown} · registry {registry}")
    verify("… and every repo once", sorted(shown) == sorted(REPOS), f"{sorted(shown)}")
    rows = page.evaluate("() => [...document.querySelectorAll('#log [data-event=\"spawn\"]')].map(li => li.textContent)")
    verify("the four spawn rows of the log of the DOM carry their repo",
           all(any(f"repo={repo}" in row for row in rows) for repo in REPOS), f"{rows}")
    screenshot(page, screenshots, "reporters")
    same_as_disk(page, disk, "with the four reporters")


def new_state(folder, grants):
    """A state root under the folder with a copy of the grants."""
    state = folder / "state"
    (state / "grants").mkdir(parents=True)
    for grant in grants.glob("*.json"):
        shutil.copy(grant, state / "grants" / grant.name)
    return state


# <editor-fold desc="In neutral">
def claude_witness(state):
    """A `claude` first on the PATH of the loop: it writes every call in claude-calls.txt, then runs
    the real one. A call that lasts a moment leaves its line too, where a look at /proc can miss it."""
    real = shutil.which("claude") or "false"
    directory = state / "witness-bin"
    directory.mkdir(parents=True)
    calls = state / "claude-calls.txt"
    witness = directory / "claude"
    witness.write_text(f'#!/bin/sh\necho "$$ $*" >> {shlex.quote(str(calls))}\nexec {shlex.quote(real)} "$@"\n',
                       encoding="utf-8")
    witness.chmod(0o755)
    return {"PATH": f"{directory}:{os.environ['PATH']}"}, calls


def claude_processes_under(root):
    """The pids of the `claude` processes whose working directory is the root or lies under it."""
    found = set()
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            command = (entry / "comm").read_text().strip()
            arguments = [Path(argument.decode(errors="replace")).name
                         for argument in (entry / "cmdline").read_bytes().split(b"\0")[:2] if argument]
            directory = Path(os.readlink(entry / "cwd"))
        except OSError:
            continue
        if (command == "claude" or "claude" in arguments) and (directory == root or root in directory.parents):
            found.add(int(entry.name))
    return found


def neutral_scenario(folder, seconds):
    """The loop on the LLM fixture, every switch off, for the given seconds."""
    state = new_state(folder, FIXTURE_GRANTS)
    environment, calls = claude_witness(state)
    processes = Processes(state, folder, environment)
    disk = Disk(state)
    try:
        processes.start_loop()
        print(f"in neutral on the fixture, {seconds} s")
        seen = set()
        limit = time.monotonic() + seconds
        while time.monotonic() < limit:
            seen |= claude_processes_under(state)
            time.sleep(0.25)
        status = disk.read(state / "loop" / "status.json", {})
        switches = disk.read(state / "loop" / "switches.json", {})
        verify("the loop runs on the three positions of the fixture",
               processes.loop.poll() is None and sorted(status.get("positions", {})) == ["lead", "root", "worker"],
               f"{sorted(status.get('positions', {}))}")
        verify("… every switch off", not any((entry or {}).get("on") for entry in switches.values()), f"{switches}")
        verify(f"… 0 spawn rows and 0 launching rows in {seconds} s",
               disk.count(event="spawn") == 0 and disk.count(event="launching") == 0,
               f"{disk.count(event='spawn')} spawn, {disk.count(event='launching')} launching")
        verify("… 0 calls to claude", not calls.exists() or not calls.read_text().strip(),
               calls.read_text() if calls.exists() else "")
        verify("… 0 claude processes under the state root", not seen, f"{sorted(seen)}")
    finally:
        processes.stop()
# </editor-fold>


# <editor-fold desc="A real session">
def session_status(short_id):
    """The status of the session in `claude agents --json`: idle once it has ended its turn."""
    try:
        output = subprocess.run(["claude", "agents", "--json"], capture_output=True, text=True, timeout=60)
        return next((entry.get("status") for entry in json.loads(output.stdout) if entry.get("id") == short_id), None)
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return None


def user_texts(transcript_path):
    """The `user` entries of the transcript that are not a tool_result, in order, as text."""
    texts = []
    for line in transcript_path.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("type") != "user":
            continue
        content = (entry.get("message") or {}).get("content")
        if isinstance(content, list):
            if content and all(isinstance(block, dict) and block.get("type") == "tool_result" for block in content):
                continue
            content = "".join(block.get("text", "") for block in content if isinstance(block, dict))
        texts.append({"text": content, "origin": (entry.get("origin") or {}).get("kind")})
    return texts


def wake_text():
    """WAKE_TEXT of bin/loop.py, with the path of the request command as the loop writes it."""
    text = re.search(r'^WAKE_TEXT = "(.+)"$', LOOP.read_text(encoding="utf-8"), re.MULTILINE).group(1)
    return text.replace("{request}", str(ANTFARM / "bin" / "request"))


def real_session_scenario(folder):
    state = new_state(folder, FIXTURE_GRANTS)
    processes = Processes(state, folder)
    disk = Disk(state)
    entry = {}
    try:
        processes.start_loop()
        print("a real session (haiku)")
        processes.human("switch", "root", "on")
        verify("switch root on → spawn row of root-1",
               wait_until(lambda: disk.count(event="spawn", session="root-1") == 1, REAL_LAUNCH_S))
        entry = disk.sessions().get("root-1") or {}
        verify("… a Claude Code session in the background, with its session id and short id",
               entry.get("type") == "llm" and bool(entry.get("sessionId")) and bool(entry.get("short_id")), f"{entry}")
        if not entry.get("short_id"):
            return
        verify("its first action, the spawn on lead of its prompt, is in the log: queued, lead switched off",
               wait_until(lambda: disk.count(event="queued", position="lead", reason="switched-off", **{"from": "root-1"}) == 1,
                          REAL_TURN_S))
        verify("… it ends its turn: idle in claude agents --json",
               wait_until(lambda: session_status(entry["short_id"]) == "idle", REAL_TURN_S))
        message_for(state, "root", "root-1", "Messaggio del collaudo per root-1.")
        verify("a message for it → the loop wakes it: wake row",
               wait_until(lambda: disk.count(event="wake", session="root-1") == 1, REAL_LAUNCH_S))
        verify("… the same session: same short id in the registry",
               disk.sessions()["root-1"]["short_id"] == entry["short_id"], f"{disk.sessions()['root-1']}")
        verify("its second action, the read of the mailbox, is in the log: received row",
               wait_until(lambda: disk.count(event="received", session="root-1") == 1, REAL_TURN_S))
        verify("… it ends its turn again",
               wait_until(lambda: session_status(entry["short_id"]) == "idle", REAL_TURN_S))
        transcript = next(iter(sorted((Path.home() / ".claude" / "projects").glob(f"*/{entry['sessionId']}.jsonl"))), None)
        verify("its transcript exists", transcript is not None)
        if transcript is None:
            return
        texts = user_texts(transcript)
        after_launch = texts[1:]
        expected = wake_text()
        verify("in the transcript, after the launch, every user entry that is not a tool_result is the wake text",
               bool(after_launch) and all(text["text"] == expected for text in after_launch),
               f"{[text['text'][:80] for text in after_launch]}")
        verify("… one per wake row", len(after_launch) == disk.count(event="wake", session="root-1"),
               f"{len(after_launch)} texts, {disk.count(event='wake', session='root-1')} wake rows")
        print(f"       origin.kind of the wake text: {sorted({text['origin'] for text in after_launch})}; "
              f"tokens of root: {(disk.read(state / 'loop' / 'status.json', {}) or {}).get('positions', {}).get('root', {}).get('tokens')}")
    finally:
        for session in disk.sessions().values():
            if session.get("type") == "llm" and session.get("status") == "alive" and session.get("short_id"):
                subprocess.run(["claude", "stop", session["short_id"]], capture_output=True, timeout=60)
        processes.stop()
# </editor-fold>


def run_scenario(folder, grants, scenario_function, screenshots, page_errors, loop_environment=None):
    """A state root under the folder with a copy of the grants, the loop and the view on it, and the
    scenario driven in a page of its own."""
    state = new_state(folder, grants)
    environment = loop_environment(state) if loop_environment else None
    processes = Processes(state, folder, environment)
    try:
        processes.start_loop()
        processes.start_view()
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page(viewport={"width": 1400, "height": 1000})
            page.on("pageerror", lambda error: page_errors.append(str(error)))
            scenario_function(page, processes, Disk(state), screenshots)
            browser.close()
    finally:
        processes.stop()
        subprocess.run(["pkill", "-9", "-f", str(state / "fake-bin" / "claude")], capture_output=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--real-session", action="store_true", help="also one real session on haiku")
    parser.add_argument("--neutral-s", type=float, default=NEUTRAL_S, help="seconds in neutral on the fixture")
    parser.add_argument("--screenshots", type=Path)
    parser.add_argument("--keep", action="store_true", help="keep the temporary folder")
    args = parser.parse_args()
    if args.screenshots:
        args.screenshots.mkdir(parents=True, exist_ok=True)
    folder = Path(tempfile.mkdtemp(prefix="antfarm-loop-acceptance-"))
    page_errors = []
    try:
        neutral_scenario(folder / "neutral", args.neutral_s)
        run_scenario(folder / "script", GRANTS, scenario, args.screenshots, page_errors)
        run_scenario(folder / "fake", FAKE_GRANTS, launching_scenario, args.screenshots, page_errors, fake_claude)
        run_scenario(folder / "gazette", GAZETTE_GRANTS, reporters_scenario, args.screenshots, page_errors, fake_claude)
        verify("no JavaScript error in the pages", not page_errors, "; ".join(page_errors))
        if args.real_session:
            real_session_scenario(folder / "real")
    finally:
        if args.keep:
            print(f"--keep: the temporary folder stays in {folder}")
        else:
            shutil.rmtree(folder, ignore_errors=True)
    print()
    if failed:
        print(f"LOOP ACCEPTANCE TEST FAILED: {failed} checks")
        return 1
    print("loop acceptance test: all ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
