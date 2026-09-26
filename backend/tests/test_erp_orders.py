import copy
import json

import httpx
import pytest

from app.adapters.erp_orders import ErpContract, HttpWorkOrders, WorkOrderMapper
from app.domain.errors import InvalidEvent
from tests.erp_support import BoundaryStub, order_profile


def test_mapping_has_explicit_identity_and_pinned_route_plan_and_sessions():
    order, profile = order_profile()
    before = copy.deepcopy(order)
    mapped = WorkOrderMapper(profile, BoundaryStub()).map(order)
    first = mapped[profile["items"]["assembly-001"]["item_id"]]
    assert first.external_item_id == "assembly-001"
    assert first.external_order_id == order["external_order_id"]
    assert first.work_order_id == "order-001"
    assert (first.route_id, first.route_version, first.control_plan_version) == (
        "route-bracket",
        "1",
        "1",
    )
    assert first.component_ids == ("bracket-001", "insert-001")
    assert first.controls[0].control_session_id == "assembly-001-initial-incoming"
    assert first.controls[0].operation_run_id is None
    assert first.controls[-1].operation_run_id == "run-assembly-001"
    assert order == before


@pytest.mark.parametrize(
    "edit",
    [
        lambda order, profile: order.update(route_version="unknown"),
        lambda order, profile: order["item_ids"].append("unmapped"),
        lambda order, profile: order["components"]["assembly-001"].append("unknown-part"),
        lambda order, profile: order["components"].update(unmapped=[]),
        lambda order, profile: order["checkpoints"][0].update(repeat_after_rework=False),
        lambda order, profile: order["checkpoints"][0].update(method_version="unknown"),
        lambda order, profile: order["checkpoints"].pop(),
        lambda order, profile: profile["items"]["assembly-001"]["context"]["controls"][0].pop(
            "control_session_id"
        ),
        lambda order, profile: profile["items"]["assembly-002"].update(
            item_id=profile["items"]["assembly-001"]["item_id"]
        ),
    ],
)
def test_unknown_mapping_and_unsupported_carry_fail_closed(edit):
    order, profile = order_profile()
    edit(order, profile)
    with pytest.raises(InvalidEvent, match="pinned production profile"):
        WorkOrderMapper(profile, BoundaryStub()).map(order)


def test_http_pagination_uses_same_endpoint_and_bearer():
    order, _ = order_profile()
    cursors = []

    def handle(request):
        assert request.headers["authorization"] == "Bearer synthetic-token"
        assert request.url.path == "/external/v2/work-orders"
        cursors.append(request.url.params.get("cursor"))
        return httpx.Response(
            200, json={"items": [order], "next_cursor": "50" if len(cursors) == 1 else None}
        )

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        reader = HttpWorkOrders(
            "http://erp/external/v2/work-orders", "synthetic-token", BoundaryStub(), client=client
        )
        assert list(reader.orders()) == [order, order]
    assert cursors == [None, "50"]


@pytest.mark.parametrize(
    "body",
    [
        b"{",
        b'{"items":[],"items":[],"next_cursor":null}',
        b'{"items":[],"next_cursor":"https://other/secret"}',
        b" " * 1_048_577,
    ],
    ids=["malformed", "duplicate-key", "invalid-cursor", "oversized"],
)
def test_invalid_pages_do_not_echo_response_or_yield_orders(body):
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=body))
    ) as client:
        reader = HttpWorkOrders("http://erp/orders", "secret", BoundaryStub(), client=client)
        with pytest.raises(InvalidEvent) as error:
            list(reader.orders())
        assert "secret" not in str(error.value)


def test_repeated_pagination_is_an_error_not_silent_completion():
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"items": [], "next_cursor": "50"})
        )
    ) as client:
        reader = HttpWorkOrders("http://erp/orders", "secret", BoundaryStub(), client=client)
        with pytest.raises(InvalidEvent, match="advance"):
            list(reader.orders())


@pytest.mark.parametrize("status", [302, 401, 429, 503])
def test_http_failures_are_not_successful_imports(status):
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                status, text="private body", headers={"Location": "http://other"}
            )
        )
    ) as client:
        reader = HttpWorkOrders("http://erp/orders", "secret", BoundaryStub(), client=client)
        with pytest.raises(InvalidEvent, match=f"HTTP {status}") as error:
            list(reader.orders())
        assert "private" not in str(error.value)


def test_timeout_is_retryable_without_exposing_transport_credentials():
    def timeout(request):
        raise httpx.ReadTimeout("private transport diagnostic", request=request)

    with httpx.Client(transport=httpx.MockTransport(timeout)) as client:
        reader = HttpWorkOrders("http://erp/orders", "secret", BoundaryStub(), client=client)
        with pytest.raises(InvalidEvent, match="retry the import") as error:
            list(reader.orders())
        assert "private" not in str(error.value)


def test_contract_loader_obeys_supplied_schema_and_rejects_external_refs(tmp_path):
    # Artificial spec tests the loader, not claimed to be the role 2 contract.
    path = tmp_path / "external.yaml"
    spec = {
        "openapi": "3.1.0",
        "components": {
            "schemas": {
                "WorkOrder": {"type": "object", "required": ["sentinel"]},
                "WorkOrderPage": {"type": "object"},
            }
        },
    }
    path.write_text(json.dumps(spec), encoding="utf-8")
    contract = ErpContract(path)
    contract.validate("WorkOrder", {"sentinel": True})
    with pytest.raises(InvalidEvent):
        contract.validate("WorkOrder", {})
    spec["components"]["schemas"]["WorkOrder"] = {"$ref": "https://not-local/schema"}
    path.write_text(json.dumps(spec), encoding="utf-8")
    with pytest.raises(InvalidEvent, match="Cannot load"):
        ErpContract(path)
