"""CLI for the same bounded membership engine used by the unified coordinator."""

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from app.domain.evidence_graph import (
    Change,
    Completeness,
    Membership,
    Snapshot,
    TraversalLimitExceeded,
    assess,
    load_case,
)

__all__ = [
    "Change",
    "Completeness",
    "Membership",
    "Snapshot",
    "TraversalLimitExceeded",
    "assess",
    "load_case",
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", default="late-component")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    document = json.loads(Path(__file__).with_name("impact_inputs.json").read_text())
    if args.case not in document["cases"]:
        parser.error("Unknown case")
    snapshot, change = load_case(document, args.case)
    result = assess(snapshot, change)
    if args.json:
        print(json.dumps(asdict(result), indent=2))
    else:
        print(f"SYNTHETIC input: {args.case}; coverage: {result.coverage}")
        for entry in result.explanations:
            print(f"  {entry.item_id}: {entry.classification}")
            print(f"    {entry.reason}: {' -> '.join(entry.path) or entry.completeness_reference}")


if __name__ == "__main__":
    main()
