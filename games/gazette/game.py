#!/usr/bin/env python3
"""The gazette: an editor that cannot read the repos writes their news, by launching one reporter per
repo, all together, on the loop.

    ANTFARM_STATE_DIR=<project> antfarm-core/games/gazette/game.py --repo <path> [--repo <path>...]
        [--timeout <s>] [--view-port <n>] [--negative-test]
    ANTFARM_STATE_DIR=<project> antfarm-core/games/gazette/game.py report <match>

The game runs on the loop, not on the director: it has no graph and no bus. Three positions
(grants/): `desk`, a script at the root (desk.py), asks the spawn of `editor`; the editor, an LLM
session with the request command only, asks N spawns on `reporter` in one Bash command, one per
repo, with the parameter `repo`; every reporter reads `git -C <state>/repos/<repo> log -20`, the
only git command its grant allows, and escalates its news to the editor; the editor composes the
gazette and escalates it to the desk, which writes gazette.md. The editor ends its turn after the
spawns, and the loop wakes it at every piece.

A match is a state root of its own, `<project>/matches/<id>/`, with its own loop: the budget never
resets and the session ids grow, so two matches on one root would mix. The game creates it with
`repos/<name>`, a symbolic link to every repo given — the values of the parameter are the names, so
registry and report carry names and the grants no path of the machine —, generates `grants/` from
the templates, validates them with bin/validate-grants.py, and starts there the loop of this working
tree with --concurrency N and a cycle of CYCLE seconds, so that the N `request spawn` of the one
command of the editor reach the loop in consecutive cycles and their launches run together. It
starts the view of the loop on the match and prints its address. It switches reporter, editor and
desk on, the root last, and waits for gazette.md, up to --timeout. Then it copies the registry of
the sessions and every message of the mailboxes into the match, because loop/ stays out of git,
stops the loop, stops the sessions, and writes report.md (report.py). Only a match whose gazette
arrived gets its row in registry/matches.tsv of the project: without a gazette there is no outcome to
register, and the report says why.

The editor composes at most 5 news per repo, each text at most 100 characters, without titles: under
`dontAsk` Claude Code denies a Bash command that the permission rule of the request command covers
when it is too long, or when a line of its quoted text starts with `#` — `#` at the start of the text
or inside a line passes (test/probe-denials.py). A gazette of every news of 4 repos is longer than
the threshold, and `## <repo>` titles start lines with `#`. Both prompts carry the cap of a sent
text, match.SENT_TEXT_CAP characters, in place of `[[cap]]`, and tell editor and reporters to
shorten, drop the `#` and send again when the command is denied.

SIGTERM and SIGINT end the wait as a timeout does: the game still copies the state, stops loop and
sessions, and writes the report, with close reason `interrupted`.

`--negative-test` plays a `*-negative-test-*` match: one reporter, on the first repo, told to try
`cat` on a file of its repo and `git log` on the second repo before its news. An acceptance test of
the engine, not a game: no row in the match registry.

Standard library only, as the loop. Exits 0 when the gazette arrives, 1 when it does not, 2 on wrong
usage or with grants that do not validate.
"""

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ENGINE))
sys.path.insert(0, str(ENGINE / "bin"))
import loopstate as libstate  # noqa: E402
import match as libmatch  # noqa: E402
import observe as libobserve  # noqa: E402
from games.gazette import report as libreport  # noqa: E402

GAME = "gazette"
DIRECTORY = Path(__file__).resolve().parent
TEMPLATES = DIRECTORY / "grants"
LOOP = ENGINE / "bin" / "loop.py"
VIEW = ENGINE / "bin" / "loop-view.py"
VALIDATOR = ENGINE / "bin" / "validate-grants.py"
REQUEST = ENGINE / "bin" / "request"
CYCLE = "0.2"
TOKENS_PER_SESSION = 300000
TIMEOUT_S = 900
LOOP_START_S = 15
STOP_WAIT_S = 180
REPO_NAME = re.compile(r"^[A-Za-z0-9._-]+$")
NEGATIVE_BLOCK = ("Prima delle notizie prova questi due comandi, uno per chiamata Bash, alla lettera; se vengono negati "
                  "vai avanti senza riprovarli e senza cercare un'altra strada:\n```\ncat {state}/repos/{repo}/[[file]]\n```\n"
                  "```\ngit -C {state}/repos/[[other]] log -20\n```\n\n")


def log(text):
    print(f"{datetime.now().strftime('%H:%M:%S')} {text}", flush=True)


def git(repository, *arguments):
    return subprocess.run(["git", "-C", str(repository), *arguments], capture_output=True, text=True)


def repositories(paths):
    """name → resolved path; exits 2 on a repo that is not a git working tree, a name that cannot be a
    value of the parameter, or two repos with the same name."""
    found = {}
    for given in paths:
        path = Path(given).resolve()
        name = path.name
        if git(path, "rev-parse", "--git-dir").returncode != 0:
            raise SystemExit(f"{given}: not a git working tree")
        if not REPO_NAME.match(name):
            raise SystemExit(f"{given}: the name {name!r} is not a value a parameter takes ([A-Za-z0-9._-])")
        if name in found:
            raise SystemExit(f"{given}: two repos named {name}")
        found[name] = path
    return found


