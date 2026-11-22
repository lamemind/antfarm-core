#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["jsonschema>=4.18"]
# ///
"""Gate of the grants of the loop: exits 0 if the directory holds a valid org chart, 1 if it does not.

Checks every `*.json` of the directory against schema/grant.schema.json, then the consistency the
schema does not express (bin/grants.py): file name equal to the position, no position called
`human`, exactly one root, every parent among the declared positions, no cycle. A file that is not
readable JSON makes the set invalid.

The loop runs it at start and every time the grants directory changes: at start an invalid set
stops the loop, while running it keeps the last valid set.

Usage:  antfarm-core/bin/validate-grants.py <grants directory>
Exits 2 on wrong usage or if the directory does not exist.
"""

import json
import sys
from pathlib import Path

import jsonschema

sys.path.insert(0, str(Path(__file__).resolve().parent))
import grants as libgrants  # noqa: E402

SCHEMA = Path(__file__).resolve().parent.parent / "schema" / "grant.schema.json"


def forbidden_fields(schema, launch):
    """The fields of the launch that its type does not take: the `false` properties of the
    `then` branch for a script, of the `else` branch for an llm."""
    branch = schema["$defs"]["launch"]["then" if launch.get("type") == "script" else "else"]
    return [field for field, allowed in branch["properties"].items() if allowed is False and field in launch]


def shape_errors(name, grant, schema, validator):
    for error in sorted(validator.iter_errors(grant), key=lambda e: list(e.absolute_path)):
        position = "/".join(str(part) for part in error.absolute_path) or "(root)"
        if error.validator is None:
            # A `false` subschema: jsonschema stops the error path at the launch and does not name
            # the field, so the field is read from the schema.
            for field in forbidden_fields(schema, grant["launch"]):
                yield f"{name}: shape: {position}/{field}: a {grant['launch'].get('type')} launch does not take this field"
            continue
        yield f"{name}: shape: {position}: {error.message}"


def main():
    if len(sys.argv) != 2 or not Path(sys.argv[1]).is_dir():
        print(__doc__.strip(), file=sys.stderr)
        return 2
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)
    contents = libgrants.read_directory(sys.argv[1])
    errors = [f"{name}: not readable as JSON" for name, grant in contents.items() if grant is None]
    for name, grant in contents.items():
        if grant is not None:
            errors += list(shape_errors(name, grant, schema, validator))
    if not contents:
        errors.append("no grant file: exactly one root is needed")
    if not errors:
        errors = libgrants.consistency_errors(contents)
    for error in errors:
        print(error, file=sys.stderr)
    if errors:
        print(f"INVALID GRANTS: {len(errors)} errors — {sys.argv[1]}", file=sys.stderr)
        return 1
    grants = libgrants.by_position(contents)
    print(f"valid grants: {len(grants)} positions, root {libgrants.root(grants)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
