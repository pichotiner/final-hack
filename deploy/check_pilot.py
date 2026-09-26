"""Isolated populated TLS/key-rotation/backup/restore smoke test (Docker required)."""

import argparse
import copy
import json
import secrets
import socket
import time
from uuid import uuid4

from pilot import ROOT, Pilot, rotate_key, write


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def ready(pilot):
    for _ in range(60):
        try:
            pilot.http("/health/ready")
            return
        except OSError:
            time.sleep(1)
    raise RuntimeError("HTTPS readiness timed out after restart")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--distributed-tls", action="store_true")
    args = parser.parse_args()
    directory = ROOT / ".local" / ("p7-operations-" + uuid4().hex[:10])
    directory.mkdir(mode=0o700)
    source = Pilot(directory / "source", free_port(), distributed_tls=args.distributed_tls)
    restored = Pilot(directory / "restored", free_port())
    try:
        source.initialize("localhost")
        source.compose("up", "--build", "-d", "--wait", "--wait-timeout", "180")
        ready(source)
        source.seed()
        tokens = json.loads((source.directory / "credentials/demo-tokens.json").read_text())
        item = source.http("/api/v2/items/assembly-001", tokens["controller"])
        card = item["nonconformities"][0]
        old_basis = source.http("/api/v2/bases/" + card["basis_id"], tokens["controller"])
        source.http(
            "/api/v2/nonconformities/" + card["id"] + "/decisions",
            tokens["controller"],
            {
                "decision": "confirm",
                "reason": "Synthetic populated restore verification",
                "expected_revision": item["context_revision"],
                "expected_basis_id": card["basis_id"],
                "idempotency_key": str(uuid4()),
            },
        )
        before = source.verify()
        assert before["key_versions"] == [1] and before["counts"]["decisions"] == 1
        assert rotate_key(source.directory / "credentials") == 2
        source.compose("restart", "api", "worker")
        ready(source)
        assert source.http("/api/v2/bases/" + card["basis_id"], tokens["controller"]) == old_basis
        dataset = json.loads((ROOT / "data/v2/source-events.json").read_text("utf-8"))
        event = copy.deepcopy(dataset["events"]["incoming-1"])
        event["event_id"] = "after-key-rotation"
        source.http("/api/v2/events", tokens["vision-demo"], event)
        rotated = source.verify()
        assert rotated["key_versions"] == [1, 2]
        password = secrets.token_urlsafe(40)
        backup = directory / "backup"
        source.backup(backup, password)
        report = restored.restore(backup, password)
        manifest = json.loads((backup / "manifest.json").read_text())
        assert report["fingerprints"] == manifest["evidence"]["fingerprints"]
        assert report["key_versions"] == [1, 2]
        assert report["redelivered"] == report["counts"]["outbox"] > 0
        assert restored.http("/api/v2/bases/" + card["basis_id"], tokens["controller"]) == old_basis
        write(directory / "report.json", json.dumps(report, indent=2))
        print(f"Populated TLS/rotation/restore verified: {directory / 'report.json'}")
    finally:
        for pilot in (restored, source):
            if (pilot.directory / "credentials/compose.env").exists():
                pilot.compose("down")


if __name__ == "__main__":
    main()