def status_of(repos):
    return {name: git(path, "status", "--porcelain").stdout for name, path in repos.items()}


def negative_file(repository):
    """A file of the repo for the cat of the negative test: README.md if tracked, else the first tracked file."""
    files = git(repository, "ls-files").stdout.splitlines()
    return "README.md" if "README.md" in files else files[0]


def spawn_command(reporters):
    return " && ".join(f"{{request}} spawn reporter repo={name}" for name in reporters)


def grants(reporters, negative):
    """The grants of the match from the templates: the values of `repo`, the cap and the budget of
    reporter, the list of the repos and the spawn command in the prompt of the editor, the block
    of the negative test in the prompt of the reporter, and in both prompts the characters of a sent
    text, match.SENT_TEXT_CAP."""
    loaded = {path.stem: json.loads(path.read_text(encoding="utf-8")) for path in sorted(TEMPLATES.glob("*.json"))}
    editor, reporter = loaded["editor"], loaded["reporter"]
    cap = str(libmatch.SENT_TEXT_CAP)
    editor["launch"]["prompt"] = (editor["launch"]["prompt"].replace("[[count]]", str(len(reporters)))
                                  .replace("[[repos]]", ", ".join(reporters)).replace("[[spawn]]", spawn_command(reporters))
                                  .replace("[[cap]]", cap))
    reporter["parameters"]["repo"]["values"] = list(reporters)
    reporter["max_sessions"] = len(reporters)
    reporter["budget_tokens"] = len(reporters) * TOKENS_PER_SESSION
    block = ""
    if negative:
        block = NEGATIVE_BLOCK.replace("[[file]]", negative["file"]).replace("[[other]]", negative["other"])
    reporter["launch"]["prompt"] = reporter["launch"]["prompt"].replace("[[negative]]", block).replace("[[cap]]", cap)
    return loaded


def write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def prepare(args):
    """The directory of the match: repos/, grants/ validated, meta.json. Returns (directory, meta)."""
    repos = repositories(args.repo)
    names = list(repos)
    negative = None
    if args.negative_test:
        if len(names) < 2:
            raise SystemExit("--negative-test needs two repos: the reporter reads the first and tries the second")
        negative = {"file": negative_file(repos[names[0]]), "other": names[1]}
        reporters = names[:1]
    else:
        reporters = names
    stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    match_id = f"{stamp}-{GAME}" + ("-negative-test" if negative else "")
    directory = libmatch.MATCHES / match_id
    (directory / "repos").mkdir(parents=True)
    for name, path in repos.items():
        (directory / "repos" / name).symlink_to(path)
    (directory / "grants").mkdir()
    for position, grant in grants(reporters, negative).items():
        write_json(directory / "grants" / f"{position}.json", grant)
    validation = subprocess.run([str(VALIDATOR), str(directory / "grants")], capture_output=True, text=True)
    if validation.returncode != 0:
        print(validation.stderr, file=sys.stderr)
        raise SystemExit(2)
    meta = {"match": match_id, "game": GAME, "start": libmatch.now(), "state": str(directory),
            "request": str(REQUEST), "code": str(ENGINE), "repos": {name: str(path) for name, path in repos.items()},
            "reporters": reporters, "negative_test": bool(negative), "negative": negative,
            "model": json.loads((TEMPLATES / "reporter.json").read_text(encoding="utf-8"))["launch"].get("model"),
            "concurrency": len(reporters), "cycle": float(CYCLE), "timeout_s": args.timeout,
            "heads": {name: git(path, "rev-parse", "HEAD").stdout.strip() for name, path in repos.items()},
            "status_before": status_of(repos), "validate_grants": validation.stdout.strip()}
    write_json(directory / "meta.json", meta)
    return directory, meta


def start(directory, meta, view_port):
    """The loop and its view on the match; returns (loop, view, address)."""
    environment = {**os.environ, "ANTFARM_STATE_DIR": str(directory)}
    loop = subprocess.Popen([str(LOOP), "--cycle", CYCLE, "--concurrency", str(meta["concurrency"]), "--no-notify"],
                            env=environment, stdout=open(directory / "loop.out", "w", encoding="utf-8"),
                            stderr=subprocess.STDOUT)
    limit = time.monotonic() + LOOP_START_S
    while not (directory / "loop" / "status.json").exists():
        if loop.poll() is not None or time.monotonic() > limit:
            raise SystemExit(f"the loop does not start: {(directory / 'loop.out').read_text(encoding='utf-8')}")
        time.sleep(0.2)
    view = subprocess.Popen([str(VIEW), "--port", str(view_port)], env=environment, stdout=subprocess.PIPE,
                            stderr=open(directory / "view.err", "w", encoding="utf-8"), text=True)
    first_line = view.stdout.readline()
    hit = re.search(r"http://127\.0\.0\.1:\d+/", first_line)
    address = hit.group(0) if hit else None
    log(f"view of the loop: {address or 'not started — see view.err'}")
    for position in ("reporter", "editor", "desk"):
        subprocess.run([str(LOOP), "switch", position, "on"], env=environment, capture_output=True, text=True, timeout=30)
    return loop, view, address


