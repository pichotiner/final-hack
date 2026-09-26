"""Replay fixed source events through the real /api/v2 boundary, not a fake core.

Does not invent server IDs, decisions or ACKs. Authorized decisions are submitted
through the pinned-review UI or a future role 1 acceptance runner.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import NAMESPACE_URL, uuid5

from contracts.validation import validate, validate_source_event


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=["base_order", "late_order", "rework_order"])
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--duplicate", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--data", type=Path, default=Path("data/v2/source-events.json"))
    parser.add_argument(
        "--cycle-receipt",
        type=Path,
        help="Saved server ControlCycle response, required for rework",
    )
    args = parser.parse_args()
    dataset = json.loads(args.data.read_text(encoding="utf-8"))
    cycle = None
    if args.stage == "rework_order" and not args.dry_run:
        if args.cycle_receipt is None:
            parser.error("rework_order requires --cycle-receipt from the server")
        cycle = json.loads(args.cycle_receipt.read_text(encoding="utf-8"))
        validate("ControlCycle", cycle)
    event_ids = dataset[args.stage]
    if args.duplicate:
        event_ids = [*event_ids, event_ids[-1]]
    for event_id in event_ids:
        event = dataset["events"][event_id]
        if cycle:
            event = json.loads(json.dumps(event))
            event["event_id"] = str(
                uuid5(NAMESPACE_URL, repr((event_id, cycle["control_cycle_id"])))
            )
            event_id = event["event_id"]
            event["payload"]["control_cycle_id"] = cycle["control_cycle_id"]
            if event["event_type"] == "inspection_observed":
                checkpoint = event["payload"]["checkpoint_id"]
                event["payload"]["control_session_id"] = cycle["control_sessions"][
                    checkpoint
                ]
                event["operation_run_id"] = cycle["checkpoint_operation_run_ids"][
                    checkpoint
                ]
            elif (
                event["operation_run_id"]
                not in cycle["checkpoint_operation_run_ids"].values()
            ):
                parser.error(
                    "The fixture rework run is not registered in the supplied cycle"
                )
        validate_source_event(event)
        if args.dry_run:
            print(f"{event_id}: {event['event_type']} {event['occurred_at']}")
            continue
        token = os.environ.get(
            "VISION_SOURCE_TOKEN"
            if event["source_id"] == "vision-emulator-1"
            else "LINE_SOURCE_TOKEN",
            "",
        )
        if not token:
            parser.error(f"Source credential missing for {event['source_id']}")
        request = Request(
            args.base_url.rstrip("/") + "/api/v2/events",
            method="POST",
            data=json.dumps(event).encode(),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urlopen(request, timeout=10) as response:
                receipt = json.load(response)
                validate("EventReceipt", receipt)
                if (
                    receipt["event_id"] != event_id
                    or receipt["source_id"] != event["source_id"]
                ):
                    raise ValueError("Receipt mismatch")
                print(
                    f"{event_id}: HTTP {response.status}, {receipt['status']}, {receipt['projection_status']}"
                )
        except HTTPError as error:
            raise SystemExit(
                f"{event_id}: rejected with HTTP {error.code}; remaining events not sent"
            ) from error


if __name__ == "__main__":
    main()
