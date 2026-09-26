"""Synthetic server-only demo; inputs and independent expectations are in examples/."""

import argparse
import json
from pathlib import Path
from uuid import uuid4

import httpx


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--tokens", type=Path, default=Path("../.local/demo-tokens.json"))
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1] / "examples"
    item_id = f"demo-{uuid4().hex[:12]}"
    inputs = json.loads(
        (root / "server-demo.json").read_text(encoding="utf-8").replace("$ITEM", item_id)
    )
    expected = json.loads((root / "server-demo-expected.json").read_text(encoding="utf-8"))
    tokens = json.loads(args.tokens.read_text(encoding="utf-8"))
    source_roles = {"vision-1": "vision", "mes-source": "mes", "erp-source": "erp"}
    with httpx.Client(base_url=args.url, timeout=30, trust_env=False) as client:

        def request(method, path, body=None, role="controller", status=200):
            result = client.request(
                method, path, json=body, headers={"Authorization": f"Bearer {tokens[role]}"}
            )
            if result.status_code != status:
                raise RuntimeError(f"{method} {path}: {result.status_code} {result.text}")
            return result.json()

        request(
            "POST",
            "/api/v2/imports/production-context",
            {
                "item_id": item_id,
                "import_id": item_id,
                "context": inputs["context"],
            },
            "integration",
            201,
        )
        events = [inputs["envelope"] | event for event in inputs["events"]]
        for event in events:
            request("POST", "/api/v2/events", event, source_roles[event["source_id"]], 201)
        duplicate = request("POST", "/api/v2/events", events[-1], "vision")
        assert duplicate["status"] == expected["duplicate"]["status"]
        basis = request("POST", f"/api/v2/items/{item_id}/bases", {}, status=201)
        command = {
            "action": "released",
            "reason": "Synthetic normal control reviewed",
            "expected_revision": basis["context_revision"],
            "expected_basis_id": basis["basis_id"],
            "idempotency_key": item_id,
        }
        path = f"/api/v2/items/{item_id}/dispositions"
        released = request("POST", path, command, status=201)
        assert request("POST", path, command) == released
        state = request("GET", f"/api/v2/items/{item_id}")
        assert state["kpi"]["confirmed_defects"] == expected["normal"]["confirmed_defects"]
        assert state["release_allowed_now"] is expected["normal"]["release_allowed_now"]
        assert state["disposition"] == expected["normal"]["disposition"]
        late = events[-1] | {"event_id": item_id + "-late", "occurred_at": "2026-09-26T10:08:30Z"}
        request("POST", "/api/v2/events", late, "vision", 201)
        old_basis = request("GET", f"/api/v2/bases/{basis['basis_id']}")
        assert old_basis == basis
        stale = request("POST", path, command | {"idempotency_key": item_id + "-stale"}, status=409)
        assert stale["error"]["code"] == expected["stale"]["error_code"]
        assert request("POST", path, command) == released
        state = request("GET", f"/api/v2/items/{item_id}")
        for key, value in expected["late"].items():
            assert state[key] == value
        print(
            json.dumps(
                {
                    "item_id": item_id,
                    "basis_id": basis["basis_id"],
                    "checks": [
                        "normal_release",
                        "duplicate",
                        "late_review",
                        "immutable_basis",
                        "stale_rejection",
                        "exact_command_retry",
                    ],
                    "disposition": state["disposition"],
                    "blockers": state["blockers"],
                    "outbox_messages": len(state["outbox"]),
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
