"""Synthetic ERP inputs; semantic expectations live in the tests, not this builder."""

import json
from pathlib import Path
from uuid import uuid4


def order_profile():
    profile = json.loads(
        Path(__file__)
        .parents[1]
        .joinpath("examples/erp-import-profile.json")
        .read_text(encoding="utf-8")
    )
    profile["profile_id"] = f"profile-{uuid4().hex}"
    external_order_id = f"erp-order-{uuid4().hex}"
    profile["expected_order"]["external_order_id"] = external_order_id
    for mapping in profile["items"].values():
        mapping["item_id"] = f"internal-{uuid4().hex}"
    order = {
        "message_id": f"message-{uuid4().hex}",
        "external_order_id": external_order_id,
        "revision": 1,
        "item_ids": ["assembly-001", "assembly-002", "assembly-003"],
        "item_type_id": "bracket-demo",
        "route_id": "route-bracket",
        "route_version": "1",
        "control_plan_version": "1",
        "checkpoints": [
            {
                "checkpoint_id": checkpoint,
                "region_id": "face-A",
                "method_version": "emulator-1",
                "repeat_after_rework": True,
            }
            for checkpoint in ("incoming-surface", "after-machining", "final-surface")
        ],
        "components": {
            "assembly-001": ["bracket-001", "insert-001"],
            "assembly-002": ["bracket-002", "insert-002"],
            "assembly-003": ["bracket-003", "insert-003"],
        },
    }
    return order, profile


class BoundaryStub:
    """Only for isolated mapper/transport tests; never presented as the shared schema."""

    def validate(self, name, value):
        pass
