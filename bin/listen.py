#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["websockets==17.3"]
# ///
"""Test client of the harness channel: receives the events, writes them, counts them, checks their order, measures the delay.

    antfarm-core/bin/listen.py [--port N] [--events <file.jsonl>] [--until-close] [--timeout S]
                          [--delay] [--origin URL]

Connects to ws://127.0.0.1:N/events and receives until the harness closes the channel — the
replay closes it on its own —, or, with --until-close, until the first match-closed event, or
until the timeout (default 120 s). With --events it writes every event as it arrived, one per line.

Prints a JSON summary on stdout: events per class/type, matches, message indices in order of
arrival, whether the arrival of the messages follows the order of their ts, nodes launched and
closed, how the listening ended. With --delay it adds, for the messages, the delay between the ts
of the log row and the arrival at the client: n, median, max, in milliseconds. --origin sends the
Origin header, to test the rejection of pages from another origin.

Exits 0 if the listening ends as expected — channel closed by the harness, or match-closed with
--until-close —, 1 on timeout or if the connection is rejected, 2 on wrong usage.
"""

import argparse
import asyncio
import json
import statistics
import sys
import time
from datetime import datetime

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus


def instant(ts):
    return datetime.fromisoformat(ts).timestamp()


async def receive(args, output):
    arrivals, end, closure = [], "timeout", None
    uri = f"ws://127.0.0.1:{args.port}/events"
    try:
        async with connect(uri, origin=args.origin, max_size=None) as connection:
            limit = time.monotonic() + args.timeout
            try:
                while True:
                    remaining = limit - time.monotonic()
                    if remaining <= 0:
                        break
                    try:
                        text = await asyncio.wait_for(connection.recv(), timeout=remaining)
                    except TimeoutError:
                        break
                    arrivals.append((time.time(), text))
                    if output is not None:
                        output.write(text + "\n")
                        output.flush()
                    if args.until_close and json.loads(text).get("type") == "match-closed":
                        end = "match-closed"
                        break
            except ConnectionClosed as closed:
                end = "channel-closed"
                closure = {"code": closed.rcvd.code if closed.rcvd else None,
                            "reason": closed.rcvd.reason if closed.rcvd else None}
    except InvalidStatus as rejection:
        end, closure = "rejected", {"code": rejection.response.status_code, "reason": rejection.response.body.decode(errors="replace").strip()}
    except OSError as error:
        end, closure = "unreachable", {"code": None, "reason": str(error)}
    return arrivals, end, closure


def summary(arrivals, end, closure, with_delay):
    events = [(arrival, json.loads(text)) for arrival, text in arrivals]
    per_type, matches = {}, []
    for _, event in events:
        key = f"{event['class']}/{event['type']}"
        per_type[key] = per_type.get(key, 0) + 1
        if event["match"] not in matches:
            matches.append(event["match"])
    messages = [(arrival, event) for arrival, event in events if event["class"] == "message"]
    instants = [instant(event["ts"]) for _, event in messages]
    data = {
        "end": end,
        "closure": closure,
        "events": len(events),
        "per_type": dict(sorted(per_type.items())),
        "matches": matches,
        "messages": len(messages),
        "message_indices": [[event["match"], event["index"]] for _, event in messages],
        "messages_in_ts_order": all(first <= second for first, second in zip(instants, instants[1:])),
        "nodes_launched": [[event["match"], event["node"]] for _, event in events if event["type"] == "node-launched"],
        "nodes_closed": [[event["match"], event["node"]] for _, event in events if event["type"] == "node-closed"],
    }
    if with_delay:
        delays = [(arrival - instant(event["ts"])) * 1000 for arrival, event in messages]
        data["delay_ms"] = {
            "n": len(delays),
            "median": round(statistics.median(delays), 1) if delays else None,
            "max": round(max(delays), 1) if delays else None,
        }
    return data


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8740)
    parser.add_argument("--events")
    parser.add_argument("--until-close", action="store_true")
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--delay", action="store_true")
    parser.add_argument("--origin")
    args = parser.parse_args()
    output = open(args.events, "w", encoding="utf-8") if args.events else None
    try:
        arrivals, end, closure = asyncio.run(receive(args, output))
    finally:
        if output is not None:
            output.close()
    print(json.dumps(summary(arrivals, end, closure, args.delay), ensure_ascii=False, indent=2))
    expected = "match-closed" if args.until_close else "channel-closed"
    return 0 if end == expected else 1


if __name__ == "__main__":
    sys.exit(main())
