"""Measure real TCP HTTP, PostgreSQL storage, ERP ACK and advisory contention.

Launched on a NEW isolated stand by acceptance_demo.py --benchmark --api-workers N.
No production credentials or database are accepted by this runner.
"""

import copy
import json
import math
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
from sqlalchemy import create_engine, text

from app.adapters.erp_http import HttpErpReceiver
from app.adapters.locks import production_guard
from app.application.bootstrap import ProductionSettings, build_runtime
from app.application.outbox import OutboxDeliveryWorker


def summary(values):
    ordered = sorted(values)
    return {
        "n": len(values),
        "mean_ms": round(sum(values) / len(values), 3),
        "p50_ms": round(ordered[len(values) // 2], 3),
        "p95_ms": round(ordered[math.ceil(len(values) * 0.95) - 1], 3),
        "max_ms": round(ordered[-1], 3),
    }


def main():
    root = Path(__file__).resolve().parents[2]
    directory = Path(os.environ["QC_DEMO_DIRECTORY"])
    if not directory.resolve().is_relative_to(root / ".local"):
        raise ValueError("Use acceptance_demo.py --benchmark on its isolated stand")
    tokens = json.loads((directory / "credentials/demo-tokens.json").read_text())
    dataset = json.loads((root / "data/v2/source-events.json").read_text("utf-8"))
    engine = create_engine(os.environ["DATABASE_URL"], hide_parameters=True)
    client = httpx.Client(base_url=os.environ["QC_API_URL"], timeout=120, trust_env=False)
    item = "assembly-001"
    base_event = dataset["events"]["incoming-1"]
    runtime = build_runtime(ProductionSettings.from_environment(os.environ))
    receiver = HttpErpReceiver(
        os.environ["QC_ERP_URL"] + "/external/v2/quality-results",
        (directory / "credentials/erp.token").read_text().strip(),
    )
    worker = OutboxDeliveryWorker(runtime.service.journal, receiver)
    phases, lock_samples = [], []
    stop = threading.Event()

    def sample_locks():
        with engine.connect() as connection:
            while not stop.wait(0.01):
                # pg_locks is live even in a long transaction; no cached statistics snapshot.
                count = connection.scalar(
                    text("""
                    SELECT count(*) FROM pg_locks WHERE locktype='advisory' AND NOT granted
                      AND database=(SELECT oid FROM pg_database WHERE datname=current_database())
                """)
                )
                lock_samples.append(int(count))

    def request(method, path, token, body=None):
        started = time.perf_counter()
        response = client.request(
            method, path, headers={"Authorization": f"Bearer {token}"}, json=body
        )
        response.raise_for_status()
        return (time.perf_counter() - started) * 1000, response.json()

    def ingest(event):
        return request(
            "POST",
            "/api/v2/events",
            tokens["vision-demo" if event["source_id"] == "vision-emulator-1" else "line-demo"],
            event,
        )[0]

    def storage():
        with engine.connect() as connection:
            sizes = {}
            for table, column in (
                ("quality_items", "state"),
                ("quality_bases", "snapshot"),
                ("quality_state_versions", "snapshot"),
                ("quality_outbox", "payload"),
                ("item_projection_versions", "snapshot"),
            ):
                row = (
                    connection.execute(
                        text(f"""
                    SELECT count(*) AS rows,
                           coalesce(sum(octet_length({column}::text)),0) AS json_bytes,
                           coalesce(sum(pg_column_size({column})),0) AS stored_column_bytes
                    FROM {table}
                """)
                    )
                    .mappings()
                    .one()
                )
                sizes[table] = {
                    **dict(row),
                    "relation_bytes": connection.scalar(
                        text("SELECT pg_total_relation_size(to_regclass(:table))"), {"table": table}
                    ),
                }
            sizes["raw_events"] = dict(
                connection.execute(
                    text("""
                SELECT count(*) AS rows,
                       coalesce(sum(octet_length(ciphertext)),0) AS ciphertext_bytes,
                pg_total_relation_size('raw_events') AS relation_bytes FROM raw_events
            """)
                )
                .mappings()
                .one()
            )
            return sizes

    sampler = threading.Thread(target=sample_locks)
    sampler.start()
    try:
        initial_ms = [ingest(dataset["events"][key]) for key in dataset["base_order"]]
        phases.append({"name": "seed", "ingest": summary(initial_ms), "storage": storage()})
        current = 0
        for total in (8, 24, 48):
            events = []
            for index in range(current, total):
                event = copy.deepcopy(base_event)
                event["event_id"] = f"performance-{index}"
                events.append(event)
            current = total
            started = time.perf_counter()
            with ThreadPoolExecutor(max_workers=4) as pool:
                futures = [pool.submit(ingest, value) for value in events]
                reads = [
                    pool.submit(request, "GET", f"/api/v2/items/{item}", tokens["controller"])
                    for _ in range(8)
                ]
                writes = [f.result() for f in futures]
                gets = [f.result()[0] for f in reads]
            elapsed = time.perf_counter() - started
            before_ack = storage()
            ack_times = []
            # All immutable messages are delivered through the real TCP SQLite ERP.
            messages = runtime.service.quality.list_outbox()
            for message in messages:
                if message["status"] == "acknowledged":
                    continue
                started = time.perf_counter()
                result = worker.deliver_once(message["message_id"])
                assert result and result["status"] == "acknowledged"
                ack_times.append((time.perf_counter() - started) * 1000)
            after_ack = storage()
            assert before_ack["quality_state_versions"] == after_ack["quality_state_versions"]
            phases.append(
                {
                    "extra_inspections": total,
                    "ingest": summary(writes),
                    "get": summary(gets),
                    "write_throughput_per_s": round(len(events) / elapsed, 3),
                    "ack": summary(ack_times),
                    "storage_before_ack": before_ack,
                    "storage_after_ack": after_ack,
                }
            )
        with ThreadPoolExecutor(max_workers=1) as pool:
            with engine.begin() as connection:
                production_guard(connection)
                blocked_event = copy.deepcopy(base_event)
                blocked_event["event_id"] = "controlled-guard-wait"
                began = time.perf_counter()
                blocked = pool.submit(ingest, blocked_event)
                deadline = began + 10
                while time.perf_counter() < deadline:
                    with engine.connect() as probe:
                        waiting = probe.scalar(
                            text(
                                "SELECT count(*) FROM pg_locks "
                                "WHERE locktype='advisory' AND NOT granted"
                            )
                        )
                    if waiting:
                        break
                    time.sleep(0.01)
                assert waiting, "writer never reached the held guard"
                read_ms, _ = request("GET", f"/api/v2/items/{item}", tokens["controller"])
                time.sleep(0.25)
                observed_wait_ms = (time.perf_counter() - began) * 1000
                assert not blocked.done()
            blocked_ms = blocked.result()
        report = {
            "synthetic": True,
            "api_worker_processes": int(os.environ["QC_API_WORKERS"]),
            "concurrent_clients": 4,
            "transport": "TCP HTTP/Uvicorn + PostgreSQL + SQLite ERP",
            "phases": phases,
            "locks": {
                "sampling_interval_ms": 10,
                "samples": len(lock_samples),
                "samples_with_waiters": sum(value > 0 for value in lock_samples),
                "max_waiters": max(lock_samples, default=0),
                "controlled_hold_observed_ms": round(observed_wait_ms, 3),
                "blocked_ingest_ms": round(blocked_ms, 3),
                "get_while_guard_held_ms": round(read_ms, 3),
            },
            "limitations": [
                "one synthetic hot item",
                "4 writer clients",
                "local Docker network",
                "relation bytes include indexes/TOAST and dead tuples; no VACUUM",
                "lock sampling is not exact per-request wait attribution",
                "not an SLA",
            ],
        }
        (directory / "evidence/performance.json").write_text(
            json.dumps(report, indent=2), encoding="utf-8"
        )
        print(json.dumps(report, indent=2))
    finally:
        stop.set()
        sampler.join(timeout=10)
        receiver.close()
        runtime.close()
        client.close()
        engine.dispose()


if __name__ == "__main__":
    main()
