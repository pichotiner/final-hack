"""Send synthetic source events to the backend without changing expected results."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data/scenarios.json"


def request_json(url: str, method: str, token: str, payload: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    request = Request(url, data=data,
                      headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"}, method=method)
    try:
        with urlopen(request, timeout=10) as response:
            return response.status, json.load(response)
    except HTTPError as exc:
        try:
            body = json.load(exc)
        except ValueError:
            body = {"error": {"code": "invalid_response", "message": exc.reason}}
        return exc.code, body


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("scenario", choices=["first-slice", "normal", "incoming-defect", "poor-and-multiple"])
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--duplicate", action="store_true", help="Replay selected events with the same source and ID")
    parser.add_argument("--late", action="store_true", help="Send late events after the main scenario")
    parser.add_argument("--late-only", action="store_true", help="Send only the late events after review")
    parser.add_argument("--dry-run", action="store_true", help="Print order and IDs without sending")
    parser.add_argument("--decisions", action="store_true", help="Apply scripted controller decisions after events")
    parser.add_argument("--negative", action="store_true", help="Send invalid/conflicting first-slice events and verify expected errors")
    args = parser.parse_args()
    dataset = json.loads(DATA.read_text(encoding="utf-8"))
    scenario = next(s for s in dataset["scenarios"] if s["id"] == args.scenario)
    if args.negative and (args.scenario != "first-slice" or args.late_only):
        parser.error("--negative requires the main first-slice replay")
    events = list(scenario["late_events"] if args.late_only else scenario["events"])
    if args.duplicate:
        events += [next(e for e in scenario["events"] if e["event_id"] == event_id) for event_id in scenario["duplicate_event_ids"]]
    if args.late:
        events += scenario["late_events"]
    for index, event in enumerate(events, 1):
        if args.dry_run:
            print(f"{index:02d} {event['event_id']} {event['event_type']} {event['occurred_at']}")
            continue
        token = os.environ.get("VISION_SOURCE_TOKEN" if event["source_id"] == "vision-emulator-1" else "LINE_SOURCE_TOKEN", "")
        if not token:
            parser.error(f"Missing token for {event['source_id']}; set VISION_SOURCE_TOKEN or LINE_SOURCE_TOKEN")
        try:
            status, body = request_json(args.base_url.rstrip("/") + "/api/v1/events", "POST", token, event)
        except URLError as exc:
            parser.error(f"Backend unavailable: {exc}")
        print(f"{index:02d} {event['event_id']}: HTTP {status} {body.get('status') or body.get('error', {}).get('code')}")
        if status not in (200, 201):
            raise SystemExit(1)
    if args.decisions and not args.late_only:
        controller_token = os.environ.get("CONTROLLER_TOKEN", "")
        if not args.dry_run and not controller_token:
            parser.error("Missing CONTROLLER_TOKEN for --decisions")
        for action in scenario["actions"]:
            if args.dry_run:
                print(f"ACTION {action['kind']} {action['item_id']}: {action.get('decision') or action.get('disposition')}")
                continue
            base = args.base_url.rstrip("/")
            if action["kind"] == "nonconformity":
                status, page = request_json(base + "/api/v1/nonconformities", "GET", controller_token)
                if status != 200:
                    parser.error(f"Cannot list nonconformities: HTTP {status}")
                cards = [card for card in page["items"] if card["item_id"] == action["item_id"]]
                if not cards:
                    parser.error(f"No nonconformity for {action['item_id']}")
                for card in cards:
                    command = {"decision": action["decision"], "reason": action["reason"],
                               "expected_revision": card["revision"],
                               "idempotency_key": f"demo-{args.scenario}-{card['id']}-{action['decision']}",
                               "cause_status": "unresolved", "cause_category": "unknown", "evidence_event_ids": []}
                    status, body = request_json(base + f"/api/v1/nonconformities/{card['id']}/decisions", "POST", controller_token, command)
                    print(f"ACTION {card['id']}: HTTP {status} {body.get('id') or body.get('error', {}).get('code')}")
                    if status not in (200, 201):
                        raise SystemExit(1)
            else:
                item_id = action["item_id"]
                status, item = request_json(base + f"/api/v1/items/{item_id}", "GET", controller_token)
                if status != 200:
                    parser.error(f"Cannot get item {item_id}: HTTP {status}")
                command = {"disposition": action["disposition"], "reason": action["reason"],
                           "expected_revision": item["revision"],
                           "idempotency_key": f"demo-{args.scenario}-{item_id}-{action['disposition']}"}
                status, body = request_json(base + f"/api/v1/items/{item_id}/dispositions", "POST", controller_token, command)
                print(f"ACTION {item_id}: HTTP {status} {body.get('id') or body.get('error', {}).get('code')}")
                if status not in (200, 201):
                    raise SystemExit(1)
    if args.negative:
        expected = json.loads((ROOT / "data/expected.json").read_text(encoding="utf-8"))["negative_cases"]
        for case in dataset["negative_cases"]:
            source_event = case["event"]
            if args.dry_run:
                print(f"NEGATIVE {case['id']}: expected HTTP {expected[case['id']]['http_status']}")
                continue
            token = os.environ.get("VISION_SOURCE_TOKEN" if source_event["source_id"] == "vision-emulator-1" else "LINE_SOURCE_TOKEN", "")
            if not token:
                parser.error(f"Missing token for {source_event['source_id']}")
            status, body = request_json(args.base_url.rstrip("/") + "/api/v1/events", "POST", token, source_event)
            code = body.get("error", {}).get("code")
            want = expected[case["id"]]
            print(f"NEGATIVE {case['id']}: HTTP {status} {code}")
            if status != want["http_status"] or code != want["error_code"]:
                raise SystemExit(f"Unexpected response for {case['id']}: wanted {want}")


if __name__ == "__main__":
    main()
