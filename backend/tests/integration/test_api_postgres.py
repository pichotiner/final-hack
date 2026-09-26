import hashlib
import json
from dataclasses import asdict
from uuid import uuid4

import httpx
import pytest

from app.api.server import create_app
from app.application.production import IdentityDirectory, Principal, ProductionService
from tests.integration.test_traceability_postgres import final_inspection, normal_production

pytestmark = [pytest.mark.integration, pytest.mark.anyio]
pytest_plugins = ("tests.integration.test_traceability_postgres",)


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def http_service(production, protector):
    journal = production[2]
    identities = {
        hashlib.sha256(token.encode()).hexdigest(): Principal(actor, frozenset({role}))
        for token, actor, role in [
            ("controller", "controller-1", "controller"),
            ("master", "master-1", "master"),
            ("integration", "erp-adapter", "integration"),
            ("manager", "manager-1", "manager"),
        ]
    }
    for authority in production[7].source_authorities:
        identities[hashlib.sha256(authority.source_id.encode()).hexdigest()] = Principal(
            authority.source_id, frozenset({"source"}), authority.source_id
        )
    return ProductionService(journal, protector, IdentityDirectory(identities))


def auth(token="controller"):
    return {"Authorization": f"Bearer {token}"}


async def post(client, path, body, token="controller", expected=201):
    response = await client.post(path, json=body, headers=auth(token))
    assert response.status_code == expected, response.text
    return response.json()


async def basis(client, item_id, target=None):
    result = await post(client, f"/api/v2/items/{item_id}/bases", {"target_id": target})
    return {
        "expected_revision": result["context_revision"],
        "expected_basis_id": result["basis_id"],
        "reason": "synthetic review",
        "idempotency_key": str(uuid4()),
    }


async def get(client, path):
    response = await client.get(path, headers=auth())
    assert response.status_code == 200, response.text
    return response.json()


async def source(client, document):
    return await post(client, "/api/v2/events", document, document["source_id"])


async def seed(client, production, *, defect=False):
    events = []
    fixture = (*production[:4], events.append, *production[5:])
    normal_production(fixture)
    final_inspection(fixture, defect=defect)
    for document in events:
        await source(client, document)
    return events


async def test_http_release_late_basis_exact_retry_permissions_and_restart(
    production,
    http_service,
):
    item_id, context = production[5], production[7]
    transport = httpx.ASGITransport(app=create_app(http_service))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(f"/api/v2/items/{item_id}")
        assert response.status_code == 401
        imported = await post(
            client,
            "/api/v2/imports/production-context",
            {
                "item_id": item_id,
                "import_id": f"http-{item_id}",
                "context": asdict(context),
            },
            "integration",
        )
        assert imported["status"] == "accepted"
        events = await seed(client, production)
        current = await get(client, f"/api/v2/items/{item_id}")
        assert current["release_allowed_now"] is True
        command = await basis(client, item_id)
        command["action"] = "released"
        path = f"/api/v2/items/{item_id}/dispositions"
        before = await get(client, f"/api/v2/bases/{command['expected_basis_id']}")
        release = await post(client, path, command)
        assert release["actor_id"] == "controller-1"
        assert await post(client, path, command, expected=200) == release
        await post(client, path, command | {"reason": "changed"}, expected=409)
        await post(client, path, command, "manager", expected=403)
        await post(client, path, command | {"actor_id": "forged"}, expected=422)
        await post(client, "/api/v2/events", events[-1], events[-1]["source_id"], expected=200)
        late = events[-1] | {"event_id": "late-good", "occurred_at": "2026-09-25T10:10:30Z"}
        await source(client, late)
        assert await get(client, f"/api/v2/bases/{command['expected_basis_id']}") == before
        stale = await post(client, path, command | {"idempotency_key": "stale"}, expected=409)
        assert stale["error"]["code"] == "stale_revision"
        assert await post(client, path, command, expected=200) == release
        timeline = await get(client, f"/api/v2/items/{item_id}/timeline")
        assert timeline["dispositions"][0]["decision_id"] == release["decision_id"]
    # Recreate HTTP/service composition; PostgreSQL retains the successful command.
    restarted = create_app(
        ProductionService(
            http_service.journal, http_service.ingestion.protector, http_service.identities
        )
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=restarted), base_url="http://test"
    ) as client:
        assert await post(client, path, command, expected=200) == release


