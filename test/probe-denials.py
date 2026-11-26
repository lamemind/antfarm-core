#!/usr/bin/env python3
"""Probe of the two silent denials of `dontAsk`: how long a Bash command that its permission rule
covers can be, and the `#` at the start of a line of its text. On haiku.

Under `--permission-mode dontAsk` Claude Code denies a Bash command that the rule `Bash(<request> *)`
covers in two cases, with the same text as every other denial, so the session does not learn why:
when the command is too long, and when a line of its quoted text starts with `#`. The probe launches
haiku sessions on the loop, as the loop launches every LLM session — `--safe-mode`, Bash only,
`dontAsk`, the request command only —, one session per step, and reads from the transcript the
command each session ran and its outcome. The steps, in order:

    split   two commands that tell characters from lines: a text of numbers on one line, in double
            quotes as in `bus send`, of about SPLIT_LENGTH characters; and a text of SPLIT_LINES
            numbers, one per line, in single quotes, of a few hundred characters. A first command
            denied and a second that passes say that the cause is the length in characters; a first
            command of at least SPLIT_LEAST characters that passes and a second denied say that it is
            the number of lines, and the search goes on in lines
    search  bisection between the longest command seen passing and the shortest seen denied, in the
            unit of the split: every step one session and one command, halfway, until the two are
            within PRECISION characters (one line, in lines) or after SEARCH_COMMANDS commands
    hash    four short texts on three lines, in single quotes, copied as written: a `#` at the start
            of a line after a new line, which is denied; a `#` at the start of the text and a `#`
            inside a line, which pass; and the same text without `#`, which passes
    cap     a command whose text is SENT_TEXT_CAP characters long, the cap of the prompts
            (bin/match.py), on one line in double quotes: it passes

The long texts are described, not written: the prompt asks for the numbers from 1 to N separated by
one space, or by a new line, and haiku does not copy as written a command of ten thousand characters.
A command is a measure when its text is digits separated by single spaces, or by single new lines,
inside the quotes the step asks — nothing else that could be the cause of a denial — and its length
is the one of the command as the session ran it, not the one asked. A command passes when the
outcome is not the denial and its text reaches the mailbox of the parent, `desk`, a script session
that only waits. The numbers are ASCII, so a character of a long text is a byte.

The cap the prompts would carry from this measure is printed next to the constant: the longest
command seen passing, rounded down to the thousand, minus 1,000.

It runs on a temporary state root, stops every session it launched and the loop before exiting, and
spends at most 19 haiku commands, one session per step.

Usage:  antfarm-core/test/probe-denials.py [--json] [--keep]
Exits 0 when the search ends within PRECISION characters, the four `#` texts give the outcome above
and the command of the cap passes; 1 otherwise.
"""

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
from pathlib import Path

ENGINE = Path(__file__).resolve().parent.parent
FOLDER = Path(tempfile.mkdtemp(prefix="antfarm-probe-denials-"))
STATE = FOLDER / "state"
STATE.mkdir()
os.environ["ANTFARM_STATE_DIR"] = str(STATE)
sys.path.insert(0, str(ENGINE / "bin"))
import loopstate as libstate  # noqa: E402
import match as libmatch  # noqa: E402

