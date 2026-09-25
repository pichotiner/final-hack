"""Synthetic, in-memory cost experiment for the existing traceability reducer.

Does not connect to a database, ingest events, or measure end-to-end throughput.
JSON byte counts are uncompressed representations, not PostgreSQL disk usage.
"""

import argparse
import json
import os
import platform
import random
import statistics
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import perf_counter
from uuid import NAMESPACE_URL, UUID, uuid5

from app.adapters.json_events import JsonEventCodec
from app.domain.events import RecordMetadata
from app.domain.traceability import PROCESSOR_VERSION, rebuild_history, trace_fact

ITEM_ID = "measurement-assembly-001"
BASE_TIME = datetime(2026, 9, 25, 10, tzinfo=UTC)
INPUT_SEED = 20260925


def positive(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("Expected a positive integer.")
    return number


def facts_for(operations: int, *, chain: bool) -> list:
    """Stable synthetic IDs, reliable clocks, two events per completed operation."""
    codec = JsonEventCodec()
    facts = []

    def add(event_id: str, kind: str, seconds: int, payload: dict, run_id=None):
        occurred = BASE_TIME + timedelta(seconds=seconds)
        document = {
            "event_id": event_id,
            "event_type": kind,
            "schema_version": "1.0",
            "source_id": "measurement-source",
            "item_id": ITEM_ID,
            "item_type_id": "bracket-demo",
            "occurred_at": occurred.isoformat(),
            "identification_status": "reliable",
            "time_quality": "synchronized",
            "line_id": "line-demo",
            "station_id": "station-demo",
            "operator_id": "operator-demo",
            "equipment_id": "machine-demo",
            "operation_run_id": run_id,
            "payload": payload,
        }
        prepared = codec.decode(json.dumps(document).encode("utf-8"))
        metadata = RecordMetadata(
            record_id=uuid5(NAMESPACE_URL, f"efqa-cost/{event_id}"),
            envelope=prepared.envelope,
            received_at=occurred + timedelta(seconds=1),
            content_digest=prepared.content_digest,
            initial_projection_status="unresolved",
            initial_issues=("measurement_input",),
        )
        facts.append(trace_fact(metadata, document))

    add(
        "received",
        "item_received",
        0,
        {
            "work_order_id": "order-demo",
            "component_ids": ["component-demo"],
            "assembly_version": "1",
        },
    )
    for index in range(operations):
        run_id = f"run-{index:06d}"
        payload = {"operation_type_id": "machining", "shift_id": "shift-demo"}
        if chain and index:
            payload["previous_operation_run_id"] = f"run-{index - 1:06d}"
        start = 1 + index * 180
        add(f"start-{index:06d}", "operation_started", start, payload, run_id)
        add(f"finish-{index:06d}", "operation_finished", start + 120, payload, run_id)
    return facts


def json_value(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    raise TypeError("Unsupported snapshot value.")


def snapshot_bytes(history) -> bytes:
    # Same asdict/default JSON shape used by the current storage adapter.
    return json.dumps(asdict(history), default=json_value).encode("utf-8")


def summary(samples: list[float]) -> dict:
    return {
        "median_ms": round(statistics.median(samples), 3),
        "max_ms": round(max(samples), 3),
        "samples_ms": [round(value, 3) for value in samples],
    }


def measure(operations: int, *, chain: bool, repeats: int) -> dict:
    facts = facts_for(operations, chain=chain)
    expected = rebuild_history(ITEM_ID, facts)
    assert len(expected.timeline) == 1 + 2 * operations
    assert len(expected.operations) == operations
    assert all(op.status == "completed" for op in expected.operations)
    assert all(op.durations[0].value == 120 for op in expected.operations)
    assert sum(op.previous_operation_run_id is not None for op in expected.operations) == (
        operations - 1 if chain else 0
    )
    assert rebuild_history(ITEM_ID, list(reversed(facts))) == expected
    shuffled_facts = list(facts)
    random.Random(INPUT_SEED).shuffle(shuffled_facts)
    assert rebuild_history(ITEM_ID, shuffled_facts) == expected
    encoded = snapshot_bytes(expected)  # Warm up conversion separately from replay.
    replay_times, encoding_times = [], []
    for _ in range(repeats):
        started = perf_counter()
        history = rebuild_history(ITEM_ID, shuffled_facts)
        replay_times.append((perf_counter() - started) * 1000)
        started = perf_counter()
        encoded = snapshot_bytes(history)
        encoding_times.append((perf_counter() - started) * 1000)
        assert history == expected  # Verification is outside both timed sections.
    return {
        "shape": "rework_chain" if chain else "independent_operations",
        "operations": operations,
        "events": len(facts),
        "replay": summary(replay_times),
        "snapshot_encoding": summary(encoding_times),
        "final_snapshot_json_bytes": len(encoded),
    }


def snapshot_growth(operations: int) -> dict:
    """Count actual serialized prefixes, without pretending to persist them."""
    facts = facts_for(operations, chain=False)
    total_bytes, final_bytes = 0, 0
    for length in range(1, len(facts) + 1):
        history = rebuild_history(ITEM_ID, facts[:length])
        assert len(history.timeline) == length
        final_bytes = len(snapshot_bytes(history))
        total_bytes += final_bytes
    return {
        "operations": operations,
        "events": len(facts),
        "snapshots": len(facts),
        "final_snapshot_json_bytes": final_bytes,
        "all_version_snapshots_json_bytes": total_bytes,
        "all_versions_to_final_ratio": round(total_bytes / final_bytes, 2),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--operations", type=positive, nargs="+", default=[50, 250, 1000])
    parser.add_argument("--repeats", type=positive, default=7)
    parser.add_argument("--growth-operations", type=positive, nargs="+", default=[20, 40, 80])
    parser.add_argument(
        "--output", type=Path, help="Save UTF-8 JSON to a new file; never overwrite a previous run."
    )
    args = parser.parse_args()
    report = {
        "experiment": "traceability-cost-v1",
        "synthetic": True,
        "processor_version": PROCESSOR_VERSION,
        "input_order": "seeded shuffle",
        "seed": INPUT_SEED,
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "processor": platform.processor(),
            "logical_cpus": os.cpu_count(),
        },
        "scope": "in-memory reducer and JSON encoding; excludes database, crypto and transport",
        "repeats": args.repeats,
        "replay_cases": [
            measure(operations, chain=chain, repeats=args.repeats)
            for operations in args.operations
            for chain in (False, True)
        ],
        "snapshot_growth": [snapshot_growth(size) for size in args.growth_operations],
    }
    report["recorded_at"] = datetime.now(UTC).isoformat()
    encoded_report = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    if args.output is not None:
        with args.output.open("x", encoding="utf-8") as output:
            output.write(encoded_report)
    print(encoded_report, end="")


if __name__ == "__main__":
    main()
