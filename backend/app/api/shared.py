"""Shared OpenAPI routes backed by the single P4 transaction coordinator."""

import copy
from datetime import UTC, datetime

from fastapi import Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from app.adapters.json_events import JsonEventCodec
from app.adapters.shared_contract import document, validate
from app.adapters.shared_views import SharedViews
from app.api.server import _body, _page, _token
from app.application.outbox import OutboxDeliveryWorker
from app.application.source_validation import validate_source_payload
from app.domain.errors import UnknownEntity

READ_ROLES = {"controller", "master", "technologist", "manager", "admin"}
ROLES = {
    "ingestEvent": {"source"},
    "decideNonconformity": {"controller"},
    "resolveNonconformity": {"controller"},
    "decideDisposition": {"controller"},
    "startControlCycle": {"master"},
    "getDeliveries": {"integration", "admin"},
    "retryDelivery": {"integration", "admin"},
}


def install(app, service):
    spec = document()
    views = SharedViews(service)
    app.openapi = lambda: copy.deepcopy(spec)
    app.title, app.version = spec["info"]["title"], spec["info"]["version"]

    def dispatch(operation, request, token, body, raw):
        ids = request.path_params
        quality = service.quality
        if operation == "ingestEvent":
            result = service.ingest(token, raw)
            return result, 200 if result.status == "duplicate" else 201
        methods = {
            "decideNonconformity": "decide_nonconformity",
            "decideDisposition": "dispose",
            "startControlCycle": "start_cycle",
        }
        if operation in methods or operation == "resolveNonconformity":
            command = dict(body)
            if operation == "decideNonconformity":
                command["action"] = command.pop("decision")
                cause = command.pop("cause", None)
                if cause:
                    command.update(
                        cause_status=cause["status"],
                        cause_category=cause["category"],
                        cause_evidence_record_ids=cause["record_ids"],
                    )
                    for name in ("operation_run_id", "operator_id", "action_record_id"):
                        command[name] = cause.get(name)
            elif operation == "decideDisposition":
                command["action"] = command.pop("disposition")
            elif operation == "startControlCycle":
                command["operation_run_ids"] = command.pop("planned_operation_run_ids")
            elif command["action"] == "resolve":
                command["rework_operation_id"] = command.pop("rework_operation_run_id")
            method = methods.get(operation) or (
                "resolve_nonconformity" if body["action"] == "resolve" else "reopen_nonconformity"
            )
            result = service.command(
                token, method, ids.get("item_id", ids.get("id")), command, document=body
            )
            return views.command_result(result), 200 if result.duplicate else 201
        if operation == "getItem":
            return views.item(ids["item_id"]), 200
        if operation == "listItems":
            return _page(
                request,
                [views.item(state["item_id"]) for state in quality.list_states()],
                "item_id",
            ), 200
        if operation == "getNonconformity":
            item = views.item(quality.find_item_for_nonconformity(ids["id"]))
            return next(card for card in item["nonconformities"] if card["id"] == ids["id"]), 200
        if operation == "listNonconformities":
            cards = [
                card
                for state in quality.list_states()
                for card in views.item(state["item_id"])["nonconformities"]
            ]
            return _page(request, cards, "id"), 200
        if operation == "getBasis":
            return views.basis(ids["basis_id"]), 200
        if operation == "getTimeline":
            quality.state(ids["item_id"])
            history = service.journal.get_history(ids["item_id"])
            return _page(request, history["timeline"] if history else [], "record_id"), 200
        if operation == "getAnalytics":
            result = quality.analytics(request.query_params.getlist("item_id") or None)
            return {
                "item_ids": result["item_ids"],
                "counts": result["kpi"],
                "computed_at": datetime.now(UTC).isoformat(),
            }, 200
        if operation == "getDeliveries":
            return _page(request, quality.list_outbox(), "message_id"), 200
        if operation == "retryDelivery":
            OutboxDeliveryWorker(service.journal, None).retry(ids["message_id"])
            return quality.get_outbox(ids["message_id"]), 200
        if operation == "listWorkOrders":
            return _page(request, views.orders(), "id"), 200
        if operation == "getWorkOrder":
            order = next((part for part in views.orders() if part["id"] == ids["id"]), None)
            if order is None:
                raise UnknownEntity("Unknown imported work order.")
            return order, 200
        if operation == "getLineStatus":
            histories = [
                service.journal.get_history(state["item_id"]) for state in quality.list_states()
            ]
            entries = [
                {**entry, "item_id": history["item_id"]}
                for history in histories
                if history
                for entry in history["timeline"]
                if entry.get("line_id") == ids["id"]
            ]
            active = [
                op["operation_run_id"]
                for history in histories
                if history
                for op in history["operations"]
                if op.get("line_id") == ids["id"] and op["status"] == "in_progress"
            ]
            return {
                "line_id": ids["id"],
                "computed_at": datetime.now(UTC).isoformat(),
                "source_last_received_at": max(
                    (entry["received_at"] for entry in entries), default=None
                ),
                "unresolved_items": len(
                    {
                        entry["item_id"]
                        for entry in entries
                        if entry["projection_status"] == "unresolved"
                    }
                ),
                "active_operation_run_ids": active,
            }, 200
        raise UnknownEntity("Unknown shared operation.")

    def endpoint(operation):
        name = operation["operationId"]

        async def handle(request: Request):
            token = _token(request)
            service._require(token, ROLES.get(name, READ_ROLES))
            for value in request.path_params.values():
                validate("Id", value)
            raw, body = b"", {}
            if request.method == "POST":
                raw, body = await _body(request)
                if name == "ingestEvent":
                    JsonEventCodec().decode(raw)
                    validate_source_payload(body)
                schema = operation["requestBody"]["content"]["application/json"]["schema"][
                    "$ref"
                ].rsplit("/", 1)[-1]
                validate(schema, body)
            result, status = await run_in_threadpool(dispatch, name, request, token, body, raw)
            result = jsonable_encoder(result)
            schema = operation["responses"][str(status)]["content"]["application/json"]["schema"][
                "$ref"
            ].rsplit("/", 1)[-1]
            validate(schema, result, response=True)
            return JSONResponse(result, status_code=status)

        return handle

    for path, methods in spec["paths"].items():
        for method, operation in methods.items():
            if method in {"get", "post"}:
                app.add_api_route(
                    path,
                    endpoint(operation),
                    methods=[method.upper()],
                    operation_id=operation["operationId"],
                )
