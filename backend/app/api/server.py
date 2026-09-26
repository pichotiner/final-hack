"""A single draft contract drives validation, routing, RBAC and published OpenAPI."""

import copy
import json
import os
from contextlib import asynccontextmanager
from functools import cache, lru_cache
from pathlib import Path
from uuid import uuid4

import rfc8785
from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from jsonschema import Draft202012Validator
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool

from app.adapters.json_events import JsonEventCodec, _invalid_constant, _object
from app.application.bootstrap import ProductionSettings, build_runtime
from app.application.outbox import OutboxDeliveryWorker
from app.application.source_validation import validate_source_payload
from app.domain.control import decode_context
from app.domain.errors import InvalidEvent, QualityError, ReleaseBlocked, UnknownEntity


@lru_cache(maxsize=1)
def contract():
    return json.loads(Path(__file__).with_name("contract.json").read_text(encoding="utf-8"))


@cache
def _validator(name):
    return Draft202012Validator(
        {
            "$ref": f"#/components/schemas/{name}",
            "components": contract()["components"],
        }
    )


def _validate(name, body):
    if next(_validator(name).iter_errors(body), None) is not None:
        # Validator messages can contain raw input. Never echo them across the boundary.
        raise InvalidEvent("The request does not match the operation schema.")


def _token(request):
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    return token if scheme.lower() == "bearer" else ""


async def _body(request):
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 1_048_576:
            raise InvalidEvent("The request exceeds the size limit.")
    try:
        body = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_object, parse_constant=_invalid_constant
        )
        rfc8785.dumps(body)
    except (ValueError, UnicodeError, RecursionError) as error:
        raise InvalidEvent("A valid UTF-8 JSON object is required.") from error
    if not isinstance(body, dict):
        raise InvalidEvent("A JSON object is required.")
    return bytes(raw), body


def _page(request, values, key):
    try:
        limit = int(request.query_params.get("limit", "50"))
        if not 1 <= limit <= 200:
            raise ValueError
    except ValueError as error:
        raise InvalidEvent("limit must be between 1 and 200.") from error
    cursor = request.query_params.get("cursor", "")
    values = sorted((value for value in values if value[key] > cursor), key=lambda v: v[key])
    return {
        "items": values[:limit],
        "next_cursor": values[limit - 1][key] if len(values) > limit else None,
    }


def _dispatch(service, operation, request, body, raw, token):
    ids = request.path_params
    quality = service.quality
    if operation == "ingest":
        result = service.ingest(token, raw)
        return result, 200 if result.status == "duplicate" else 201
    if operation == "import_context":
        try:
            context = decode_context(body["context"])
        except ValueError as error:
            raise InvalidEvent("Inconsistent production context.") from error
        result = service.import_context(
            token, body["item_id"], context, import_id=body["import_id"]
        )
        return result, 200 if result["status"] == "duplicate" else 201
    if operation == "create_basis":
        return service.create_basis(token, ids["item_id"], body.get("target_id")), 201
    if operation == "basis":
        item_id = quality.find_basis_item(ids["basis_id"])
        return quality.get_basis(item_id, ids["basis_id"]), 200
    if operation in {"decide_nonconformity", "dispose", "resolution", "start_cycle"}:
        method = operation
        if operation == "resolution":
            method = (
                "resolve_nonconformity" if body["action"] == "resolve" else "reopen_nonconformity"
            )
        target = ids.get("item_id", ids.get("nonconformity_id"))
        result = service.command(token, method, target, body)
        return result, 200 if getattr(result, "duplicate", False) else 201
    if operation == "item":
        return service.state(token, ids["item_id"]), 200
    if operation == "items":
        return _page(request, quality.list_states(), "item_id"), 200
    if operation == "timeline":
        state = service.state(token, ids["item_id"])
        return {
            "item_id": ids["item_id"],
            "context_revision": state["context_revision"],
            "history": service.journal.get_history(ids["item_id"]),
            "decisions": state["decisions"],
            "dispositions": state["dispositions"],
        }, 200
    if operation == "nonconformities":
        values = []
        for state in quality.list_states():
            values.extend(
                {**nc, "context_revision": state["context_revision"]}
                for nc in state["nonconformities"]
            )
        return _page(request, values, "nonconformity_id"), 200
    if operation == "nonconformity":
        item_id = quality.find_item_for_nonconformity(ids["nonconformity_id"])
        state = service.state(token, item_id)
        nc = next(
            v for v in state["nonconformities"] if v["nonconformity_id"] == ids["nonconformity_id"]
        )
        return {
            **nc,
            "context_revision": state["context_revision"],
            "control_cycle_id": state["cycle_id"],
            "blockers": state["blockers"],
            "observations": state["observations"],
            "decisions": state["decisions"],
        }, 200
    if operation == "analytics":
        return quality.analytics(), 200
    if operation == "deliveries":
        return _page(request, quality.list_outbox(), "message_id"), 200
    if operation == "retry_delivery":
        return OutboxDeliveryWorker(service.journal, None).retry(ids["message_id"]), 200
    raise UnknownEntity("Unknown operation.")