LOOP = ENGINE / "bin" / "loop.py"
REQUEST = ENGINE / "bin" / "request"
PATHS = libstate.Paths(STATE)
MODEL = "haiku"
CYCLE = "0.2"
LAUNCH_WAIT_S = 120
TURN_WAIT_S = 600
RELOAD_WAIT_S = 15
CHARACTERS, LINES = "characters", "lines"
SPLIT_LENGTH = 10500
SPLIT_LEAST = 10010
SPLIT_LINES = 120
PRECISION = {CHARACTERS: 100, LINES: 1}
SEARCH_COMMANDS = 12
PASSED, DENIED, FAILED = "passed", "denied", "failed"
HASH_TEXTS = {
    "1_without": ("- alfa 1fd8365 — riga uno\ntitolo beta\n- beta 6d623f4 — riga due", PASSED),
    "2_after_new_line": ("- alfa 1fd8365 — riga uno\n## beta\n- beta 6d623f4 — riga due", DENIED),
    "3_at_the_start": ("## alfa\n- alfa 1fd8365 — riga uno\n- beta 6d623f4 — riga due", PASSED),
    "4_inside_a_line": ("- alfa 1fd8365 — riga uno, issue #3\n- beta 6d623f4 — riga due", PASSED),
}
PROMPT = """Sei una sonda di antfarm: misuri quali comandi ti sono permessi. Il tuo solo strumento è Bash.
Esegui con Bash {what}, ognuno in una chiamata Bash separata. Se un comando viene negato o fallisce, \
passa al successivo: non riprovarlo, non cambiarlo, non cercare un'altra strada.

{blocks}

Dopo {last} finisci il turno senza fare altro."""
QUOTE = {" ": '"', "\n": "'"}
SEPARATOR_WORDS = {" ": "separati da un solo spazio, su una riga sola, fra virgolette doppie",
                   "\n": "uno per riga, separati da un solo a capo, fra apici singoli"}


def log(text):
    print(text, file=sys.stderr, flush=True)


def numbers(count, separator):
    return separator.join(str(number) for number in range(1, count + 1))


def escalate(text, quote):
    return f"{REQUEST} escalate {quote}{text}{quote}"


def count_for_length(length):
    """The most numbers whose escalate command, on one line in double quotes, is at most `length`
    characters long."""
    count = 1
    while len(escalate(numbers(count + 1, " "), '"')) <= length:
        count += 1
    return count


def count_for_text(length):
    """The fewest numbers whose text, on one line, is at least `length` characters long."""
    count = 1
    while len(numbers(count, " ")) < length:
        count += 1
    return count


class Described:
    """A command the prompt describes: the numbers from 1 to `count`, with the separator. It matches
    a Bash call whose command is escalate with digits and the separator only, inside the quotes."""

    def __init__(self, key, count, separator):
        self.key, self.count, self.separator = key, count, separator
        quote = QUOTE[separator]
        digits = r"[0-9]+(?:" + re.escape(separator) + r"[0-9]+)*"
        self.pattern = re.compile(re.escape(f"{REQUEST} escalate {quote}") + f"({digits})" + re.escape(quote))

    def block(self, index):
        quote = QUOTE[self.separator]
        example = escalate(numbers(3, self.separator), quote)
        return (f"{index}. il comando `{REQUEST} escalate {quote}<testo>{quote}`, dove <testo> sono i numeri interi "
                f"da 1 a {self.count} in ordine, {SEPARATOR_WORDS[self.separator]}. Scrivi nel comando tutti i numeri, "
                "uno per uno, senza abbreviare e senza puntini: niente `$(…)`, `seq`, `printf`, cicli, variabili o "
                f"altri comandi. Con i numeri da 1 a 3 il comando sarebbe:\n```\n{example}\n```")

    def text_of(self, command):
        hit = self.pattern.fullmatch(command.strip())
        return hit.group(1) if hit else None


class Literal:
    """A command the prompt writes as it is: it matches the Bash call with the same command."""

    def __init__(self, key, text):
        self.key, self.command, self.text = key, escalate_quoted(text), text

    def block(self, index):
        return f"{index}. comando, alla lettera, a capo compresi:\n```\n{self.command}\n```"

    def text_of(self, command):
        return self.text if command.strip() == self.command else None


def escalate_quoted(text):
    return f"{REQUEST} escalate {shlex.quote(text)}"


def prompt(steps):
    count = len(steps)
    what = "questo comando" if count == 1 else f"questi {count} comandi, nell'ordine"
    last = "il comando" if count == 1 else "l'ultimo comando"
    blocks = "\n\n".join(step.block(index) for index, step in enumerate(steps, start=1))
    return PROMPT.format(what=what, blocks=blocks, last=last)


