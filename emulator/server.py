"""Persistent DP-01 snapshot receiver, suitable for isolated demonstrator tests."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import socket
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit
from uuid import uuid4

import rfc8785

from contracts.validation import strict_json, validate

STREAM_FIELDS = (
    "producer_id",
    "external_system",
    "external_order_id",
    "external_item_id",
)


class Rejected(Exception):
    def __init__(self, status: int, code: str):
        self.status, self.code = status, code
        super().__init__(code)


class Receiver:
    def __init__(
        self,
        path: Path,
        orders: list[dict],
        *,
        producer_id="efqa-demo",
        system="erp-demo",
    ):
        self.path, self.orders = path, orders
        self.producer_id, self.system = producer_id, system
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS inbox (
                message_id TEXT PRIMARY KEY, stream TEXT NOT NULL, version INTEGER NOT NULL,
                digest TEXT NOT NULL, body TEXT NOT NULL, receipt TEXT NOT NULL,
                UNIQUE(stream, version))""")
            db.execute("""CREATE TABLE IF NOT EXISTS current_results (
                stream TEXT PRIMARY KEY, version INTEGER NOT NULL, message_id TEXT NOT NULL)""")

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.execute("PRAGMA synchronous=FULL")
        return db

    @staticmethod
    def stream(value: dict) -> str:
        return json.dumps([value[field] for field in STREAM_FIELDS], ensure_ascii=False)

    def receive(self, value: dict) -> tuple[int, dict]:
        validate("QualityResult", value, external=True)
        body = rfc8785.dumps(value)
        digest = hashlib.sha256(body).hexdigest()
        stream, version = self.stream(value), value["result_version"]
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute(
                "SELECT digest, receipt FROM inbox WHERE message_id=?",
                (value["message_id"],),
            ).fetchone()
            if prior:
                if prior[0] != digest:
                    raise Rejected(409, "message_id_conflict")
                return 200, json.loads(prior[1]) | {"status": "duplicate"}
            if db.execute(
                "SELECT 1 FROM inbox WHERE stream=? AND version=?", (stream, version)
            ).fetchone():
                raise Rejected(409, "result_version_conflict")
            # Auth is checked by HTTP even on duplicates. Mutable order membership is checked
            # only for a new message; an old successful receipt must remain replayable.
            if (
                value["producer_id"] != self.producer_id
                or value["external_system"] != self.system
            ):
                raise Rejected(403, "stream_owner_mismatch")
            if not any(
                order["external_order_id"] == value["external_order_id"]
                and value["external_item_id"] in order["item_ids"]
                for order in self.orders
            ):
                raise Rejected(422, "unknown_order_or_item")
            if version == 1 and value["previous_message_id"] is not None:
                raise Rejected(422, "invalid_previous_message")
            if value["previous_message_id"] == value["message_id"]:
                raise Rejected(422, "invalid_previous_message")
            current = db.execute(
                "SELECT version FROM current_results WHERE stream=?", (stream,)
            ).fetchone()
            current_version = current[0] if current else 0
            applied = version > current_version
            receipt = {field: value[field] for field in STREAM_FIELDS} | {
                "message_id": value["message_id"],
                "result_version": version,
                "content_digest": digest,
                "status": "accepted",
                "application_status": "applied" if applied else "superseded",
                "received_at": datetime.now(UTC).isoformat(),
                "current_version_at_accept": max(version, current_version),
            }
            db.execute(
                "INSERT INTO inbox VALUES (?,?,?,?,?,?)",
                (
                    value["message_id"],
                    stream,
                    version,
                    digest,
                    body.decode(),
                    json.dumps(receipt),
                ),
            )
            if applied:
                db.execute(
                    "INSERT INTO current_results VALUES (?,?,?) "
                    "ON CONFLICT(stream) DO UPDATE SET version=excluded.version, "
                    "message_id=excluded.message_id",
                    (stream, version, value["message_id"]),
                )
        return 201, receipt

    def saved(self, message_id: str) -> dict:
        with closing(self.connect()) as db:
            row = db.execute(
                "SELECT body, receipt FROM inbox WHERE message_id=?", (message_id,)
            ).fetchone()
        if not row:
            raise Rejected(404, "not_found")
        return {"body": json.loads(row[0]), "ack": json.loads(row[1])}

    def state(self, key: dict) -> dict:
        stream = self.stream(key)
        with closing(self.connect()) as db:
            # One consistent read transaction: current pointer and history cannot disagree.
            db.execute("BEGIN")
            current = db.execute(
                "SELECT i.body, i.receipt FROM current_results c "
                "JOIN inbox i ON i.message_id=c.message_id WHERE c.stream=?",
                (stream,),
            ).fetchone()
            versions = [
                row[0]
                for row in db.execute(
                    "SELECT version FROM inbox WHERE stream=? ORDER BY version",
                    (stream,),
                )
            ]
        if not current:
            raise Rejected(404, "not_found")
        gaps, expected = [], 1
        for version in versions:
            if version > expected:
                gaps.append({"from_version": expected, "to_version": version - 1})
            expected = version + 1
        return {
            "current": {"body": json.loads(current[0]), "ack": json.loads(current[1])},
            "missing_version_ranges": gaps,
        }