def wait_for_gazette(directory, loop, timeout_s):
    """'gazette' when gazette.md arrives, 'timeout' at the end of the wait, 'loop-died' if the loop
    exits; it prints the rows of the log of the loop as they come."""
    limit = time.monotonic() + timeout_s
    seen = 0
    while True:
        rows = libreport.log_rows(directory)
        for row in rows[seen:]:
            words = [row["event"], row.get("position") or "", row.get("session") or "",
                     " ".join(f"{name}={value}" for name, value in (row.get("parameters") or {}).items()),
                     row.get("reason") or "", (row.get("detail") or "")[:90]]
            log(" ".join(word for word in words if word))
        seen = len(rows)
        if (directory / libreport.GAZETTE).is_file():
            return "gazette"
        if loop.poll() is not None:
            return "loop-died"
        if time.monotonic() > limit:
            return "timeout"
        time.sleep(1)


def keep_state(directory):
    """The registry of the sessions and every message of the mailboxes, copied out of loop/, which
    git ignores: the report reads them from here."""
    paths = libstate.Paths(directory)
    shutil.copyfile(paths.sessions, directory / "sessions.json")
    messages = []
    for path in sorted(paths.mailboxes.glob("*/[0-9]*.json")) + sorted(paths.mailboxes.glob("*/received/[0-9]*.json")):
        message = libstate.read_json(path)
        if message:
            messages.append(message)
    messages.sort(key=lambda message: message.get("ts") or "")
    (directory / "mailboxes.jsonl").write_text("".join(json.dumps(message, ensure_ascii=False) + "\n" for message in messages),
                                               encoding="utf-8")


def stop(directory, loop, view):
    """The loop first, so that it starts nothing more; then every session it launched, then the view."""
    loop.send_signal(signal.SIGTERM)
    try:
        loop.wait(timeout=STOP_WAIT_S)
    except subprocess.TimeoutExpired:
        loop.kill()
    for entry in libstate.registry(libstate.Paths(directory))["sessions"].values():
        if entry.get("status") != "alive":
            continue
        if entry.get("type") == "llm" and entry.get("short_id"):
            subprocess.run(["claude", "stop", entry["short_id"]], capture_output=True, text=True, timeout=60)
        elif entry.get("type") == "script" and entry.get("pid"):
            try:
                os.killpg(entry["pid"], signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
    view.terminate()
    try:
        view.wait(timeout=10)
    except subprocess.TimeoutExpired:
        view.kill()


def interrupted(signal_number, _frame):
    raise KeyboardInterrupt(signal.Signals(signal_number).name)


def play(args):
    signal.signal(signal.SIGTERM, interrupted)
    directory, meta = prepare(args)
    log(f"match {meta['match']}: reporters {', '.join(meta['reporters'])}")
    loop, view, address = start(directory, meta, args.view_port)
    reason = "error"
    try:
        try:
            reason = wait_for_gazette(directory, loop, args.timeout)
            time.sleep(2)
        except KeyboardInterrupt:
            reason = "interrupted"
        log(f"close: {reason}")
        keep_state(directory)
    finally:
        stop(directory, loop, view)
    meta = libreport.read_json(directory / "meta.json")
    meta.update(closed=libmatch.now(), close_reason=reason, view=address,
                status_after=status_of({name: Path(path) for name, path in meta["repos"].items()}))
    write_json(directory / "meta.json", meta)
    result, observed = libreport.compose(directory)
    if not meta["negative_test"] and reason == "gazette":
        libobserve.register_match({"match": meta["match"], "game": GAME, "outcome": result["outcome"],
                                   "human_turns": observed["human_turns"], "token": observed["token"], "cwd": directory})
    log(f"outcome {result['outcome']} · human turns {observed['human_turns']} · tokens {observed['token']} · "
        f"report {directory / 'report.md'}")
    return 0 if reason == "gazette" else 1


def main():
    arguments = sys.argv[1:]
    if arguments[:1] == ["report"]:
        if len(arguments) != 2:
            print(__doc__.strip(), file=sys.stderr)
            return 2
        directory = libmatch.MATCHES / arguments[1]
        result, observed = libreport.compose(directory)
        print(f"outcome {result['outcome']} · human turns {observed['human_turns']} · tokens {observed['token']}")
        return 0
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--repo", action="append", required=True, help="a repo the reporters read; repeatable")
    parser.add_argument("--timeout", type=float, default=TIMEOUT_S, help=f"seconds to wait for the gazette (default {TIMEOUT_S})")
    parser.add_argument("--view-port", type=int, default=0, help="port of the view of the loop (default: one the system chooses)")
    parser.add_argument("--negative-test", action="store_true", help="one reporter that tries cat and another repo")
    return play(parser.parse_args(arguments))


if __name__ == "__main__":
    sys.exit(main())
