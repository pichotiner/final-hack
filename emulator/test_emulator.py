from __future__ import annotations

import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from generate import make
from server import make_handler


class EmulatorTest(unittest.TestCase):
    def test_scenarios_are_deterministic_and_separate(self):
        scenarios, expected = make(2026)
        self.assertEqual((scenarios, expected), make(2026))
        self.assertNotEqual(scenarios, make(2027)[0])
        self.assertNotIn("expected", scenarios)
        self.assertEqual(expected["first-slice"]["before_decision"]["inspection_events"], 4)
        self.assertIn("operator_action", {event["event_type"] for event in scenarios["scenarios"][0]["events"]})

    def test_ack_duplicate_conflict_and_retryable_fault(self):
        with tempfile.TemporaryDirectory() as directory:
            orders = make(2026)[0]["work_orders"]
            server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(orders, Path(directory) / "state.sqlite"))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_port}"
            def post(body, headers=None):
                request = Request(base + "/external/v1/quality-results", data=json.dumps(body).encode(), headers={"Content-Type": "application/json", **(headers or {})}, method="POST")
                try:
                    with urlopen(request) as response:
                        return response.status, json.load(response)
                except HTTPError as error:
                    return error.code, json.load(error)
            result = {"message_id": "result-1", "external_order_id": "order-001", "external_item_id": "assembly-001", "result_version": 1, "disposition": "rework", "confirmed_nonconformities": ["card-1"], "cause_status": "unresolved", "review_required": False}
            try:
                with urlopen(base + "/external/v1/work-orders") as response:
                    self.assertEqual(json.load(response)["items"][0]["external_order_id"], "order-001")
                self.assertEqual(post(result)[1]["status"], "accepted")
                self.assertEqual(post(result)[1]["status"], "duplicate")
                self.assertEqual(post({**result, "disposition": "rejected"})[0], 409)
                self.assertEqual(post({**result, "message_id": "result-2"}, {"X-Emulator-Failure": "503"})[0], 503)
            finally:
                server.shutdown()
                thread.join()
                server.server_close()


if __name__ == "__main__":
    unittest.main()
