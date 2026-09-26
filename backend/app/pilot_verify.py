"""Verify restored evidence and optionally reconcile every immutable ERP message."""

import argparse
import json
import os
from pathlib import Path

from sqlalchemy import select

from app.adapters.erp_http import HttpErpReceiver
from app.adapters.postgres import (
    _record,
    quality_bases,
    quality_decisions,
    quality_deliveries,
    quality_outbox,
    quality_state_versions,
    raw_events,
)
from app.adapters.quality import _encode
from app.adapters.shared_views import SharedViews
from app.application.bootstrap import ProductionSettings, build_runtime
from app.domain.errors import IntegrityViolation
from app.domain.quality import _digest


def verify(runtime, *, receiver=None):
    service = runtime.service
    with runtime.engine.connect().execution_options(
        isolation_level="REPEATABLE READ"
    ) as connection:
        records = list(
            connection.execute(select(raw_events).order_by(raw_events.c.record_id)).mappings()
        )
        bases = list(
            connection.execute(select(quality_bases).order_by(quality_bases.c.basis_id)).mappings()
        )
        decisions = list(
            connection.execute(
                select(quality_decisions).order_by(quality_decisions.c.decision_id)
            ).mappings()
        )
        messages = list(
            connection.execute(
                select(quality_outbox).order_by(
                    quality_outbox.c.item_id, quality_outbox.c.result_version
                )
            ).mappings()
        )
        versions = list(
            connection.execute(
                select(quality_state_versions).order_by(
                    quality_state_versions.c.item_id, quality_state_versions.c.storage_revision
                )
            ).mappings()
        )
        for row in records:
            service.journal._fact(_record(row))
        for row in messages:
            if _digest(row["payload"]) != row["content_digest"]:
                raise IntegrityViolation("Outbox digest mismatch.")
    views = SharedViews(service)
    for row in bases:
        views.basis(row["basis_id"])
    for state in service.quality.list_states():
        views.item(state["item_id"])
    redelivered = 0
    if receiver:
        for row in messages:
            receipt = receiver(row["payload"])
            with runtime.engine.begin() as connection:
                service.quality.acknowledge_in(connection, row["message_id"], receipt=receipt)
                connection.execute(
                    quality_deliveries.update()
                    .where(quality_deliveries.c.message_id == row["message_id"])
                    .values(
                        status="acknowledged",
                        lease_token=None,
                        lease_until=None,
                        last_error=None,
                        attempts=quality_deliveries.c.attempts + 1,
                    )
                )
            redelivered += 1
    # Fingerprints intentionally omit mutable lease/ACK fields. Pin outside the DB backup.
    fingerprints = {
        "raw": _digest(
            [{"id": str(row["record_id"]), "digest": row["content_digest"]} for row in records]
        ),
        "bases": _digest([row["snapshot"] for row in bases]),
        "decisions": _digest([row["decision"] for row in decisions]),
        "outbox": _digest([row["payload"] for row in messages]),
        "versions": _digest([row["snapshot"] for row in versions]),
    }
    return {
        "verified": True,
        "counts": dict(
            zip(
                fingerprints, map(len, (records, bases, decisions, messages, versions)), strict=True
            )
        ),
        "fingerprints": fingerprints,
        "redelivered": redelivered,
        "key_versions": sorted({row["key_version"] for row in records}),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--redeliver", action="store_true")
    args = parser.parse_args()
    runtime = build_runtime(ProductionSettings.from_environment(os.environ))
    receiver = None
    try:
        if args.redeliver:
            receiver = HttpErpReceiver(
                os.environ.get("QC_ERP_RESULTS_URL", "http://erp:8090/external/v2/quality-results"),
                Path("/run/credentials/erp.token").read_text().strip(),
                verify=os.environ.get("QC_ERP_CA_FILE", True),
            )
        print(json.dumps(_encode(verify(runtime, receiver=receiver))))
    finally:
        if receiver:
            receiver.close()
        runtime.close()


if __name__ == "__main__":
    main()