def grants(probe_prompt):
    """`desk`, a script root that only waits and keeps the escalations, and `probe`, the haiku
    session of the step. The prompt of `probe` changes at every step."""
    desk = {"position": "desk", "description": "Radice della sonda: aspetta, e tiene nella casella le escalation della sonda.",
            "parent": "human", "max_sessions": 1, "budget_tokens": 0,
            "launch": {"type": "script", "command": ["sleep", "7200"]}}
    probe = {"position": "probe", "description": "La sonda dei rifiuti di dontAsk: comandi request escalate.",
             "parent": "desk", "max_sessions": 40, "budget_tokens": 5000000,
             "launch": {"type": "llm", "model": MODEL, "prompt": probe_prompt}}
    for grant in (desk, probe):
        libstate.write_json(PATHS.grants / f"{grant['position']}.json", grant)


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def sessions():
    return (read_json(PATHS.sessions, {}) or {}).get("sessions", {})


def grants_loaded():
    try:
        rows = PATHS.log.read_text(encoding="utf-8").splitlines()
    except OSError:
        return 0
    return sum(1 for row in rows if '"event": "grants-loaded"' in row)


def wait_until(predicate, seconds, pause=1):
    limit = time.monotonic() + seconds
    while time.monotonic() < limit:
        value = predicate()
        if value:
            return value
        time.sleep(pause)
    return None


def escalations():
    """The texts of the escalations in the mailbox of desk, read or not."""
    mailbox = PATHS.mailbox("desk")
    found = []
    for path in sorted([*mailbox.glob("[0-9]*.json"), *mailbox.glob("received/[0-9]*.json")]):
        message = read_json(path) or {}
        if message.get("kind") == "escalation":
            found.append(message.get("text"))
    return found


def status_of(short_id):
    return libmatch.session_states().get(short_id, {}).get("status")


def last_turn_record(transcript):
    """The last assistant or user record of the transcript, None if there is none yet. A line
    being written is skipped."""
    last = None
    for line in transcript.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if record.get("type") in ("assistant", "user"):
            last = record
    return last


def turn_over(entry):
    """The session is idle and its last record is an answer without a tool call."""
    if status_of(entry["short_id"]) != "idle":
        return False
    transcript = libmatch.transcript(entry["sessionId"])
    if transcript is None:
        return False
    last = last_turn_record(transcript)
    if last is None or last["type"] != "assistant":
        return False
    content = (last.get("message") or {}).get("content")
    return isinstance(content, list) and not any(block.get("type") == "tool_use" for block in content)


def run_step(steps, label):
    """One session on `probe` with the prompt of the steps: the grant rewritten, the reload of the
    loop awaited, the spawn asked as the human. Returns one result per step, and the other calls."""
    loaded = grants_loaded()
    grants(prompt(steps))
    if not wait_until(lambda: grants_loaded() > loaded, RELOAD_WAIT_S, pause=0.2):
        raise RuntimeError(f"{label}: the loop does not reload the grants")
    before = set(sessions())
    outcome = subprocess.run([str(LOOP), "spawn", "probe"], capture_output=True, text=True, timeout=60)
    if outcome.returncode != 0:
        raise RuntimeError(f"{label}: spawn rejected: {outcome.stdout.strip()} {outcome.stderr.strip()}")
    session_id = wait_until(lambda: next(iter(set(sessions()) - before), None), 30, pause=0.2)
    if not session_id:
        raise RuntimeError(f"{label}: no session in the registry after the spawn")

    def launched():
        entry = sessions().get(session_id) or {}
        if entry.get("status") == "closed":
            raise RuntimeError(f"{label}: {session_id} closed before its turn: {entry.get('close_reason')}")
        return entry if entry.get("status") == "alive" and entry.get("short_id") else None

    entry = wait_until(launched, LAUNCH_WAIT_S)
    if not entry:
        raise RuntimeError(f"{label}: {session_id} is not launched within {LAUNCH_WAIT_S} s")
    log(f"{label}: {session_id} launched, {len(steps)} command(s)")
    try:
        if not wait_until(lambda: turn_over(entry), TURN_WAIT_S, pause=3):
            raise RuntimeError(f"{label}: {session_id} does not end its turn within {TURN_WAIT_S} s")
        transcript = libmatch.transcript(entry["sessionId"])
        calls = libmatch.bash_calls(transcript)
    finally:
        subprocess.run(["claude", "stop", entry["short_id"]], capture_output=True, text=True, timeout=60)
    return measure(steps, calls, session_id)


