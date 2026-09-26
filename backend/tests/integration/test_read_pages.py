import copy
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import event

from app.adapters.erp_orders import ErpContract, WorkOrderMapper
from app.adapters.read_pages import ReadPages
from app.adapters.shared_views import SharedViews
from app.api.server import create_app
from tests.erp_support import order_profile
from tests.integration.conftest import database as shared_database
from tests.integration.test_api_postgres import auth, get, seed, source

pytestmark = [pytest.mark.integration, pytest.mark.anyio]
pytest_plugins = ("tests.integration.test_api_postgres",)


@pytest.fixture
def database():
    # List every page only within this test's key domain. Other tests intentionally
    # leave records encrypted with different per-test keys in their session schema.
    yield from shared_database.__wrapped__()


async def test_pages_are_sql_bounded_and_do_not_call_full_scans(
    production, http_service, monkeypatch
):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(http_service, shared=True)),
        base_url="http://test",
    ) as client:
        await seed(client, production, defect=True)
        for _ in range(2):
            new_id = "paged-" + uuid4().hex
            http_service.journal.import_context(
                new_id,
                replace(production[7], external_item_id=new_id),
                import_id=str(uuid4()),
                actor_id="test",
            )

        def forbidden(*args, **kwargs):
            raise AssertionError("Full scan reached by a paged endpoint")

        monkeypatch.setattr(http_service.quality, "list_states", forbidden)
        monkeypatch.setattr(http_service.quality, "list_outbox", forbidden)
        monkeypatch.setattr(SharedViews, "orders", forbidden)
        monkeypatch.setattr(http_service.journal, "get_history", forbidden)
        statements = []

        def capture(conn, cursor, sql, params, context, many):
            statements.append(sql)

        event.listen(production[1], "before_cursor_execute", capture)
        try:
            for path, key, role in [
                ("/api/v2/items", "item_id", "controller"),
                ("/api/v2/nonconformities", "id", "controller"),
                ("/api/v2/integrations/deliveries", "message_id", "integration"),
                (f"/api/v2/items/{production[5]}/timeline", "record_id", "controller"),
            ]:
                seen, cursor = [], ""
                while True:
                    statements.clear()
                    response = await client.get(
                        path, params={"limit": 1, "cursor": cursor}, headers=auth(role)
                    )
                    assert response.status_code == 200, response.text
                    page = response.json()
                    assert len(page["items"]) <= 1
                    assert any("LIMIT" in sql.upper() for sql in statements)
                    seen.extend(row[key] for row in page["items"])
                    cursor = page["next_cursor"]
                    if cursor is None:
                        break
                assert seen and len(seen) == len(set(seen))
        finally:
            event.remove(production[1], "before_cursor_execute", capture)


async def test_sql_timeline_rejects_late_revision_and_foreign_cursor(production, http_service):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(http_service, shared=True)),
        base_url="http://test",
    ) as client:
        events = await seed(client, production)
        path = f"/api/v2/items/{production[5]}/timeline"
        first = await get(client, path + "?limit=2")
        before = await get(client, path)
        ordered = [
            (r["occurred_at"], r["source_id"], r["event_id"], r["record_id"])
            for r in before["items"]
        ]
        assert ordered == sorted(ordered)
        late = copy.deepcopy(events[-1])
        late.update(event_id="page-late", occurred_at="2026-09-25T10:10:30Z")
        await source(client, late)
        response = await client.get(path, params={"cursor": first["next_cursor"]}, headers=auth())
        assert response.status_code == 409
        response = await client.get(
            "/api/v2/items/foreign/timeline",
            params={"cursor": first["next_cursor"]},
            headers=auth(),
        )
        assert response.status_code == 422


async def test_order_pages_choose_latest_revision_without_duplicates(production, http_service):
    contract = ErpContract(Path(__file__).resolve().parents[3] / "contracts/external-openapi.yaml")
    expected = {}
    for _ in range(3):
        order, profile = order_profile()
        mapper = WorkOrderMapper(profile, contract)
        http_service.journal.import_work_order(order, mapper, actor_id="test")
        order = copy.deepcopy(order)
        order.update(message_id=str(uuid4()), revision=2)
        http_service.journal.import_work_order(order, mapper, actor_id="test")
        expected[order["external_order_id"]] = 2
    pages = ReadPages(SharedViews(http_service))
    cursor, seen = "", {}
    while True:
        page = pages.orders(cursor, 1)
        assert len(page["items"]) <= 1
        for order in page["items"]:
            assert order["id"] not in seen
            seen[order["id"]] = order
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert {
        row["external_order_id"]: row["revision"]
        for row in seen.values()
        if row["external_order_id"] in expected
    } == expected