def _openapi():
    document = copy.deepcopy(contract())
    for path, methods in document["paths"].items():
        for method, operation in methods.items():
            operation["responses"] = {
                code: {
                    "description": description,
                    "content": {
                        "application/json": {
                            "schema": {"type": "object"},
                        }
                    },
                }
                for code, description in {
                    "200": "Read or exact retry",
                    "201": "Committed",
                    "401": "Authentication required",
                    "403": "Forbidden",
                    "404": "Not found",
                    "409": "Context/command conflict",
                    "422": "Invalid request",
                    "503": "Storage unavailable",
                }.items()
            }
            operation["parameters"] = [
                {
                    "name": part[1:-1],
                    "in": "path",
                    "required": True,
                    "schema": {"$ref": "#/components/schemas/Id"},
                }
                for part in path.split("/")
                if part.startswith("{")
            ]
            if operation["operationId"] in {"items", "nonconformities", "deliveries"}:
                operation["parameters"] += [
                    {
                        "name": "limit",
                        "in": "query",
                        "schema": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50},
                    },
                    {"name": "cursor", "in": "query", "schema": {"type": "string"}},
                ]
            if method == "post":
                operation["requestBody"] = {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {"$ref": f"#/components/schemas/{operation['x-schema']}"},
                        }
                    },
                }
    return document


def create_app(service=None):
    runtime = None
    if service is None:
        runtime = build_runtime(ProductionSettings.from_environment(os.environ))
        service = runtime.service

    @asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            if runtime is not None:
                runtime.close()

    app = FastAPI(
        title=contract()["info"]["title"], version=contract()["info"]["version"], lifespan=lifespan
    )
    app.state.production_service = service
    app.openapi = _openapi

    @app.exception_handler(QualityError)
    async def domain_error(request, error):
        status = {
            "authentication_required": 401,
            "permission_denied": 403,
            "source_mismatch": 403,
            "unknown_entity": 404,
            "stale_revision": 409,
            "basis_mismatch": 409,
            "invalid_transition": 409,
            "idempotency_conflict": 409,
            "event_conflict": 409,
            "release_blocked": 409,
            "ack_mismatch": 409,
            "integrity_violation": 503,
            "key_unavailable": 503,
            "unsupported_crypto_profile": 503,
        }.get(error.code, 422)
        details = [{"blockers": list(error.blockers)}] if isinstance(error, ReleaseBlocked) else []
        return JSONResponse(
            {
                "error": {"code": error.code, "message": str(error), "details": details},
                "request_id": str(uuid4()),
            },
            status_code=status,
            headers={"WWW-Authenticate": "Bearer"} if status == 401 else None,
        )

    @app.exception_handler(SQLAlchemyError)
    async def storage_error(request, error):
        return JSONResponse(
            {
                "error": {
                    "code": "storage_unavailable",
                    "message": "Storage is unavailable.",
                    "details": [],
                },
                "request_id": str(uuid4()),
            },
            status_code=503,
        )

    @app.get("/health/live", include_in_schema=False)
    def live():
        return {"status": "ok"}

    @app.get("/health/ready", include_in_schema=False)
    def ready():
        with service.journal.engine.connect() as connection:
            connection.execute(text("SELECT 1 FROM quality_deliveries LIMIT 1"))
        return {"status": "ready"}

    def endpoint(operation):
        async def handle(request: Request):
            token = _token(request)
            service._require(token, set(operation["x-roles"]))
            raw, body = b"", {}
            if request.method == "POST":
                raw, body = await _body(request)
                if operation["operationId"] == "ingest":
                    JsonEventCodec().decode(raw)  # Preserve specialized envelope error codes.
                    validate_source_payload(body)
                _validate(operation["x-schema"], body)
            for value in request.path_params.values():
                _validate("Id", value)
            result, status = await run_in_threadpool(
                _dispatch, service, operation["operationId"], request, body, raw, token
            )
            return JSONResponse(jsonable_encoder(result), status_code=status)

        return handle

    for path, methods in contract()["paths"].items():
        for method, operation in methods.items():
            app.add_api_route(
                path,
                endpoint(operation),
                methods=[method.upper()],
                operation_id=operation["operationId"],
            )
    return app
