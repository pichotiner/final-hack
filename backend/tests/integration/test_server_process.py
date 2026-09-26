"""Real socket/process smoke: PostgreSQL -> Uvicorn -> separate delivery worker -> HTTP."""

import base64
import hashlib
import json
import os
import socket
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import httpx
import pytest
from sqlalchemy import text

from tests.integration.test_outbox_delivery import ack

pytestmark = pytest.mark.integration
pytest_plugins = ("tests.integration.test_traceability_postgres",)


def test_server_and_worker_run_as_separate_processes(production, tmp_path):
    _, writer, journal, _, _, item_id, _, _ = production
    with writer.connect() as connection:
        schema, role = connection.execute(text("SELECT current_schema(), current_user")).one()
    url = writer.url.update_query_dict({"options": f"-csearch_path={schema},public -crole={role}"})
    key = tmp_path / "key"
    key.write_text(base64.b64encode(os.urandom(32)).decode(), encoding="ascii")
    identities = tmp_path / "identities.json"
    identities.write_text(
        json.dumps(
            [
                {
                    "actor_id": "smoke-controller",
                    "roles": ["controller"],
                    "token_sha256": hashlib.sha256(b"smoke-token").hexdigest(),
                }
            ]
        ),
        encoding="utf-8",
    )
    erp_token = tmp_path / "erp-token"
    erp_token.write_text("erp-smoke", encoding="utf-8")
    environment = {
        **os.environ,
        "DATABASE_URL": url.render_as_string(hide_password=False),
        "QC_EVENT_KEY_FILE": str(key),
        "QC_EVENT_KEY_ID": "smoke",
        "QC_EVENT_KEY_VERSION": "1",
        "QC_IDENTITIES_FILE": str(identities),
        "QC_ERP_TOKEN_FILE": str(erp_token),
    }
    with socket.socket() as temporary:
        temporary.bind(("127.0.0.1", 0))
        port = temporary.getsockname()[1]
    received = []

    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):
            assert self.headers["Authorization"] == "Bearer erp-smoke"
            body = self.rfile.read(int(self.headers["Content-Length"]))
            payload = json.loads(body)
            received.append(payload)
            self.send_response(201)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(ack(payload)).encode())

        def log_message(self, *args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Receiver) as erp:
        thread = Thread(target=erp.serve_forever, daemon=True)
        thread.start()
        environment["QC_ERP_RESULTS_URL"] = (
            f"http://127.0.0.1:{erp.server_port}/external/v2/quality-results"
        )
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "app.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False) as client:
                for _ in range(100):
                    assert process.poll() is None, "Server exited before readiness."
                    try:
                        if client.get("/health/ready").status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    time.sleep(0.1)
                else:
                    pytest.fail("Server did not become ready.")
                response = client.get(
                    f"/api/v2/items/{item_id}", headers={"Authorization": "Bearer smoke-token"}
                )
                assert response.status_code == 200
                assert response.json()["item_id"] == item_id
                assert client.get("/openapi.json").json()["info"]["version"] == "0.3.0-draft"
            message_id = journal.quality.state(item_id)["outbox"][0]["message_id"]
            result = subprocess.run(
                [sys.executable, "-m", "app.worker", "--once", "--message-id", message_id],
                env=environment,
                capture_output=True,
                timeout=30,
            )
            assert result.returncode == 0, result.stderr.decode(errors="replace")
            assert journal.quality.get_outbox(message_id)["status"] == "acknowledged"
            assert any(value["message_id"] == message_id for value in received)
        finally:
            process.terminate()
            process.wait(timeout=10)
            erp.shutdown()
            thread.join(timeout=5)