async def test_http_poor_reject_does_not_release_and_malformed_json_is_not_accepted(
    production,
    http_service,
):
    item_id = production[5]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(http_service)), base_url="http://test"
    ) as client:
        events = []
        fixture = (*production[:4], events.append, *production[5:])
        normal_production(fixture)
        final_inspection(fixture, defect=True)
        events[-1]["payload"]["observation_quality"] = "poor"
        for document in events:
            await source(client, document)
        state = await get(client, f"/api/v2/items/{item_id}")
        nc = state["nonconformities"][0]["nonconformity_id"]
        command = await basis(client, item_id, nc)
        await post(
            client, f"/api/v2/nonconformities/{nc}/decisions", command | {"action": "reject"}
        )
        release = await basis(client, item_id)
        blocked = await post(
            client,
            f"/api/v2/items/{item_id}/dispositions",
            release | {"action": "released"},
            expected=409,
        )
        assert blocked["error"]["code"] == "release_blocked"
        assert "not_assessable:final" in blocked["error"]["details"][0]["blockers"]
        before = http_service.quality.state(item_id)
        for body in [b'{"action":"rework","action":"released"}', b'{"x": NaN}', b"{"]:
            response = await client.post(
                f"/api/v2/items/{item_id}/dispositions", content=body, headers=auth()
            )
            assert response.status_code == 422
        assert http_service.quality.state(item_id) == before
        response = await client.post(
            "/api/v2/events", content=json.dumps(events[0]), headers=auth(events[-1]["source_id"])
        )
        assert response.status_code == 403


async def test_http_confirm_rework_resolve_separate_release(production, http_service):
    item_id, document = production[5], production[3]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(http_service)), base_url="http://test"
    ) as client:
        await seed(client, production, defect=True)
        state = await get(client, f"/api/v2/items/{item_id}")
        nc = state["nonconformities"][0]["nonconformity_id"]
        command = await basis(client, item_id, nc)
        await post(
            client, f"/api/v2/nonconformities/{nc}/decisions", command | {"action": "confirm"}
        )
        command = await basis(client, item_id)
        await post(client, f"/api/v2/items/{item_id}/dispositions", command | {"action": "rework"})
        command = await basis(client, item_id)
        command.update(
            operation_run_ids=["repair-1"],
            control_sessions={"incoming": "repair-in", "final": "repair-final"},
            checkpoint_operation_run_ids={"incoming": None, "final": "repair-1"},
        )
        path = f"/api/v2/items/{item_id}/control-cycles"
        await post(client, path, command, expected=403)
        cycle = await post(client, path, command, "master")
        assert await post(client, path, command, "master", expected=200) == cycle
        for kind, time in [("operation_started", "10:20:00"), ("operation_finished", "10:22:00")]:
            event = document(
                kind + "-repair", kind, time, operation_run_id="repair-1", station_id="assembly"
            )
            event["payload"]["previous_operation_run_id"] = "run-2"
            await source(client, event)
        for checkpoint, phase, run in [
            ("incoming", "incoming", None),
            ("final", "final", "repair-1"),
        ]:
            event = document(
                f"repair-{checkpoint}",
                "inspection_observed",
                "10:23:00",
                station_id="final",
                operation_run_id=run,
            )
            event["payload"].update(
                checkpoint_id=checkpoint,
                inspection_phase=phase,
                analyzer_version="analyzer-1",
                control_cycle_id=cycle["cycle_id"],
                control_session_id=command["control_sessions"][checkpoint],
            )
            await source(client, event)
        command = await basis(client, item_id)
        await post(
            client,
            f"/api/v2/items/{item_id}/dispositions",
            command | {"action": "released"},
            expected=409,
        )
        state = await get(client, f"/api/v2/items/{item_id}")
        observation = next(
            v
            for v in state["observations"]
            if v["cycle_id"] == cycle["cycle_id"] and v["checkpoint_id"] == "final"
        )
        command = await basis(client, item_id, nc)
        await post(
            client,
            f"/api/v2/nonconformities/{nc}/resolutions",
            command
            | {
                "action": "resolve",
                "rework_operation_id": "repair-1",
                "previous_operation_run_id": "run-2",
                "observation_ids": [observation["observation_id"]],
            },
        )
        command = await basis(client, item_id)
        await post(
            client, f"/api/v2/items/{item_id}/dispositions", command | {"action": "released"}
        )
        state = await get(client, f"/api/v2/items/{item_id}")
        assert state["kpi"]["confirmed_defects"] == 1
        assert state["kpi"]["open_confirmed_defects"] == 0
        assert state["kpi"]["resolved_confirmed_defects"] == 1
        assert state["kpi"]["rework_operations"] == 1
        assert state["disposition"] == "released"
