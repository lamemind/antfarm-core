#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["playwright==1.60.0"]
# ///
"""Acceptance test of the loop view from the DOM, with headless Chromium driven by Playwright. No LLM:
the sessions are script sessions (test/loop-node.py, on the grants of test/grants/script).

    antfarm-core/test/acceptance-loop.py [--screenshots <directory>] [--keep]

It runs on a temporary state root with a copy of the script grants, starts the loop and the view on
it, and drives the page. Every figure it compares with the DOM is read here from the files of the
loop — loop/status.json, switches.json, sessions.json, mailboxes/, requests/, interpellations/,
registry/loop.jsonl — without going through the code of the view:

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

With --screenshots it saves one screenshot per step; with --keep the temporary folder stays, with
the log of the loop and of the view. Exits 1 if a check fails.
"""

import argparse
import json
import os
import re
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
CYCLE = "0.2"
WAIT_S = 15
WAIT_MS = WAIT_S * 1000
FOREIGN_ORIGIN = "http://example.org"

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
    def __init__(self, state, records):
        self.state = state
        self.records = records
        self.environment = {**os.environ, "ANTFARM_STATE_DIR": str(state)}
        self.loop = None
        self.view = None
        self.address = None

    def start_loop(self):
        output = open(self.records / "loop.out", "a", encoding="utf-8")
        self.loop = subprocess.Popen([str(LOOP), "--cycle", CYCLE, "--no-notify"], env=self.environment,
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


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--screenshots", type=Path)
    parser.add_argument("--keep", action="store_true", help="keep the temporary folder")
    args = parser.parse_args()
    if args.screenshots:
        args.screenshots.mkdir(parents=True, exist_ok=True)
    folder = Path(tempfile.mkdtemp(prefix="antfarm-loop-view-"))
    state = folder / "state"
    (state / "grants").mkdir(parents=True)
    for grant in GRANTS.glob("*.json"):
        shutil.copy(grant, state / "grants" / grant.name)
    processes = Processes(state, folder)
    try:
        processes.start_loop()
        processes.start_view()
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page(viewport={"width": 1400, "height": 1000})
            page_errors = []
            page.on("pageerror", lambda error: page_errors.append(str(error)))
            scenario(page, processes, Disk(state), args.screenshots)
            verify("no JavaScript error in the page", not page_errors, "; ".join(page_errors))
            browser.close()
    finally:
        processes.stop()
        if args.keep:
            print(f"--keep: the temporary folder stays in {folder}")
        else:
            shutil.rmtree(folder, ignore_errors=True)
    print()
    if failed:
        print(f"LOOP VIEW ACCEPTANCE TEST FAILED: {failed} checks")
        return 1
    print("loop view acceptance test: all ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
