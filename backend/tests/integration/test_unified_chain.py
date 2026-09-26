"""Real HTTP core + protected PostgreSQL ledger + graph + external HTTP receiver.

Fixtures supply physical facts explicitly; none are inferred from successful tests.
"""

import copy
import json
import os
import threading
from pathlib import Path
from uuid import uuid4

import pytest
from contracts.validation import validate
from emulator.server import Receiver, make_server
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.adapters.postgres import quality_bases, raw_events
from app.adapters.protection import AesGcmProtector
from app.api.server import create_app
from app.application.delivery import deliver
from app.application.unified import UnifiedChain
from app.domain.errors import EventConflict

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def integrated(database, tmp_path):
    _, writer, _ = database
    prefix = uuid4().hex[:10]
    item_id = f"{prefix}-assembly-001"
    ids = {
        old: f"{prefix}-{old}"
        for old in (
            "assembly-001",
            "assembly-002",
            "assembly-003",
            "bracket-001",
            "bracket-002",
            "bracket-003",
            "insert-001",
            "insert-002",
            "insert-003",
            "order-001",
            "run-machining-001",
            "run-assembly-001",
            "run-rework-001",
        )
    }

    def translate(value):
        if isinstance(value, str):
            return ids.get(value, value)
        if isinstance(value, dict):
            return {ids.get(key, key): translate(part) for key, part in value.items()}
        if isinstance(value, list):
            return [translate(part) for part in value]
        return value

    order = translate(json.loads((ROOT / "data/v2/work-orders.json").read_text())[0])
    inputs = translate(json.loads((ROOT / "data/v2/source-events.json").read_text()))
    for event in inputs["events"].values():
        event["source_id"] = f"{prefix}-{event['source_id']}"
        event["event_id"] = f"{prefix}-{event['event_id']}"
    credentials = {
        "controller-test": {"actor_id": "controller-demo", "role": "controller"},
        "master-test": {"actor_id": "master-demo", "role": "master"},
        "integrator-test": {"actor_id": "integrator-demo", "role": "integrator"},
        "vision-test": {
            "actor_id": "camera-demo",
            "role": "source",
            "source_id": f"{prefix}-vision-emulator-1",
        },
        "line-test": {
            "actor_id": "line-demo",
            "role": "source",
            "source_id": f"{prefix}-line-emulator-1",
        },
    }
    protector = AesGcmProtector({("test", 1): os.urandom(32)}, active_key=("test", 1))
    chain = UnifiedChain(writer, protector, namespace=f"pilot-{prefix}")
    client = TestClient(create_app(chain, credentials), raise_server_exceptions=False)
    assert (
        client.post(
            "/api/v2/imports/work-orders",
            json=order,
            headers={"Authorization": "Bearer integrator-test"},
        ).status_code
        == 200
    )
    composition = {
        "snapshot_id": f"composition-{prefix}-1",
        "owner_id": "synthetic-MES-owner",
        "proof_id": f"synthetic-completeness-{prefix}",
        "items": [
            *order["item_ids"],
            *[component for parts in order["components"].values() for component in parts],
        ],
        "memberships": [
            {
                "assertion_id": f"installed-{component}",
                "component_id": component,
                "assembly_id": assembly,
                "state": "current",
            }
            for assembly, parts in order["components"].items()
            for component in parts
        ],
    }
    assert (
        client.post(
            "/api/v2/composition", json=composition, headers={"Authorization": "Bearer master-test"}
        ).status_code
        == 200
    )

    def send(key):
        event = inputs["events"][key]
        token = "vision-test" if event["source_id"].endswith("vision-emulator-1") else "line-test"
        response = client.post(
            "/api/v2/events", json=event, headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code in {200, 201}, response.text
        return response.json()

    def get_item():
        response = client.get(
            f"/api/v2/items/{item_id}", headers={"Authorization": "Bearer controller-test"}
        )
        assert response.status_code == 200, response.text
        validate("Item", response.json())
        return response.json()

    def command(kind, action, **extra):
        item = get_item()
        target = item["nonconformities"][0] if kind in {"decisions", "resolutions"} else item
        path = (
            f"/api/v2/nonconformities/{target['id']}/{kind}"
            if "id" in target
            else f"/api/v2/items/{item_id}/{kind}"
        )
        field = (
            "decision"
            if kind == "decisions"
            else "disposition"
            if kind == "dispositions"
            else "action"
        )
        body = {
            "reason": "Synthetic independent review",
            "expected_revision": item["context_revision"],
            "expected_basis_id": target["basis_id"],
            "idempotency_key": uuid4().hex,
            **extra,
        }
        if kind != "control-cycles":
            body[field] = action
        response = client.post(
            path,
            json=body,
            headers={
                "Authorization": "Bearer master-test"
                if kind == "control-cycles"
                else "Bearer controller-test"
            },
        )
        return response, body, path

    yield chain, client, inputs, send, get_item, command, order, composition, ids
    client.close()


def test_real_chain_pinned_bases_graph_kpi_and_full_external_ack(integrated, tmp_path):
    chain, client, inputs, send, get_item, command, order, _, ids = integrated
    receipts = {key: send(key) for key in inputs["base_order"]}
    before = get_item()
    card = before["nonconformities"][0]
    basis_before = chain.basis(card["basis_id"])
    validate("Basis", basis_before)
    assert len(basis_before["inputs"]) == len(inputs["base_order"])
    assert basis_before["pinned_versions"]["route"] == "1"
    assert basis_before["pinned_versions"]["factual_composition"]
    assert {version["version"] for version in basis_before["analyzer_versions"]} == {"emulator-1"}
    assert chain.raw(receipts["final-1"]["record_id"]) == inputs["events"]["final-1"]
    response, _, _ = command("decisions", "confirm")
    assert response.status_code == 201, response.text
    response, _, _ = command("dispositions", "rework")
    assert response.status_code == 201, response.text
    old_basis = chain.basis(get_item()["basis_id"])
    prior = chain.analytics([ids["assembly-001"]])["counts"]
    duplicate = send("final-1")
    assert duplicate["status"] == "duplicate"
    assert chain.analytics([ids["assembly-001"]])["counts"] == prior
    late = send("late-incoming")
    after = get_item()
    assert after["review_required"]
    assert chain.basis(old_basis["basis_id"]) == old_basis
    new_basis = chain.basis(after["basis_id"])
    assert late["record_id"] not in {x["record_id"] for x in old_basis["inputs"]}
    assert new_basis["contradictions"] and any(
        x["role"] == "contradictory" for x in new_basis["inputs"]
    )
    assert new_basis["previous_decisions"]
    assert new_basis["impact"]["paths"]
    counts = chain.analytics([ids["assembly-001"]])["counts"]
    assert counts["confirmed_defects"] == 1 and counts["inspected_items"] == 1
    stale = {
        "decision": "confirm",
        "reason": "old context",
        "expected_revision": before["context_revision"],
        "expected_basis_id": card["basis_id"],
        "idempotency_key": "stale",
    }
    rejected = client.post(
        f"/api/v2/nonconformities/{card['id']}/decisions",
        json=stale,
        headers={"Authorization": "Bearer controller-test"},
    )
    assert rejected.status_code == 409, rejected.text
    assert rejected.json()["error"]["code"] == "stale_revision"
    # Explicit new review; contradiction remains historically visible, not erased.
    response, _, _ = command("decisions", "confirm")
    assert response.status_code == 201, response.text
    response, _, _ = command("dispositions", "rework")
    assert response.status_code == 201, response.text
    rework = response.json()
    response, _, _ = command(
        "control-cycles",
        None,
        rework_disposition_id=rework["decision_id"],
        planned_operation_run_ids=[ids["run-rework-001"]],
    )
    assert response.status_code == 201, response.text
    # KPI regression: opening a new cycle cannot erase previously inspected items.
    assert chain.analytics([ids["assembly-001"]])["counts"]["inspected_items"] == 1
    fresh = {key: send(key) for key in inputs["rework_order"]}
    response, saved_body, path = command(
        "resolutions",
        "resolve",
        rework_operation_run_id=ids["run-rework-001"],
        previous_operation_run_id=ids["run-assembly-001"],
        control_record_ids=[fresh["final-recheck"]["record_id"]],
    )
    assert response.status_code == 201, response.text
    repeat = client.post(path, json=saved_body, headers={"Authorization": "Bearer controller-test"})
    assert repeat.status_code == 200 and repeat.json() == response.json()
    response, _, _ = command("dispositions", "released")
    assert response.status_code == 201, response.text
    final = get_item()
    assert final["disposition"] == "released" and final["release_allowed_now"]
    final_basis = chain.basis(final["basis_id"])
    assert final_basis["excluded_inputs"] and all(
        x["reason"] for x in final_basis["excluded_inputs"]
    )
    kpi = chain.analytics([ids["assembly-001"], ids["assembly-001"]])["counts"]
    assert kpi["confirmed_defects"] == 1 and kpi["resolved_confirmed_defects"] == 1
    assert kpi["open_confirmed_defects"] == 0 and kpi["rework_operations"] == 1
    receiver = Receiver(tmp_path / "erp.sqlite", [order], producer_id=chain.namespace)
    token = uuid4().hex
    server = make_server(receiver, token)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        body = chain.pending()[-1]
        validate("QualityResult", body, external=True)
        assert receiver.receive(body)[1]["application_status"] == "applied"
        result = deliver(chain, f"http://127.0.0.1:{server.server_port}", token)
        assert result["acknowledged"] > 0 and chain.pending() == []
        assert receiver.state(body)["current"]["body"]["disposition"] == "released"
        assert receiver.state(body)["current"]["body"]["result_version"] == max(
            value["body"]["result_version"]
            for value in chain.execute(lambda state, _, __: state["wire"]).values()
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_component_signal_reaches_parent_without_false_merge(integrated):
    chain, _, inputs, send, get_item, _, _, _, ids = integrated
    for key in inputs["base_order"]:
        send(key)
    before = get_item()
    component = copy.deepcopy(inputs["events"]["late-incoming"])
    component.update(item_id=ids["insert-001"], event_id=uuid4().hex)
    receipt = chain.ingest(json.dumps(component).encode(), component["source_id"])
    basis = chain.basis(get_item()["basis_id"])
    assert receipt["record_id"] in {value["record_id"] for value in basis["inputs"]}
    assert any(ids["insert-001"] in value["path"] for value in basis["impact"]["paths"])
    assert len(get_item()["nonconformities"]) == len(before["nonconformities"])
    assert receipt["projection_status"] == "unresolved"  # component control cycle not asserted
    assert (
        "item_or_assembly_link_unresolved" not in receipt["issues"]
    )  # identity itself is reliable
    other = chain.item(ids["assembly-002"])
    assert receipt["record_id"] not in {
        x["record_id"] for x in chain.basis(other["basis_id"])["inputs"]
    }


def test_multi_signs_one_event_and_stable_ref_not_three_defects(integrated):
    chain, _, inputs, send, get_item, command, _, _, ids = integrated
    for key in inputs["base_order"]:
        send(key)
    event = copy.deepcopy(inputs["events"]["final-1"])
    event["event_id"] = uuid4().hex
    event["payload"]["defects"] = [
        {**event["payload"]["defects"][0], "defect_ref": "scratch-A"},
        {**event["payload"]["defects"][0], "defect_ref": "scratch-B"},
    ]
    receipt = chain.ingest(json.dumps(event).encode(), event["source_id"])
    assert receipt["status"] == "accepted"
    cards = get_item()["nonconformities"]
    assert len(cards) == 3  # one original mark + two distinct signs in one payload
    response, _, _ = command("decisions", "confirm")  # fixture helper selects first card
    assert response.status_code == 201
    # Leave remaining two candidates pending: only one confirmed logical defect.
    counts = chain.analytics([ids["assembly-001"], ids["assembly-001"]])["counts"]
    assert counts["confirmed_defects"] == 1
    assert counts["confirmed_nonconforming_items"] == 1
    before = get_item()["context_revision"]
    chain.ingest(json.dumps(event).encode(), event["source_id"])
    assert get_item()["context_revision"] == before
    assert chain.analytics([ids["assembly-001"]])["counts"] == counts


def test_early_component_observation_later_installation_rechecks_existing_raw(integrated):
    chain, _, inputs, send, get_item, _, _, composition, ids = integrated
    # Physical owner currently reports the component as not installed.
    early = composition | {
        "snapshot_id": uuid4().hex,
        "memberships": [
            edge for edge in composition["memberships"] if edge["component_id"] != ids["insert-001"]
        ],
    }
    chain.set_composition(early)
    component = copy.deepcopy(inputs["events"]["late-incoming"])
    component.update(item_id=ids["insert-001"], event_id=uuid4().hex)
    receipt = chain.ingest(json.dumps(component).encode(), component["source_id"])
    assert receipt["record_id"] not in {
        x["record_id"] for x in chain.basis(get_item()["basis_id"])["inputs"]
    }
    # A later installation links already accepted evidence; source does not resend it.
    chain.set_composition(composition | {"snapshot_id": uuid4().hex})
    assert receipt["record_id"] in {
        x["record_id"] for x in chain.basis(get_item()["basis_id"])["inputs"]
    }


def test_missing_proof_and_uncertain_identity_are_explicit_blockers(integrated):
    chain, _, inputs, send, get_item, _, _, composition, _ = integrated
    missing = composition | {"proof_id": None, "snapshot_id": uuid4().hex}
    chain.set_composition(missing)
    send("unknown-item")
    current = get_item()
    basis = chain.basis(current["basis_id"])
    assert not current["release_allowed_now"]
    assert "composition_completeness_unknown" in current["blockers"]
    assert basis["unknowns"] and basis["impact"]["paths"]


def test_open_unknown_scope_still_blocks_future_item(integrated):
    chain, _, inputs, send, get_item, _, order, _, ids = integrated
    send("unknown-item")
    new_id = f"future-{uuid4().hex}"
    second_order = copy.deepcopy(order)
    second_order.update(
        external_order_id=f"order-{uuid4().hex}", message_id=uuid4().hex, item_ids=[new_id]
    )
    second_order["components"] = {new_id: []}
    chain.import_order(second_order)
    future = chain.item(new_id)
    assert "scope_unknown" in future["blockers"]
    assert not future["release_allowed_now"]
    assert chain.basis(future["basis_id"])["impact"]["paths"]


def test_history_cycle_terminates_and_current_conflict_is_unknown(integrated):
    chain, _, inputs, send, get_item, _, order, original, ids = integrated
    old = copy.deepcopy(original)
    old["snapshot_id"] = uuid4().hex
    old["memberships"] = [{**link, "state": "historical"} for link in old["memberships"]] + [
        {
            "assertion_id": uuid4().hex,
            "component_id": ids["assembly-001"],
            "assembly_id": ids["insert-001"],
            "state": "current",
        }
    ]
    result = chain.set_composition(old)
    assert "current_membership_cycle" not in result["issues"]
    assert get_item()["basis_id"]  # history cycle does not cause infinite walk
    conflict = copy.deepcopy(original)
    conflict["snapshot_id"] = uuid4().hex
    conflict["memberships"].append(
        {
            "assertion_id": uuid4().hex,
            "component_id": ids["insert-001"],
            "assembly_id": ids["assembly-002"],
            "state": "current",
        }
    )
    saved = chain.set_composition(conflict)
    assert "conflicting_current_memberships" in saved["issues"]
    assert not get_item()["release_allowed_now"]
    assert "conflicting_current_memberships" in get_item()["blockers"]


def test_source_cannot_take_human_decision_and_conflict_keeps_original(integrated):
    chain, client, inputs, send, get_item, command, _, _, _ = integrated
    for key in inputs["base_order"]:
        send(key)
    item = get_item()
    card = item["nonconformities"][0]
    forbidden = client.post(
        f"/api/v2/nonconformities/{card['id']}/decisions",
        json={},
        headers={"Authorization": "Bearer vision-test"},
    )
    assert forbidden.status_code == 403
    changed = copy.deepcopy(inputs["events"]["final-1"])
    changed["payload"]["confidence"] = 0.8
    with pytest.raises(EventConflict):
        chain.ingest(json.dumps(changed).encode(), changed["source_id"])
    assert get_item()["context_revision"] == item["context_revision"]
    with chain.engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(quality_bases)) > 0
        assert connection.scalar(select(func.count()).select_from(raw_events)) > 0
