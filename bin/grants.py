"""Grants of the loop, version 0: the org chart that the loop reads, one JSON file per position.

A grant says where a position sits — its parent, `human` for the root —, how many sessions it can
have alive together, how many tokens its sessions can spend, how the loop launches a session on it,
and, if it declares any, the parameters that whoever asks a spawn on it must pass, each from a
closed list of values. Whoever configures the project writes the grants under `<state>/grants/`; the loop only reads
them. The switch of a position is not in its grant: it is state of the loop, and it starts off.

The shape is checked by validate-grants.py against schema/grant.schema.json; the consistency that
the schema does not express is here, in standard library, because validate-grants.py and the loop
share it. Standard library only, because the loop imports it.
"""

import json
from pathlib import Path

HUMAN = "human"
DIRECTORY = "grants"
DEFAULT_TOOLS = ("Bash",)
DEFAULT_PERMISSION_MODE = "dontAsk"
PLACEHOLDERS = ("request", "code", "state", "session", "position")


def grant_files(directory):
    """The grant files of a directory: `*.json`, without hidden files, in name order."""
    return sorted(path for path in Path(directory).glob("*.json") if not path.name.startswith("."))


def read_directory(directory):
    """file name → content, for every grant file; a file that is not readable JSON maps to None."""
    contents = {}
    for path in grant_files(directory):
        try:
            contents[path.name] = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            contents[path.name] = None
    return contents


def fingerprint(directory):
    """What changes when a grant file is added, removed or rewritten: (name, size, mtime) of every file."""
    entries = []
    for path in grant_files(directory):
        try:
            status = path.stat()
        except OSError:
            continue
        entries.append((path.name, status.st_size, status.st_mtime_ns))
    return tuple(entries)


def consistency_errors(contents):
    """The errors that the schema does not see, on a set of grants already valid in shape:
    file name equal to the position, no position called `human`, exactly one root, every parent
    among the declared positions, no cycle, no parameter named as a placeholder of the loop — the
    loop would not know which value to write —, no parameter on the root, whose first session
    starts from its switch, which carries none."""
    errors = []
    grants = {}
    for name, grant in contents.items():
        if name != f"{grant['position']}.json":
            errors.append(f"{name}: the file of position {grant['position']} must be called {grant['position']}.json")
        if grant["position"] == HUMAN:
            errors.append(f"{name}: `{HUMAN}` is the parent of the root, not a position")
        grants[grant["position"]] = grant
    roots = [position for position, grant in grants.items() if grant["parent"] == HUMAN]
    if len(roots) != 1:
        errors.append(f"exactly one root is needed, a position with parent `{HUMAN}`: found {len(roots)} {sorted(roots)}")
    for position, grant in grants.items():
        for name in grant.get("parameters") or {}:
            if name in PLACEHOLDERS:
                errors.append(f"{position}: the parameter {name} has the name of a placeholder of the loop, "
                              f"{{{name}}}: the names {', '.join(PLACEHOLDERS)} are taken")
        if grant["parent"] == HUMAN and grant.get("parameters"):
            errors.append(f"{position}: the root takes no parameters: its first session starts from its switch")
        if grant["parent"] != HUMAN and grant["parent"] not in grants:
            errors.append(f"{position}: the parent {grant['parent']} is not a declared position")
    for position in grants:
        seen = [position]
        current = grants[position]["parent"]
        while current in grants and current != HUMAN:
            if current in seen:
                errors.append(f"{position}: cycle of parents {' → '.join(seen + [current])}")
                break
            seen.append(current)
            current = grants[current]["parent"]
    return errors


def by_position(contents):
    """position → grant, from file name → content."""
    return {grant["position"]: grant for grant in contents.values()}


def root(grants):
    return next(position for position, grant in grants.items() if grant["parent"] == HUMAN)


def children(grants, position):
    return sorted(child for child, grant in grants.items() if grant["parent"] == position)


def parameter_errors(grant, parameters, text):
    """The reason a spawn with these parameters and this text does not fit the grant, as
    (reason, detail in Italian), or None if it fits. Every declared parameter is required and takes
    a value of its list; a parameter the grant does not declare is rejected; a position with
    parameters takes no free text, because the parameters are the whole contract with whoever
    spawns. The detail goes back to whoever asked, an LLM session too."""
    declared = grant.get("parameters") or {}
    if declared and text:
        return "text-not-allowed", (f"{grant['position']} prende solo parametri ({', '.join(f'{name}=…' for name in declared)}), "
                                    "non testo libero")
    unknown = sorted(name for name in parameters if name not in declared)
    if unknown:
        allowed = f"i parametri di {grant['position']} sono {', '.join(declared)}" if declared             else f"{grant['position']} non prende parametri"
        return "unknown-parameter", f"{', '.join(unknown)}: {allowed}"
    missing = [name for name in declared if name not in parameters]
    if missing:
        return "missing-parameter", f"manca {', '.join(f'{name}=…' for name in missing)}: ogni parametro di {grant['position']} è obbligatorio"
    for name, definition in declared.items():
        if parameters[name] not in definition["values"]:
            return "value-not-allowed", f"{name}={parameters[name]}: i valori ammessi sono {', '.join(definition['values'])}"
    return None


def substitute(text, values):
    """Replaces {name} in a text for every name of `values`: the placeholders of the loop and the
    parameters of the spawn. Nothing else: a brace that names neither stays as it is."""
    for name, value in values.items():
        text = text.replace("{" + name + "}", str(value))
    return text


def resolved_launch(grant, values, parameters=None):
    """The launch template of the grant with the defaults filled in and the placeholders replaced.
    `values` carries request, code, state, session and position; `parameters` the values of the
    spawn, already checked against the grant. Both are replaced in the prompt, in the command and in
    the permission rules. The defaults are the most closed: safe mode on, Bash only, in Bash only
    the request command, dontAsk, an empty directory under the state root."""
    launch = dict(grant["launch"])
    values = {**values, **(parameters or {})}
    state = Path(values["state"])
    cwd = launch.get("cwd")
    launch["cwd"] = str(state / "loop" / "work" / values["session"]) if cwd is None else str(state / cwd)
    if launch["type"] == "script":
        launch["command"] = [substitute(part, values) for part in launch["command"]]
        return launch
    launch["prompt"] = substitute(launch["prompt"], values)
    if "allowed_tools" in launch:
        launch["allowed_tools"] = [substitute(rule, values) for rule in launch["allowed_tools"]]
    launch.setdefault("safe_mode", True)
    launch.setdefault("tools", list(DEFAULT_TOOLS))
    launch.setdefault("allowed_tools", [f"Bash({values['request']} *)"])
    launch.setdefault("permission_mode", DEFAULT_PERMISSION_MODE)
    launch.setdefault("model", None)
    return launch
