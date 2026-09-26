"""FastAPI boundary that joins the published role 2 contract to role 1's services."""

from __future__ import annotations

import hmac
import json
import os
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from jsonschema.exceptions import ValidationError

from app.domain import errors
from app.domain.evidence_graph import TraversalLimitExceeded


def create_app(chain, credentials):
    """Credentials map external tokens to {actor_id, role, source_id?}; no default secrets."""
    from contracts.validation import document, strict_json, validate

    app = FastAPI(title="EFQA unified demonstrator", version="0.4.0")
    app.openapi = lambda: document()

    def error(status, code, message, blockers=None):
        return JSONResponse(
            status_code=status,
            content={
                "request_id": str(uuid4()),
                "error": {
                    "code": code,
                    "message": message,
                    "details": [],
                    "blockers": list(blockers or []),
                },
            },
        )

    @app.middleware("http")
    async def authenticate(request, call_next):
        if request.url.path in {"/health/live", "/docs", "/openapi.json", "/redoc"}:
            return await call_next(request)
        token = request.headers.get("authorization", "")
        principal = next(
            (
                value
                for key, value in credentials.items()
                if hmac.compare_digest(f"Bearer {key}", token)
            ),
            None,
        )
        if not principal:
            return error(401, "unauthorized", "Authentication required")
        request.state.principal = principal
        try:
            return await call_next(request)
        except Exception:
            # Do not leak database URLs, tokens or decrypted payloads in a public error.
            return error(503, "processing_unavailable", "Command could not be completed")

    @app.exception_handler(Exception)
    async def domain_error(_, exc):
        mapping = {
            errors.StaleRevision: (409, "stale_revision"),
            errors.BasisMismatch: (409, "basis_mismatch"),
            errors.IdempotencyConflict: (409, "idempotency_conflict"),
            errors.EventConflict: (409, "conflict"),
            errors.InvalidTransition: (409, "invalid_transition"),
            errors.ReleaseBlocked: (409, "release_blocked"),
            errors.SourceMismatch: (403, "source_mismatch"),
            errors.UnknownEntity: (404, "not_found"),
            errors.AckMismatch: (409, "ack_mismatch"),
            errors.InvalidEvent: (422, "validation_error"),
            ValidationError: (422, "validation_error"),
            ValueError: (422, "validation_error"),
            TraversalLimitExceeded: (503, "impact_limit_exceeded"),
        }
        status, code = next(
            (value for kind, value in mapping.items() if isinstance(exc, kind)),
            (503, "processing_unavailable"),
        )
        return error(status, code, code, getattr(exc, "blockers", []))

    for exception_type in (
        errors.QualityError,
        ValidationError,
        ValueError,
        TraversalLimitExceeded,
    ):
        app.add_exception_handler(exception_type, domain_error)

    @app.exception_handler(HTTPException)
    async def http_error(_, exc):
        return error(
            exc.status_code,
            "forbidden" if exc.status_code == 403 else "http_error",
            "Request rejected",
        )

    def require(request, roles):
        actor = request.state.principal
        if actor["role"] not in roles:
            raise HTTPException(403, detail="Role not permitted")
        return actor

    async def body(request, schema, external=False):
        raw = await request.body()
        if not 0 < len(raw) <= 1048576:
            raise ValueError("Body must be within limit")
        value = strict_json(raw)
        validate(schema, value, external=external)
        return value

    @app.get("/health/live")
    def health():
        return {"status": "ok", "prototype": True}

    @app.post("/api/v2/imports/work-orders")
    async def import_order(request: Request):
        require(request, {"integrator"})
        return chain.import_order(await body(request, "WorkOrder", external=True))

    @app.post("/api/v2/composition")
    async def composition(request: Request):
        require(request, {"master", "integrator"})
        return chain.set_composition(await body(request, "CompositionSnapshot"))

    @app.post("/api/v2/events")
    async def ingest(request: Request):
        actor = require(request, {"source"})
        raw = await request.body()
        receipt = chain.ingest(raw, actor["source_id"])
        return JSONResponse(
            status_code=200 if receipt["status"] == "duplicate" else 201, content=receipt
        )

    @app.get("/api/v2/events/{record_id}")
    def get_raw(record_id: str, request: Request):
        require(request, {"controller", "master", "integrator", "reader"})
        return chain.raw(record_id)

    @app.get("/api/v2/items")
    def items(request: Request):
        require(request, {"controller", "master", "integrator", "reader"})
        return chain.list_items()

    @app.get("/api/v2/work-orders")
    def orders(request: Request):
        require(request, {"controller", "master", "integrator", "reader"})
        return chain.execute(
            lambda state, _, __: {
                "items": [
                    {"id": value["external_order_id"], "external_system": "erp-demo", **value}
                    for value in state["orders"].values()
                ],
                "next_cursor": None,
            }
        )

    @app.get("/api/v2/work-orders/{order_id}")
    def order(order_id: str, request: Request):
        require(request, {"controller", "master", "integrator", "reader"})

        def read(state, _, __):
            if order_id not in state["orders"]:
                raise errors.UnknownEntity("Unknown order")
            return {"id": order_id, "external_system": "erp-demo", **state["orders"][order_id]}

        return chain.execute(read)

    @app.get("/api/v2/nonconformities")
    def cards(request: Request):
        require(request, {"controller", "master", "integrator", "reader"})
        return {
            "items": [
                card for item in chain.list_items()["items"] for card in item["nonconformities"]
            ],
            "next_cursor": None,
        }

    @app.get("/api/v2/lines/{line_id}/status")
    def line(line_id: str, request: Request):
        require(request, {"controller", "master", "integrator", "reader"})

        def read(state, _, __):
            records = [entry for entry in state["refs"].values() if entry.get("line_id") == line_id]
            started = {
                x["operation_run_id"] for x in records if x["event_type"] == "operation_started"
            }
            finished = {
                x["operation_run_id"] for x in records if x["event_type"] == "operation_finished"
            }
            return {
                "line_id": line_id,
                "computed_at": datetime.now(UTC).isoformat(),
                "source_last_received_at": max((x["received_at"] for x in records), default=None),
                "unresolved_items": len(
                    {x["record_id"] for x in records if x["projection_status"] == "unresolved"}
                ),
                "active_operation_run_ids": sorted(started - finished),
            }

        from datetime import UTC, datetime

        return chain.execute(read)

    @app.get("/api/v2/items/{item_id}")
    def item(item_id: str, request: Request):
        require(request, {"controller", "master", "integrator", "reader"})
        return chain.item(item_id)

    @app.get("/api/v2/items/{item_id}/timeline")
    def timeline(item_id: str, request: Request):
        require(request, {"controller", "master", "integrator", "reader"})
        return chain.timeline(item_id)

    @app.get("/api/v2/bases/{basis_id}")
    def basis(basis_id: str, request: Request):
        require(request, {"controller", "master", "integrator", "reader"})
        return chain.basis(basis_id)

    @app.get("/api/v2/nonconformities/{card_id}")
    def card(card_id: str, request: Request):
        require(request, {"controller", "master", "integrator", "reader"})
        found = next(
            (
                card
                for item in chain.list_items()["items"]
                for card in item["nonconformities"]
                if card["id"] == card_id
            ),
            None,
        )
        if not found:
            raise errors.UnknownEntity("Unknown card")
        return found

    async def command(kind, target, request, schema, roles):
        actor = require(request, roles)
        status, result = chain.command(kind, target, await body(request, schema), actor["actor_id"])
        return JSONResponse(status_code=status, content=result)

    @app.post("/api/v2/nonconformities/{card_id}/decisions")
    async def decide(card_id: str, request: Request):
        return await command("decision", card_id, request, "ReviewCommand", {"controller"})

    @app.post("/api/v2/nonconformities/{card_id}/resolutions")
    async def resolve(card_id: str, request: Request):
        return await command("resolution", card_id, request, "ResolutionCommand", {"controller"})

    @app.post("/api/v2/items/{item_id}/dispositions")
    async def disposition(item_id: str, request: Request):
        return await command("disposition", item_id, request, "DispositionCommand", {"controller"})

    @app.post("/api/v2/items/{item_id}/control-cycles")
    async def cycle(item_id: str, request: Request):
        return await command("cycle", item_id, request, "CycleCommand", {"master"})

    @app.get("/api/v2/analytics")
    def analytics(request: Request):
        require(request, {"controller", "master", "integrator", "reader"})
        return chain.analytics(request.query_params.getlist("item_id") or None)

    @app.get("/api/v2/integrations/deliveries")
    def deliveries(request: Request):
        require(request, {"controller", "master", "integrator", "reader"})
        return chain.deliveries()

    @app.post("/api/v2/integrations/flush")
    def flush(request: Request):
        require(request, {"integrator"})
        from app.application.delivery import deliver

        return deliver(chain, os.environ["ERP_URL"], os.environ["ERP_EMULATOR_TOKEN"])

    return app


def configured_app():
    from sqlalchemy import create_engine

    from app.adapters.protection import AesGcmProtector
    from app.application.unified import UnifiedChain

    # Configuration paths, not literal secrets; no credentials are included in git.
    config = json.loads(Path(os.environ["EFQA_CREDENTIALS_FILE"]).read_text())
    key = bytes.fromhex(Path(os.environ["EFQA_KEY_FILE"]).read_text().strip())
    engine = create_engine(os.environ["DATABASE_URL"], hide_parameters=True)
    protector = AesGcmProtector({("prototype", 1): key}, active_key=("prototype", 1))
    return create_app(UnifiedChain(engine, protector), config)