def measure(steps, calls, session_id):
    """For every step the first call that matches it: its text, the length of the command, the
    lines of the text, the outcome — passed (not denied, and the text in the mailbox of desk),
    denied, failed — and the start of the outcome. A step without a call is not a measure."""
    delivered = escalations()
    used, results = set(), {}
    for step in steps:
        results[step.key] = {"session": session_id, "asked": getattr(step, "count", None), "measured": False}
        for index, (name, command, outcome, is_error) in enumerate(calls):
            text = step.text_of(command) if name == "Bash" and index not in used else None
            if text is None:
                continue
            used.add(index)
            if outcome.startswith(libmatch.DENIAL_PREFIX):
                verdict = DENIED
            elif not is_error and text in delivered:
                verdict = PASSED
            else:
                verdict = FAILED
            results[step.key] = {**results[step.key], "measured": verdict != FAILED, "outcome": verdict,
                                 "characters": len(command.strip()), "text_characters": len(text),
                                 "lines": text.count("\n") + 1, "answer": outcome[:200]}
            break
    others = [command[:200] for index, (name, command, _, _) in enumerate(calls) if index not in used]
    return results, others


def split():
    """The two commands that tell characters from lines; returns (unit or None, results, others)."""
    one_line = Described("1_one_line", count_for_length(SPLIT_LENGTH) + 1, " ")
    many_lines = Described("2_many_lines", SPLIT_LINES, "\n")
    results, others = run_step([one_line, many_lines], "split")
    first, second = results[one_line.key], results[many_lines.key]
    unit = None
    if first["measured"] and second["measured"]:
        if first["outcome"] == DENIED and second["outcome"] == PASSED:
            unit = CHARACTERS
        elif first["outcome"] == PASSED and first["characters"] >= SPLIT_LEAST and second["outcome"] == DENIED:
            unit = LINES
    return unit, results, others


def search(unit, measures):
    """Bisection in the unit between the longest command seen passing and the shortest seen denied,
    starting from the measures of the split. Returns the bounds, the steps, and whether it is
    consistent: a command that passes beyond a denied one, or is denied below a passing one, means
    that the threshold is not one length, and the search stops."""
    lo = max((value[unit] for value in measures if value["measured"] and value["outcome"] == PASSED), default=0)
    hi = min((value[unit] for value in measures if value["measured"] and value["outcome"] == DENIED), default=None)
    steps, others, consistent = [], [], hi is not None
    while consistent and hi - lo > PRECISION[unit] and len(steps) < SEARCH_COMMANDS:
        target = (lo + hi) // 2
        if unit == CHARACTERS:
            step = Described(f"search_{len(steps) + 1}", count_for_length(target), " ")
        else:
            step = Described(f"search_{len(steps) + 1}", target, "\n")
        results, extra = run_step([step], f"search {len(steps) + 1}, {target} {unit}")
        result = {**results[step.key], "target": target}
        steps.append(result)
        others += extra
        if not result["measured"]:
            continue
        value = result[unit]
        if result["outcome"] == PASSED:
            consistent = value < hi
            lo = max(lo, value)
        else:
            consistent = value > lo
            hi = min(hi, value)
    return {"unit": unit, "longest_passed": lo, "shortest_denied": hi, "commands": len(steps),
            "within_precision": consistent and hi is not None and hi - lo <= PRECISION[unit],
            "consistent": consistent, "steps": steps, "other_calls": others}


