"""Conservative recursive contract drift gate, including items/nullable/request/response.

It flags any shape change for review (not a theorem of JSON-Schema compatibility).
v1 -> v2 removal is intentionally reported as breaking, never silently accepted.
"""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path

import yaml


def differences(old, new, path=""):
    if type(old) is not type(new):
        return [path or "/"]
    if isinstance(old, dict):
        result = []
        for key in sorted(old.keys() | new.keys()):
            child = f"{path}/{key}"
            if key not in old or key not in new:
                result.append(child)
            else:
                result.extend(differences(old[key], new[key], child))
        return result
    if isinstance(old, list):
        if len(old) != len(new):
            return [path]
        return [
            hit
            for index, (a, b) in enumerate(zip(old, new))
            for hit in differences(a, b, f"{path}/{index}")
        ]
    return [] if old == new else [path]


def shape(doc):
    # Documentation text can change without invalidating the shape baseline.
    ignored = {
        "description",
        "summary",
        "title",
        "example",
        "examples",
        "servers",
        "tags",
    }

    # Keys in these maps are user-defined names, not OpenAPI/schema keywords.
    named_maps = {
        "paths",
        "webhooks",
        "schemas",
        "$defs",
        "definitions",
        "properties",
        "patternProperties",
        "dependentSchemas",
        "dependencies",
        "responses",
        "content",
        "headers",
        "securitySchemes",
        "parameters",
        "requestBodies",
        "links",
        "callbacks",
        "encoding",
        "mapping",
    }
    literal_values = {
        "const",
        "enum",
        "default",
        "required",
        "dependentRequired",
        "security",
    }

    def clean(value, *, named=False):
        if isinstance(value, dict):
            return {
                k: copy.deepcopy(v)
                if not named and k in literal_values
                else clean(v, named=not named and k in named_maps)
                for k, v in value.items()
                if named or k not in ignored
            }
        if isinstance(value, list):
            return [clean(v) for v in value]
        return value

    return clean({"paths": doc["paths"], "components": doc["components"]})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("spec", type=Path)
    parser.add_argument("baseline", type=Path)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    current = shape(yaml.safe_load(args.spec.read_text(encoding="utf-8")))
    if args.write:
        args.baseline.write_text(
            json.dumps(current, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return
    hits = differences(json.loads(args.baseline.read_text(encoding="utf-8")), current)
    if hits:
        print("Contract shape changed; review required:\n" + "\n".join(hits[:30]))
        raise SystemExit(1)
    print("Contract shape matches baseline")


if __name__ == "__main__":
    main()
