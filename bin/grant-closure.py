#!/usr/bin/env python3
"""Closure of the grants of the loop: what a position can come to do, its own permission rules and
those of every position it can make be born, down its subtree.

    antfarm-core/bin/grant-closure.py <grants directory> [--json]
    antfarm-core/bin/grant-closure.py <grants directory> --forbid <glob> [--forbid <glob>...]

On the loop a session asks spawns only on the child positions of its own, the grants are written by
whoever configures the project and never by the loop, and the permission rules of a session are the
`launch.allowed_tools` of its grant, whatever text its spawn carries: the text changes what the
child does inside its rules, not which rules it has. So what a position can reach is the union of
the rules of its subtree, a visit of the tree, with no session to observe.

Under dontAsk a session also uses without a rule the built-in tools of its `tools` field that ask
no permission (WITHOUT_RULE): each enters the closure as a rule with its bare name, `Read`, the text
of an allow rule for the whole tool, marked as granted without a rule.

For every position, in the order of the tree, the command prints its closure: every rule of the
position and of its descendants, each with the position it comes from. A rule that names a parameter
of its grant becomes one rule per value of the parameter, marked with the value. The placeholders of
the loop ({request}, {code}, {state}, {session}, {position}) and those of the setup of a game
([[name]]) stay as they are, marked as not expanded. An llm grant without allowed_tools has the
default rule of the loop, marked as default. A position whose rules do not bound what it can do is
marked not analyzable, with its reason, in its own closure and in the closure of every ancestor: a
script position, which has no rules and runs its command, and an llm position whose permission_mode
is not dontAsk, where a command outside the rules can still run. --json prints the same closures as
one JSON object.

With --forbid the command is a gate: for every glob it names each position whose closure holds a
matching rule, with the rule and the position the rule comes from, and it exits 1 if a glob matches.
The glob is fnmatch's — `*`, `?`, `[...]`, case-sensitive — on the text of the rule after the
expansion of the parameters, not the permission semantics of Claude Code: the prefix rule
`Bash(git *)` does not match the glob `Bash(git push*)`. A position not analyzable does not fail the
gate: the report names it, because the gate does not see inside it.

The grants are read as the loop reads them, with the same function, validated_grants of
bin/grants.py: bin/validate-grants.py on the directory, then the files; the rules are the ones
bin/grants.py resolves for a launch, defaults included.
Standard library only, like the loop.

Exits 0; 1 when a --forbid glob matches a rule of a closure; 2 on wrong usage, or when
validate-grants.py rejects the directory: then it prints the reason of the validator and no closure.
"""

import argparse
import fnmatch
import itertools
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import grants as libgrants  # noqa: E402

# A placeholder of the loop keeps its own name when the rules are resolved: the closure holds for
# every session and every state root.
UNRESOLVED = {name: "{" + name + "}" for name in libgrants.PLACEHOLDERS}
NOT_EXPANDED = re.compile(r"\{(?:" + "|".join(libgrants.PLACEHOLDERS) + r")\}|\[\[[^\[\]]+\]\]")
# The only permission mode where a command outside the rules is denied without asking.
BOUNDING_MODE = "dontAsk"
# The built-in tools that run without a permission rule under dontAsk: the ones the tools reference
# of Claude Code (code.claude.com/docs/en/tools-reference, Claude Code 2.1.332) marks as asking no
# permission, and the two that ask it only in some cases, EnterWorktree and Skill. The file-access
# ones, Read, Grep and Glob, still ask outside the working directories, and dontAsk denies there.
WITHOUT_RULE = frozenset({
    "Agent", "AskUserQuestion", "CronCreate", "CronDelete", "CronList", "EndConversation", "EnterPlanMode",
    "EnterWorktree", "Glob", "Grep", "ListAgents", "ListMcpResourcesTool", "ListPlugins", "ListSkills", "LSP",
    "propose_skills", "PushNotification", "Read", "ReadMcpResourceTool", "RemoteTrigger", "REPL",
    "ReportFindings", "ScheduleWakeup", "SearchPlugins", "SearchSkills", "SendFeedback", "SendMessage",
    "SendUserFile", "Skill", "SubagentHandback", "SuggestPluginInstall", "SuggestSkills", "TaskCreate",
    "TaskGet", "TaskList", "TaskOutput", "TaskStop", "TaskUpdate", "TodoWrite", "ToolSearch",
    "WaitForMcpServers",
})
# The value of `tools` that gives a session the default set of Claude Code: every tool of WITHOUT_RULE
# counts as granted.
DEFAULT_SET = "default"


def subtree(grants, position):
    """The position and its descendants, in preorder, the children in name order."""
    yield position
    for child in libgrants.children(grants, position):
        yield from subtree(grants, child)


def expanded_rules(position, grant, launch):
    """The rules of an llm launch, one per combination of the values of the parameters each rule
    names: a rule that names no parameter stays one rule."""
    declared = grant.get("parameters") or {}
    default = "allowed_tools" not in grant["launch"]
    rules = []
    for template in launch["allowed_tools"]:
        named = [name for name in declared if "{" + name + "}" in template]
        for values in itertools.product(*(declared[name]["values"] for name in named)):
            parameters = dict(zip(named, values))
            text = libgrants.substitute(template, parameters)
            rules.append({"position": position, "rule": text, "parameters": parameters, "default": default,
                          "not_expanded": list(dict.fromkeys(NOT_EXPANDED.findall(text))), "without_rule": False})
    return rules