def hash_cases():
    steps = [Literal(key, text) for key, (text, _) in HASH_TEXTS.items()]
    results, others = run_step(steps, "hash")
    for key, (_, expected) in HASH_TEXTS.items():
        results[key]["expected"] = expected
        results[key]["pass"] = results[key]["measured"] and results[key]["outcome"] == expected
    return {"cases": results, "other_calls": others, "pass": all(value["pass"] for value in results.values())}


def cap():
    step = Described("cap", count_for_text(libmatch.SENT_TEXT_CAP), " ")
    results, others = run_step([step], f"cap, a text of {libmatch.SENT_TEXT_CAP} characters")
    result = results[step.key]
    result["pass"] = (result["measured"] and result["outcome"] == PASSED
                      and result["text_characters"] >= libmatch.SENT_TEXT_CAP)
    return {"SENT_TEXT_CAP": libmatch.SENT_TEXT_CAP, "measured_on": libmatch.SENT_TEXT_CAP_CLAUDE_VERSION,
            **result, "other_calls": others}


def stop_everything(loop):
    loop.send_signal(signal.SIGTERM)
    try:
        loop.wait(timeout=180)
    except subprocess.TimeoutExpired:
        loop.kill()
    for entry in sessions().values():
        if entry.get("type") == "llm" and entry.get("short_id"):
            subprocess.run(["claude", "stop", entry["short_id"]], capture_output=True, text=True, timeout=60)
        elif entry.get("type") == "script" and entry.get("pid"):
            try:
                os.killpg(entry["pid"], signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass


def main():
    as_json = "--json" in sys.argv[1:]
    keep = "--keep" in sys.argv[1:]
    result = {"state": str(STATE), "claude_version": libmatch.claude_version(), "model": MODEL}
    PATHS.grants.mkdir(parents=True)
    grants(prompt([Literal("start", "inizio")]))
    output = open(FOLDER / "loop.out", "w", encoding="utf-8")
    loop = subprocess.Popen([str(LOOP), "--cycle", CYCLE, "--no-notify"], env=os.environ.copy(),
                            stdout=output, stderr=subprocess.STDOUT)
    try:
        if not wait_until(lambda: PATHS.status.exists(), 15):
            raise SystemExit(f"the loop does not start: {(FOLDER / 'loop.out').read_text()}")
        for arguments in (("switch", "probe", "on"), ("switch", "desk", "on")):
            subprocess.run([str(LOOP), *arguments], capture_output=True, text=True, timeout=30)
        if not wait_until(lambda: any(entry["position"] == "desk" and entry["status"] == "alive"
                                      for entry in sessions().values()), 30):
            raise SystemExit("desk does not start")
        unit, measures, others = split()
        result["split"] = {"cause": unit, **measures, "other_calls": others}
        if unit is None:
            result["error"] = "the split tells neither characters nor lines"
        else:
            result["search"] = search(unit, list(measures.values()))
            if unit == CHARACTERS and result["search"]["consistent"]:
                result["cap_from_this_measure"] = (result["search"]["longest_passed"] // 1000) * 1000 - 1000
        result["hash"] = hash_cases()
        result["cap"] = cap()
    except RuntimeError as error:
        result["error"] = str(error)
    finally:
        stop_everything(loop)
        output.close()
        if keep:
            log(f"--keep: the folder stays in {FOLDER}")
        else:
            shutil.rmtree(FOLDER, ignore_errors=True)
    passed = ("error" not in result and result["split"]["cause"] == CHARACTERS
              and result["search"]["within_precision"] and result["hash"]["pass"] and result["cap"]["pass"])
    result["pass"] = passed
    if as_json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        for key, value in result.items():
            print(f"{key}: {json.dumps(value, ensure_ascii=False)}")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
