"""Shared OpenAPI routes backed by the single P4 transaction coordinator."""

import copy
from datetime import UTC, datetime

from fastapi import Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from app.adapters.json_events import JsonEventCodec
from app.adapters.read_pages import ReadPages
from app.adapters.shared_contract import document, validate
from app.adapters.shared_views import SharedViews
from app.api.pagination import limit
from app.api.server import _body, _token
from app.application.outbox import OutboxDeliveryWorker
from app.application.source_validation import validate_source_payload
from app.domain.errors import InvalidEvent, UnknownEntity

READ_ROLES = {"controller", "master", "technologist", "manager", "admin"}
ROLES = {
    "ingestEvent": {"source"},
    "decideNonconformity": {"controller"},
    "resolveNonconformity": {"controller"},
    "decideDisposition": {"controller"},
    "startControlCycle": {"master"},
    "recoverControl": {"controller"},
    "resolveScope": {"controller"},
    "getSession": READ_ROLES | {"integration", "source"},
    "getDeliveries": {"integration", "admin"},
    "retryDelivery": {"integration", "admin"},
}


def install(app, service):
    spec = document()
    views = SharedViews(service)
    pages = ReadPages(views)
    app.openapi = lambda: copy.deepcopy(spec)
    app.title, app.version = spec["info"]["title"], spec["info"]["version"]

    def dispatch(operation, request, token, body, raw):
        ids = request.path_params
        quality = service.quality
        if operation == "getSession":
            principal = service.identities.authenticate(token)
            return {"actor_id": principal.actor_id, "roles": sorted(principal.roles)}, 200
        if operation == "listScopeConstraints":
            return views.scope_review(), 200
        if operation == "resolveScope":
            return service.resolve_scope(token, ids["id"], **body), 200
        if operation == "ingestEvent":
            result = service.ingest(token, raw)
            return result, 200 if result.status == "duplicate" else 201
        methods = {
            "decideNonconformity": "decide_nonconformity",
            "decideDisposition": "dispose",
            "startControlCycle": "start_cycle",
            "recoverControl": "recover_control",
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
            elif operation == "resolveNonconformity" and command["action"] == "resolve":
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
            return pages.items(request.query_params.get("cursor", ""), limit(request)), 200
        if operation == "getNonconformity":
            item = views.item(
                quality.find_item_for_nonconformity(ids["id"]), target_ids={ids["id"]}
            )
            return next(card for card in item["nonconformities"] if card["id"] == ids["id"]), 200
        if operation == "listNonconformities":
            return pages.cards(request.query_params.get("cursor", ""), limit(request)), 200
        if operation == "getBasis":
            return views.basis(ids["basis_id"]), 200
        if operation == "getTimeline":
            return pages.timeline(
                ids["item_id"], request.query_params.get("cursor", ""), limit(request)
            ), 200
        if operation == "getAnalytics":
            dates = {}
            try:
                for name in ("since", "until"):
                    value = request.query_params.get(name)
                    dates[name] = datetime.fromisoformat(value) if value else None
                    if dates[name] is not None and dates[name].utcoffset() is None:
                        raise ValueError
                if dates["since"] and dates["until"] and dates["since"] >= dates["until"]:
                    raise ValueError
            except ValueError as error:
                raise InvalidEvent(
                    "Analytics requires an ordered timezone-aware interval."
                ) from error
            result = quality.analytics(request.query_params.getlist("item_id") or None, **dates)
            return {
                **{k: v for k, v in result.items() if k not in {"items", "kpi"}},
                "item_ids": result["item_ids"],
                "counts": result["kpi"],
                "computed_at": datetime.now(UTC).isoformat(),
            }, 200
        if operation == "getDeliveries":
            return pages.deliveries(request.query_params.get("cursor", ""), limit(request)), 200
        if operation == "retryDelivery":
            OutboxDeliveryWorker(service.journal, None).retry(ids["message_id"])
            return quality.get_outbox(ids["message_id"]), 200
        if operation == "listWorkOrders":
            return pages.orders(request.query_params.get("cursor", ""), limit(request)), 200
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