def unruled_tools(position, tools):
    """The built-in tools of a launch that run without a rule under dontAsk, each as a rule with its
    bare name, the text of an allow rule for the whole tool."""
    names = sorted(WITHOUT_RULE) if DEFAULT_SET in tools else [tool for tool in tools if tool in WITHOUT_RULE]
    return [{"position": position, "rule": name, "parameters": {}, "default": False, "not_expanded": [],
             "without_rule": True} for name in names]


def describe(position, grant):
    """What a position can do by itself: its launch, its rules, and why it is not analyzable, if it is not."""
    launch = grant["launch"]
    entry = {"parent": grant["parent"], "type": launch["type"]}
    if launch["type"] == "script":
        command = " ".join(launch["command"])
        return {**entry, "command": command, "rules": [], "not_analyzable": f"script: {command}"}
    resolved = libgrants.resolved_launch(grant, UNRESOLVED)
    mode = resolved["permission_mode"]
    return {**entry, "permission_mode": mode, "tools": resolved["tools"], "safe_mode": resolved["safe_mode"],
            "rules": expanded_rules(position, grant, resolved) + unruled_tools(position, resolved["tools"]),
            "not_analyzable": None if mode == BOUNDING_MODE else f"permission_mode {mode}: a command outside the rules can run"}


def closures(grants):
    """position → its entry with the closure, in the order of the tree from the root."""
    entries = {position: describe(position, grants[position]) for position in subtree(grants, libgrants.root(grants))}
    for position, entry in entries.items():
        members = list(subtree(grants, position))
        entry["closure"] = {
            "positions": members,
            "rules": [rule for member in members for rule in entries[member]["rules"]],
            "not_analyzable": [{"position": member, "reason": entries[member]["not_analyzable"]}
                               for member in members if entries[member]["not_analyzable"]],
        }
    return entries


def count(number, noun):
    return f"{number} {noun}" + ("" if number == 1 else "s")


def notes(rule):
    words = [f"{name}={value}" for name, value in rule["parameters"].items()]
    if rule["without_rule"]:
        words.append("granted without a rule")
    if rule["default"]:
        words.append("default")
    if rule["not_expanded"]:
        words.append("not expanded " + " ".join(rule["not_expanded"]))
    return " · ".join(words)


def heading(position, entry):
    if entry["type"] == "script":
        return f"{position}  (script)"
    words = ["llm", entry["permission_mode"], "tools " + (",".join(entry["tools"]) or "none")]
    if not entry["safe_mode"]:
        words.append("safe mode off")
    return f"{position}  ({' · '.join(words)})"


def print_closures(directory, entries):
    root = next(iter(entries))
    width = max(len(position) for position in entries)
    print(f"grant closure of {directory}: {len(entries)} positions, root {root}")
    for position, entry in entries.items():
        closure = entry["closure"]
        origins = len({rule["position"] for rule in closure["rules"]})
        print()
        print(heading(position, entry))
        print(f"  own {count(len(entry['rules']), 'rule')} · closure {count(len(closure['rules']), 'rule')}"
              f" from {count(origins, 'position')} · {len(closure['not_analyzable'])} not analyzable")
        rule_width = max((len(rule["rule"]) for rule in closure["rules"]), default=0)
        for member in closure["positions"]:
            for rule in entries[member]["rules"]:
                print(f"    {member:<{width}}  {rule['rule']:<{rule_width}}  {notes(rule)}".rstrip())
            if entries[member]["not_analyzable"]:
                print(f"    {member:<{width}}  not analyzable — {entries[member]['not_analyzable']}")


def gate(entries, globs):
    """Prints, for every glob, the positions whose closure holds a matching rule; True if one does."""
    width = max(len(position) for position in entries)
    forbidden = False
    for glob in globs:
        hits = [(position, rule) for position, entry in entries.items() for rule in entry["closure"]["rules"]
                if fnmatch.fnmatchcase(rule["rule"], glob)]
        if not hits:
            print(f"allowed {glob!r}: no closure holds a matching rule")
            continue
        forbidden = True
        positions = list(dict.fromkeys(position for position, _ in hits))
        print(f"FORBIDDEN {glob!r}: {count(len(positions), 'position')} reach a matching rule — {', '.join(positions)}")
        for position, rule in hits:
            print(f"  {position:<{width}}  via {rule['position']:<{width}}  {rule['rule']}")
    outside = [(position, entry["not_analyzable"]) for position, entry in entries.items() if entry["not_analyzable"]]
    for position, reason in outside:
        print(f"not analyzable, outside the gate: {position} — {reason}")
    return forbidden


def main():
    parser = argparse.ArgumentParser(description="Closure of the grants of the loop: for every position, the "
                                     "permission rules of its subtree.")
    parser.add_argument("directory", type=Path, help="the grants directory")
    output = parser.add_mutually_exclusive_group()
    output.add_argument("--json", action="store_true", help="the closures as one JSON object")
    output.add_argument("--forbid", action="append", metavar="GLOB",
                        help="gate: exit 1 if a closure holds a rule that matches the glob; repeatable")
    arguments = parser.parse_args()
    if not arguments.directory.is_dir():
        parser.error(f"{arguments.directory} is not a directory")
    grants, reason = libgrants.validated_grants(arguments.directory)
    if grants is None:
        print(reason, file=sys.stderr)
        return 2
    entries = closures(grants)
    if arguments.forbid:
        return 1 if gate(entries, arguments.forbid) else 0
    if arguments.json:
        print(json.dumps({"directory": str(arguments.directory), "root": next(iter(entries)), "positions": entries},
                         ensure_ascii=False, indent=2))
    else:
        print_closures(arguments.directory, entries)
    return 0


if __name__ == "__main__":
    sys.exit(main())