def make_server(receiver: Receiver, token: str, *, port=0, allow_faults=False, host="127.0.0.1"):
    if not token:
        raise ValueError("Set a nonempty emulator token outside git")

    class Handler(BaseHTTPRequestHandler):
        def reply(self, status, body):
            raw = json.dumps(body, ensure_ascii=False, allow_nan=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def error(self, status, code):
            self.reply(
                status,
                {
                    "error": {"code": code, "message": code, "details": []},
                    "request_id": str(uuid4()),
                },
            )

        def authorized(self):
            return hmac.compare_digest(
                self.headers.get("Authorization", ""), f"Bearer {token}"
            )

        def do_GET(self):
            url = urlsplit(self.path)
            if url.path == "/health":
                return self.reply(200, {"status": "ok", "synthetic": True})
            if not self.authorized():
                return self.error(401, "unauthorized")
            try:
                query = parse_qs(url.query, keep_blank_values=True)
                if url.path == "/external/v2/work-orders":
                    offset = int(query.get("cursor", ["0"])[0])
                    if offset < 0:
                        raise ValueError("negative cursor")
                    page = receiver.orders[offset : offset + 50]
                    next_cursor = (
                        str(offset + 50) if offset + 50 < len(receiver.orders) else None
                    )
                    return self.reply(200, {"items": page, "next_cursor": next_cursor})
                if url.path.startswith("/external/v2/quality-results/"):
                    return self.reply(
                        200, receiver.saved(unquote(url.path.rsplit("/", 1)[1]))
                    )
                if url.path == "/external/v2/quality-state":
                    if any(
                        len(query.get(field, [])) != 1 or not query[field][0].strip()
                        for field in STREAM_FIELDS
                    ):
                        raise ValueError("four stream keys required")
                    return self.reply(
                        200,
                        receiver.state(
                            {field: query[field][0] for field in STREAM_FIELDS}
                        ),
                    )
                return self.error(404, "not_found")
            except Rejected as exc:
                self.error(exc.status, exc.code)
            except ValueError:
                self.error(400, "invalid_query")
            except sqlite3.Error:
                self.error(503, "storage_unavailable")

        def do_POST(self):
            if not self.authorized():
                return self.error(401, "unauthorized")
            if urlsplit(self.path).path != "/external/v2/quality-results":
                return self.error(404, "not_found")
            from jsonschema.exceptions import ValidationError

            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 1024 * 1024:
                    raise ValueError("invalid size")
                self.connection.settimeout(10)
                value = strict_json(self.rfile.read(size))
                if (
                    allow_faults
                    and self.headers.get("X-Emulator-Fault") == "before-commit"
                ):
                    return self.error(503, "injected_before_commit")
                status, receipt = receiver.receive(value)
                if allow_faults and self.headers.get("X-Emulator-Fault") == "lost-ack":
                    self.close_connection = True
                    self.connection.shutdown(socket.SHUT_RDWR)
                    return
                return self.reply(status, receipt)
            except Rejected as exc:
                self.error(exc.status, exc.code)
            except (
                ValueError,
                TypeError,
                ValidationError,
                rfc8785.CanonicalizationError,
            ):
                self.error(422, "validation_error")
            except sqlite3.Error:
                self.error(503, "storage_unavailable")

        def log_message(self, *_):
            pass

    return ThreadingHTTPServer((host, port), Handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--token-file", type=Path)
    parser.add_argument("--state", type=Path, default=Path("emulator/state-v2-0.4.sqlite"))
    parser.add_argument("--orders", type=Path, default=Path("data/v2/work-orders.json"))
    parser.add_argument("--allow-faults", action="store_true")
    args = parser.parse_args()
    orders = json.loads(args.orders.read_text(encoding="utf-8"))
    for order in orders:
        validate("WorkOrder", order, external=True)
    receiver = Receiver(args.state, orders)
    with make_server(
        receiver,
        args.token_file.read_text("utf-8").strip() if args.token_file else os.environ.get("ERP_EMULATOR_TOKEN", ""),
        port=args.port,
        host=args.host,
        allow_faults=args.allow_faults,
    ) as server:
        print(f"Synthetic ERP v2: http://{args.host}:{server.server_port}", flush=True)
        server.serve_forever()


if __name__ == "__main__":
    main()
