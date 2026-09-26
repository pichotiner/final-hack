"""Shared schema checked on real core responses; no mocked domain decisions."""

import copy
from dataclasses import replace
from uuid import uuid4

import httpx
import pytest

from app.adapters.shared_contract import validate
from app.api.server import create_app
from app.domain.errors import InvalidTransition
from app.domain.quality import _digest
from tests.integration.test_api_postgres import auth, get, post, seed, source

pytestmark = [pytest.mark.integration, pytest.mark.anyio]
pytest_plugins = ("tests.integration.test_api_postgres",)


async def command(client, item_id, target=None):
    item = await get(client, f"/api/v2/items/{item_id}")
    basis_id = (
        next(card["basis_id"] for card in item["nonconformities"] if card["id"] == target)
        if target
        else item["basis_id"]
    )
    basis = await get(client, f"/api/v2/bases/{basis_id}")
    validate("Basis", basis)
    return {
        "expected_basis_id": basis_id,
        "expected_revision": item["context_revision"],
        "reason": "synthetic shared review",
        "idempotency_key": str(uuid4()),
    }


async def test_normal_release_has_erp_decision_even_without_defects_and_late_preserves_it(
    production, http_service
):
    item_id = production[5]
    # This test uses a new stream whose first message has not been published yet;
    # production fixture already used draft, so use a new explicitly pinned item.
    new_id = f"shared-{uuid4().hex}"
    context = replace(production[7], external_item_id=new_id, erp_result_profile="dp01-0.4")
    http_service.journal.import_context(new_id, context, import_id=str(uuid4()), actor_id="test")
    fixture = list(production)
    old_document = production[3]
    fixture[3] = lambda *args, **kwargs: old_document(*args, **{**kwargs, "item_id": new_id})
    fixture[5] = new_id
    item_id = new_id
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(http_service, shared=True)),
        base_url="http://test",
    ) as client:
        events = await seed(client, tuple(fixture))
        prepared = await command(client, item_id)
        before = await get(client, f"/api/v2/bases/{prepared['expected_basis_id']}")
        first = await get(client, f"/api/v2/items/{item_id}")
        assert (await get(client, f"/api/v2/items/{item_id}"))["basis_id"] == first["basis_id"]
        released = await post(
            client, f"/api/v2/items/{item_id}/dispositions", prepared | {"disposition": "released"}
        )
        payload = max(
            http_service.quality.state(item_id)["outbox"], key=lambda msg: msg["result_version"]
        )["payload"]
        validate("QualityResult", payload, external=True)
        assert payload["confirmed_nonconformities"] == []
        assert payload["disposition_decision"]["decision_id"] == released["decision_id"]
        late = copy.deepcopy(events[-1])
        late.update(event_id="late-shared", occurred_at="2026-09-25T10:10:30Z")
        await source(client, late)
        message = max(
            http_service.quality.state(item_id)["outbox"], key=lambda msg: msg["result_version"]
        )
        assert message["payload"]["disposition_decision"] == payload["disposition_decision"]
        assert message["payload"]["review_required"] is True
        assert message["content_digest"] == _digest(message["payload"])
        assert await get(client, f"/api/v2/bases/{before['basis_id']}") == before
        assert (
            await post(
                client,
                f"/api/v2/items/{item_id}/dispositions",
                prepared | {"disposition": "released"},
                expected=200,
            )
            == released
        )
        denied = await post(
            client,
            f"/api/v2/items/{item_id}/dispositions",
            prepared | {"disposition": "released"},
            "manager",
            expected=403,
        )
        validate("ErrorResponse", denied)
        validate("LineStatus", await get(client, "/api/v2/lines/line-1/status"))
        validate("TimelinePage", await get(client, f"/api/v2/items/{item_id}/timeline"))
    with pytest.raises(InvalidTransition, match="profile"):
        http_service.journal.import_context(
            item_id,
            replace(context, erp_result_profile="draft-p3"),
            import_id=str(uuid4()),
            actor_id="test",
        )


async def test_basis_distinguishes_two_signs_and_pins_analysis_after_new_context(
    production, http_service
):
    item_id = production[5]
    events = []
    from tests.integration.test_traceability_postgres import final_inspection, normal_production

    fixture = (*production[:4], events.append, *production[5:])
    normal_production(fixture)
    final_inspection(fixture, defect=True)
    events[-1]["payload"]["defects"][0].update(component_id="insert-001", region_id="face-A")
    events[-1]["payload"]["defects"].append(
        {
            "type": "surface-mark",
            "defect_ref": "other-mark",
            "component_id": "insert-001",
            "region_id": "face-B",
        }
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(http_service, shared=True)),
        base_url="http://test",
    ) as client:
        for event in events:
            await source(client, event)
        item = await get(client, f"/api/v2/items/{item_id}")
        before = await get(client, f"/api/v2/bases/{item['basis_id']}")
        signs = [
            value for value in before["analysis_snapshot"]["observations"] if value["defect_type"]
        ]
        assert {value["defect_index"] for value in signs} == {0, 1}
        assert {value["region_id"] for value in signs} == {"face-A", "face-B"}
        assert len({value["record_id"] for value in signs}) == 1
        assert len(item["nonconformities"]) == 2
        raw = next(
            value for value in before["inputs"] if value["record_id"] == signs[0]["record_id"]
        )
        assert set(raw["observation_ids"]) == {value["observation_id"] for value in signs}
        http_service.journal.import_context(
            item_id,
            replace(production[7], route_version="2"),
            import_id=str(uuid4()),
            actor_id="test",
        )
        after_item = await get(client, f"/api/v2/items/{item_id}")
        after = await get(client, f"/api/v2/bases/{after_item['basis_id']}")
        assert after["pinned_versions"]["route"] == "2"
        assert before["pinned_versions"]["route"] != "2"
        assert await get(client, f"/api/v2/bases/{before['basis_id']}") == before


async def test_shared_release_error_has_typed_blockers_and_no_effect(production, http_service):
    item_id = production[5]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(http_service, shared=True)),
        base_url="http://test",
    ) as client:
        prepared = await command(client, item_id)
        before = http_service.quality.state(item_id)
        error = await post(
            client,
            f"/api/v2/items/{item_id}/dispositions",
            prepared | {"disposition": "released"},
            expected=409,
        )
        validate("ErrorResponse", error)
        assert error["error"]["details"] == []
        assert error["error"]["blockers"]
        assert http_service.quality.state(item_id) == before
        response = await client.post(
            f"/api/v2/items/{item_id}/dispositions",
            json=prepared | {"disposition": "released", "actor_id": "forged"},
            headers=auth(),
        )
        assert response.status_code == 422
