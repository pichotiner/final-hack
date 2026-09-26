"""Imported order through live P3 HTTP up to released; external ACK is a separate gate.

This uses the current draft commands. It does not claim shared-contract A04 acceptance.
"""

import hashlib
import importlib
import socket
import time
from threading import Thread
from uuid import uuid4

import httpx
import pytest
import uvicorn

from app.adapters.erp_orders import HttpWorkOrders
from app.api.server import create_app
from app.application.production import Principal

pytestmark = pytest.mark.integration
pytest_plugins = ("tests.integration.test_erp_import",)


def test_imported_order_confirm_rework_repeat_control_resolve_release(
    imported, role2_root, trace_document, tmp_path
):
    order, mapper, journal, service = imported
    for token, roles, source_id in [
        ("master-test", {"master"}, None),
        ("vision-emulator-1", {"source"}, "vision-emulator-1"),
        ("line-emulator-1", {"source"}, "line-emulator-1"),
    ]:
        service.identities.entries[hashlib.sha256(token.encode()).hexdigest()] = Principal(
            token, frozenset(roles), source_id
        )
    module = importlib.import_module("emulator.server")
    receiver = module.Receiver(tmp_path / "a04-erp.sqlite", [order])
    with module.make_server(receiver, "erp-test") as erp, socket.socket() as listener:
        erp_thread = Thread(target=erp.serve_forever, daemon=True)
        erp_thread.start()
        listener.bind(("127.0.0.1", 0))
        server = uvicorn.Server(
            uvicorn.Config(create_app(service), log_level="error", lifespan="off")
        )
        api_thread = Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        api_thread.start()
        reader = HttpWorkOrders(
            f"http://127.0.0.1:{erp.server_port}/external/v2/work-orders",
            "erp-test",
            mapper.contract,
        )
        try:
            for _ in range(100):
                if server.started:
                    break
                assert api_thread.is_alive(), "API exited before readiness."
                time.sleep(0.05)
            else:
                pytest.fail("API did not become ready.")
            receipt = service.import_work_order("integration-test", next(reader.orders()), mapper)
            assert receipt["status"] == "accepted"
            item_id = mapper.profile["items"]["assembly-001"]["item_id"]
            context = mapper.map(order)[item_id]
            with httpx.Client(
                base_url=f"http://127.0.0.1:{listener.getsockname()[1]}",
                trust_env=False,
                timeout=15,
            ) as client:

                def request(method, path, body=None, token="controller-test", expected=200):
                    response = client.request(
                        method, path, json=body, headers={"Authorization": f"Bearer {token}"}
                    )
                    assert response.status_code == expected, response.text
                    return response.json()

                def state():
                    return request("GET", f"/api/v2/items/{item_id}")

                def basis(target=None):
                    result = request(
                        "POST",
                        f"/api/v2/items/{item_id}/bases",
                        {"target_id": target},
                        expected=201,
                    )
                    return {
                        "expected_revision": result["context_revision"],
                        "expected_basis_id": result["basis_id"],
                        "idempotency_key": str(uuid4()),
                        "reason": "synthetic imported-order A04",
                    }

                def event(kind, timestamp, *, payload=None, **links):
                    source = (
                        "vision-emulator-1" if kind == "inspection_observed" else "line-emulator-1"
                    )
                    document = trace_document(
                        str(uuid4()), kind, timestamp, item_id=item_id, source_id=source, **links
                    )
                    document["payload"].update(payload or {})
                    return request("POST", "/api/v2/events", document, source, 201)

                def operation(run, start, finish, *, previous=None, station="assembly"):
                    for kind, timestamp in [
                        ("operation_started", start),
                        ("operation_finished", finish),
                    ]:
                        event(
                            kind,
                            timestamp,
                            operation_run_id=run,
                            station_id=station,
                            payload={"previous_operation_run_id": previous} if previous else {},
                        )

                def inspection(rule, timestamp, cycle, session, run, *, defect=False):
                    return event(
                        "inspection_observed",
                        timestamp,
                        operation_run_id=run,
                        payload={
                            "checkpoint_id": rule.checkpoint_id,
                            "inspection_phase": rule.inspection_phase,
                            "analyzer_version": rule.analyzer_version,
                            "control_cycle_id": cycle,
                            "control_session_id": session,
                            "inspection_result": "defect_signs" if defect else "no_defect_signs",
                            "defects": [
                                {
                                    "defect_ref": "mark-001",
                                    "type": "surface-mark",
                                    "component_id": "insert-001",
                                    "region_id": "face-A",
                                }
                            ]
                            if defect
                            else [],
                        },
                    )

                cycle = state()["cycle_id"]
                event("item_received", "10:00:00")
                incoming, machining, final = context.controls
                inspection(incoming, "10:02:00", cycle, incoming.control_session_id, None)
                operation("run-machining-001", "10:03:00", "10:05:00", station="machining")
                inspection(
                    machining, "10:06:00", cycle, machining.control_session_id, "run-machining-001"
                )
                operation("run-assembly-001", "10:07:00", "10:09:00")
                inspection(
                    final,
                    "10:10:00",
                    cycle,
                    final.control_session_id,
                    "run-assembly-001",
                    defect=True,
                )
                nc = state()["nonconformities"][0]["nonconformity_id"]
                confirm_command = basis(nc) | {"action": "confirm"}
                confirm_basis = request(
                    "GET", f"/api/v2/bases/{confirm_command['expected_basis_id']}"
                )
                confirm = request(
                    "POST", f"/api/v2/nonconformities/{nc}/decisions", confirm_command, expected=201
                )
                disposition_path = f"/api/v2/items/{item_id}/dispositions"
                request("POST", disposition_path, basis() | {"action": "rework"}, expected=201)
                sessions = {
                    rule.checkpoint_id: f"rework-{rule.checkpoint_id}" for rule in context.controls
                }
                runs = {
                    incoming.checkpoint_id: None,
                    machining.checkpoint_id: "repair-1",
                    final.checkpoint_id: "repair-1",
                }
                command = basis() | {
                    "operation_run_ids": ["repair-1"],
                    "control_sessions": sessions,
                    "checkpoint_operation_run_ids": runs,
                }
                cycle_path = f"/api/v2/items/{item_id}/control-cycles"
                new_cycle = request("POST", cycle_path, command, "master-test", 201)
                assert request("POST", cycle_path, command, "master-test") == new_cycle
                cycle = new_cycle["cycle_id"]
                operation("repair-1", "10:20:00", "10:22:00", previous="run-assembly-001")
                # A late delivery with an old registered session cannot close the new cycle.
                inspection(
                    incoming,
                    "10:02:30",
                    confirm_basis["cycle_id"],
                    incoming.control_session_id,
                    None,
                )
                assert "missing_checkpoint:incoming-surface" in state()["blockers"]
                for rule in context.controls:
                    inspection(
                        rule,
                        "10:23:00",
                        cycle,
                        sessions[rule.checkpoint_id],
                        runs[rule.checkpoint_id],
                    )
                blocked = request(
                    "POST", disposition_path, basis() | {"action": "released"}, expected=409
                )
                assert blocked["error"]["code"] == "release_blocked"
                observation = next(
                    part
                    for part in state()["observations"]
                    if part["cycle_id"] == cycle and part["checkpoint_id"] == final.checkpoint_id
                )
                resolve = request(
                    "POST",
                    f"/api/v2/nonconformities/{nc}/resolutions",
                    basis(nc)
                    | {
                        "action": "resolve",
                        "rework_operation_id": "repair-1",
                        "previous_operation_run_id": "run-assembly-001",
                        "observation_ids": [observation["observation_id"]],
                    },
                    expected=201,
                )
                released = request(
                    "POST", disposition_path, basis() | {"action": "released"}, expected=201
                )
                current = state()
                assert current["disposition"] == "released"
                assert current["blockers"] == []
                assert current["kpi"]["confirmed_defects"] == 1
                assert current["kpi"]["open_confirmed_defects"] == 0
                assert current["kpi"]["resolved_confirmed_defects"] == 1
                assert current["kpi"]["rework_operations"] == 1
                assert {part["decision_id"] for part in current["decisions"]} == {
                    confirm["decision_id"],
                    resolve["decision_id"],
                }
                assert request("GET", f"/api/v2/bases/{confirm_basis['basis_id']}") == confirm_basis
                # Successful retry still returns the original decision after the whole lifecycle.
                assert (
                    request("POST", f"/api/v2/nonconformities/{nc}/decisions", confirm_command)
                    == confirm
                )
                message = max(
                    journal.quality.state(item_id)["outbox"],
                    key=lambda part: part["result_version"],
                )
                assert (
                    message["payload"]["disposition_decision"]["decision_id"]
                    == released["decision_id"]
                )
                assert message["status"] == "pending"
        finally:
            reader.close()
            server.should_exit = True
            api_thread.join(timeout=10)
            erp.shutdown()
            erp_thread.join(timeout=5)
