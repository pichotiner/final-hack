"""Standalone external ERP emulator with idempotent quality-result ACKs.

Run: python3 emulator/server.py --port 8090
All data is synthetic. SQLite state survives process restarts at the selected path.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def open_db(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE IF NOT EXISTS results (message_id TEXT PRIMARY KEY, digest TEXT NOT NULL, body TEXT NOT NULL, received_at TEXT NOT NULL)")
    db.commit()
    return db


def make_handler(orders: list[dict], db_path: Path):
    class Handler(BaseHTTPRequestHandler):
        server_version = "FinalHackERPEmulator/0.1"

        def reply(self, status: int, body: dict) -> None:
            data = canonical(body)
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            url = urlsplit(self.path)
            if url.path == "/health":
                return self.reply(200, {"status": "ok"})
            if url.path == "/external/v1/work-orders":
                query = parse_qs(url.query)
                try:
                    offset = int(query.get("cursor", ["0"])[0])
                    if offset < 0:
                        raise ValueError
                except ValueError:
                    return self.reply(400, {"error": {"code": "invalid_cursor", "message": "cursor must be a nonnegative integer"}})
                page = orders[offset:offset + 50]
                next_cursor = str(offset + 50) if offset + 50 < len(orders) else None
                return self.reply(200, {"items": page, "next_cursor": next_cursor})
            if url.path == "/external/v1/quality-results":
                with open_db(db_path) as db:
                    rows = db.execute("SELECT message_id, body, received_at FROM results ORDER BY received_at, message_id").fetchall()
                return self.reply(200, {"items": [{"message_id": row[0], "body": json.loads(row[1]), "received_at": row[2]} for row in rows]})
            return self.reply(404, {"error": {"code": "not_found", "message": "Unknown route"}})

        def do_POST(self) -> None:
            if urlsplit(self.path).path != "/external/v1/quality-results":
                return self.reply(404, {"error": {"code": "not_found", "message": "Unknown route"}})
            fault = self.headers.get("X-Emulator-Failure", "")
            if fault in {"429", "500", "503"}:
                return self.reply(int(fault), {"error": {"code": "injected_failure", "message": "Synthetic retryable failure"}})
            if fault == "timeout":
                time.sleep(3)
                return self.reply(503, {"error": {"code": "injected_timeout", "message": "Synthetic timeout"}})
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if size <= 0 or size > 1024 * 1024:
                    raise ValueError("body must be 1..1048576 bytes")
                body = json.loads(self.rfile.read(size))
                if not isinstance(body, dict):
                    raise ValueError("body must be an object")
                required = ("message_id", "external_order_id", "external_item_id", "result_version", "disposition", "confirmed_nonconformities", "cause_status", "review_required")
                if any(key not in body for key in required):
                    raise ValueError("missing required quality-result field")
                if any(not isinstance(body[key], str) or not body[key] for key in ("message_id", "external_order_id", "external_item_id")):
                    raise ValueError("IDs must be nonempty strings")
                if type(body["result_version"]) is not int or body["result_version"] < 1:
                    raise ValueError("result_version must be a positive integer")
                if body["disposition"] not in ("pending", "released", "rework", "rejected"):
                    raise ValueError("unknown disposition")
                if body["cause_status"] not in ("unresolved", "hypothesis", "established"):
                    raise ValueError("unknown cause status")
                if not isinstance(body["confirmed_nonconformities"], list) or type(body["review_required"]) is not bool:
                    raise ValueError("invalid result fields")
                matching_order = next((order for order in orders if order["external_order_id"] == body["external_order_id"]), None)
                if matching_order is None or body["external_item_id"] not in matching_order["item_ids"]:
                    raise ValueError("unknown external order or item")
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                return self.reply(422, {"status": "rejected", "message_id": None, "error": str(exc)})
            digest = hashlib.sha256(canonical(body)).hexdigest()
            with open_db(db_path) as db:
                db.execute("BEGIN IMMEDIATE")
                existing = db.execute("SELECT digest FROM results WHERE message_id = ?", (body["message_id"],)).fetchone()
                if existing:
                    if existing[0] == digest:
                        return self.reply(200, {"status": "duplicate", "message_id": body["message_id"]})
                    return self.reply(409, {"status": "rejected", "message_id": body["message_id"], "error": "message_id_conflict"})
                db.execute("INSERT INTO results VALUES (?, ?, ?, datetime('now'))", (body["message_id"], digest, canonical(body).decode("utf-8")))
                db.commit()
            return self.reply(201, {"status": "accepted", "message_id": body["message_id"]})

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--data", type=Path, default=ROOT / "data/scenarios.json")
    parser.add_argument("--state", type=Path, default=ROOT / "emulator/state.sqlite")
    args = parser.parse_args()
    orders = json.loads(args.data.read_text(encoding="utf-8"))["work_orders"]
    args.state.parent.mkdir(parents=True, exist_ok=True)
    with open_db(args.state):
        pass
    server = ThreadingHTTPServer((args.host, args.port), make_handler(orders, args.state))
    print(f"ERP emulator listening on http://{args.host}:{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
