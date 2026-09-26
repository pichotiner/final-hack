"""Independent role 2 expectations through P5 HTTP and real PostgreSQL.

ASGI transport exercises the public boundary; TCP/worker/ERP are covered by A04.
No private domain method is used to execute a human command.
"""

import copy
from uuid import uuid4

import httpx
import pytest

from app.adapters.shared_contract import validate
from app.api.server import create_app
from tests.integration.test_api_postgres import auth, get, post, seed, source
from tests.integration.test_shared_api import command
from tests.integration.test_traceability_postgres import final_inspection, normal_production

pytestmark = [pytest.mark.integration, pytest.mark.anyio]
pytest_plugins = ("tests.integration.test_api_postgres",)


async def test_a01_a08_duplicate_then_late_rejects_stale_without_new_effects(
    production, http_service
):
    item_id = production[5]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(http_service, shared=True)),
        base_url="http://test",
    ) as client:
        events = await seed(client, production, defect=True)
        item = await get(client, f"/api/v2/items/{item_id}")
        target = item["nonconformities"][0]["id"]
        prepared = await command(client, item_id, target)
        old_basis = await get(client, f"/api/v2/bases/{prepared['expected_basis_id']}")
        before = http_service.quality.state(item_id)
        await post(client, "/api/v2/events", events[-1], events[-1]["source_id"], expected=200)
        assert http_service.quality.state(item_id) == before
        late = copy.deepcopy(events[-1])
        late["event_id"] = str(uuid4())
        late["occurred_at"] = "2026-09-25T10:10:30Z"
        await source(client, late)
        now = await get(client, f"/api/v2/items/{item_id}")
        assert len(now["nonconformities"]) == 1
        new_basis = await get(client, f"/api/v2/bases/{now['basis_id']}")
        signs = [o for o in new_basis["analysis_snapshot"]["observations"] if o["defect_type"]]
        assert len(signs) == 2
        after_late = http_service.quality.state(item_id)
        rejected = await post(
            client,
            f"/api/v2/nonconformities/{target}/decisions",
            prepared | {"decision": "confirm"},
            expected=409,
        )
        validate("ErrorResponse", rejected)
        assert rejected["error"]["code"] == "stale_revision"
        assert http_service.quality.state(item_id) == after_late
        assert await get(client, f"/api/v2/bases/{old_basis['basis_id']}") == old_basis


async def test_a07_poor_negative_is_not_release_evidence(production, http_service):
    events = []
    fixture = (*production[:4], events.append, *production[5:])
    normal_production(fixture)
    final_inspection(fixture)
    events[-1]["payload"]["observation_quality"] = "poor"
    item_id = production[5]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(http_service, shared=True)),
        base_url="http://test",
    ) as client:
        for event in events:
            await source(client, event)
        item = await get(client, f"/api/v2/items/{item_id}")
        basis = await get(client, f"/api/v2/bases/{item['basis_id']}")
        obs = next(o for o in basis["analysis_snapshot"]["observations"] if o["quality"] == "poor")
        assert obs["result"] == "no_defect_signs"
        assert obs["effective_result"] == "not_assessable"
        assert not item["release_allowed_now"]
        prepared = await command(client, item_id)
        before = http_service.quality.state(item_id)
        refused = await post(
            client,
            f"/api/v2/items/{item_id}/dispositions",
            prepared | {"disposition": "released"},
            expected=409,
        )
        assert refused["error"]["code"] == "release_blocked"
        assert http_service.quality.state(item_id) == before


async def test_a03_original_command_survives_revoked_access_and_late(production, http_service):
    import hashlib

    item_id = production[5]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(http_service, shared=True)),
        base_url="http://test",
    ) as client:
        events = await seed(client, production, defect=True)
        item = await get(client, f"/api/v2/items/{item_id}")
        target = item["nonconformities"][0]["id"]
        prepared = await command(client, item_id, target)
        body = prepared | {"decision": "confirm"}
        path = f"/api/v2/nonconformities/{target}/decisions"
        original = await post(client, path, body)
        key = hashlib.sha256(b"controller").hexdigest()
        principal = http_service.identities.entries.pop(key)
        try:
            response = await client.post(path, json=body, headers=auth())
            assert response.status_code in {401, 403}
        finally:
            http_service.identities.entries[key] = principal
        late = copy.deepcopy(events[-1])
        late["event_id"] = str(uuid4())
        await source(client, late)
        before_retry = http_service.quality.state(item_id)
        assert await post(client, path, body, expected=200) == original
        assert http_service.quality.state(item_id) == before_retry
        conflict = await post(client, path, body | {"reason": "different"}, expected=409)
        assert conflict["error"]["code"] == "idempotency_conflict"
