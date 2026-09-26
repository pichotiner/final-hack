import copy
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select

from app.adapters.locks import production_guard
from app.adapters.postgres import quality_items, quality_state_versions
from app.adapters.shared_views import SharedViews
from app.api.server import create_app
from app.application.outbox import OutboxDeliveryWorker
from tests.integration.test_api_postgres import auth, get, post, seed, source
from tests.integration.test_outbox_delivery import ack
from tests.integration.test_shared_api import command

pytestmark = [pytest.mark.integration, pytest.mark.anyio]
pytest_plugins = ("tests.integration.test_api_postgres",)


async def test_shared_recovery_poor_unbound_retry_stale_and_immutable_basis(
    production, http_service
):
    item_id = production[5]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(http_service, shared=True)),
        base_url="http://test",
    ) as client:
        events = await seed(client, production)
        poor = copy.deepcopy(events[-1])
        poor.update(event_id="poor", occurred_at="2026-09-25T10:12:00Z")
        poor["payload"]["observation_quality"] = "poor"
        poor["payload"]["control_session_id"] = "unregistered"
        record = (await source(client, poor))["record_id"]
        prepared = await command(client, item_id)
        old = await get(client, f"/api/v2/bases/{prepared['expected_basis_id']}")
        path = f"/api/v2/items/{item_id}/control-recoveries"
        body = prepared | {
            "action": "bind_control",
            "record_id": record,
            "control_cycle_id": "cycle-1",
        }
        await post(client, path, body, "manager", expected=403)
        result = await post(client, path, body)
        assert await post(client, path, body, expected=200) == result
        await post(client, path, body | {"idempotency_key": str(uuid4())}, expected=409)
        bound = await get(client, f"/api/v2/items/{item_id}")
        assert "observation_cycle_unresolved" not in bound["blockers"]
        assert "not_assessable:final" in bound["blockers"]
        good = copy.deepcopy(events[-1])
        good.update(event_id="repeat", occurred_at="2026-09-25T10:13:00Z")
        replacement = (await source(client, good))["record_id"]
        prepared = await command(client, item_id)
        body = prepared | {
            "action": "replace_control",
            "record_id": record,
            "replacement_record_id": replacement,
        }
        await post(client, path, body | {"replacement_record_id": str(uuid4())}, expected=409)
        await post(client, path, body)
        assert (await get(client, f"/api/v2/items/{item_id}"))["release_allowed_now"]
        assert await get(client, f"/api/v2/bases/{old['basis_id']}") == old
        await post(
            client,
            f"/api/v2/items/{item_id}/dispositions",
            (await command(client, item_id)) | {"disposition": "released"},
        )


async def test_sending_read_lease_expiry_ack_and_no_state_growth(production, http_service):
    _, writer, journal, _, _, item_id, _, _ = production
    clock = [datetime.now(UTC)]
    worker = OutboxDeliveryWorker(journal, ack, clock=lambda: clock[0])
    with writer.connect() as connection:
        before = connection.scalar(select(func.count()).select_from(quality_state_versions))
    claimed = worker.claim_one(journal.quality.state(item_id)["outbox"][0]["message_id"])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(http_service, shared=True)),
        base_url="http://test",
    ) as client:
        entries, cursor = [], ""
        while True:
            response = await client.get(
                "/api/v2/integrations/deliveries",
                params={"cursor": cursor},
                headers=auth("integration"),
            )
            assert response.status_code == 200, response.text
            entries.extend(response.json()["items"])
            cursor = response.json()["next_cursor"]
            if cursor is None:
                break
        assert (
            next(row for row in entries if row["message_id"] == claimed["message_id"])["status"]
            == "sending"
        )
        assert worker.claim_one(claimed["message_id"]) is None
        clock[0] += timedelta(seconds=61)
        assert worker.deliver_once(claimed["message_id"])["status"] == "acknowledged"
    with writer.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(quality_state_versions)) == before
        state = connection.scalar(
            select(quality_items.c.state).where(quality_items.c.item_id == item_id)
        )
        assert state["storage_format"] == 2
        assert state["bases"] == state["outbox"] == {}
        assert state["decisions"] == []


async def test_reads_complete_while_writer_guard_is_held_and_timeline_is_paged(
    production, http_service
):
    writer, item_id = production[1], production[5]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(http_service, shared=True)),
        base_url="http://test",
    ) as client:
        await seed(client, production)
        entries, cursor = [], None
        while True:
            page = await get(
                client,
                f"/api/v2/items/{item_id}/timeline?limit=2"
                + (f"&cursor={cursor}" if cursor else ""),
            )
            entries.extend(page["items"])
            cursor = page["next_cursor"]
            if not cursor:
                break
        assert len({e["record_id"] for e in entries}) == len(entries)
        assert [e["occurred_at"] for e in entries] == sorted(e["occurred_at"] for e in entries)
    with ThreadPoolExecutor() as pool:
        with writer.begin() as connection:
            production_guard(connection)
            connection.execute(
                select(quality_items).where(quality_items.c.item_id == item_id).with_for_update()
            )
            future = pool.submit(SharedViews(http_service).item, item_id)
            value = future.result(timeout=5)
            assert value["item_id"] == item_id
