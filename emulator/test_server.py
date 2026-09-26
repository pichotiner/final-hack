from __future__ import annotations

import copy
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from uuid import uuid4

import pytest

from contracts.validation import validate
from emulator.server import Receiver, Rejected, STREAM_FIELDS, make_server

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def feed():
    return json.loads((ROOT / "data/v2/erp-results.json").read_text())


@pytest.fixture
def receiver(tmp_path):
    orders = json.loads((ROOT / "data/v2/work-orders.json").read_text())
    return Receiver(tmp_path / "test.sqlite", orders)


def test_reorder_gaps_original_receipt_and_final_sync(receiver, feed):
    _, second = receiver.receive(feed[1])
    assert second["application_status"] == "applied"
    assert receiver.state(feed[1])["missing_version_ranges"] == [
        {"from_version": 1, "to_version": 1}
    ]
    _, first = receiver.receive(feed[0])
    assert first["application_status"] == "superseded"
    receiver.receive(feed[2])
    status, repeat = receiver.receive(feed[1])
    assert status == 200
    assert repeat == second | {"status": "duplicate"}
    assert repeat["current_version_at_accept"] == 2  # original commit, not current 3
    state = receiver.state(feed[0])
    assert state["current"]["body"] == feed[2]
    assert state["missing_version_ranges"] == []
    validate("QualityState", state, external=True)
    validate("Ack", repeat, external=True)


def test_restart_duplicate_does_not_depend_on_mutated_orders(receiver, feed):
    _, first = receiver.receive(feed[1])
    restarted = Receiver(receiver.path, [])
    status, ack = restarted.receive(feed[1])
    assert status == 200 and ack == first | {"status": "duplicate"}
    assert restarted.saved(feed[1]["message_id"])["body"] == feed[1]


def test_conflicts_even_for_superseded_versions_are_atomic(receiver, feed):
    receiver.receive(feed[1])
    receiver.receive(feed[0])
    other = feed[0] | {"message_id": "different-id-v1"}
    with pytest.raises(Rejected, match="result_version_conflict"):
        receiver.receive(other)
    changed = feed[1] | {"review_required": False}
    with pytest.raises(Rejected, match="message_id_conflict"):
        receiver.receive(changed)
    assert receiver.state(feed[0])["current"]["body"] == feed[1]


def test_stream_isolation_and_concurrent_duplicate(receiver, feed):
    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = list(pool.map(lambda _: receiver.receive(feed[1]), range(4)))
    assert sorted(status for status, _ in outcomes) == [200, 200, 200, 201]
    other = copy.deepcopy(feed[0])
    other.update(message_id="other-item-v1", external_item_id="assembly-002")
    receiver.receive(other)
    assert receiver.state(other)["current"]["body"]["result_version"] == 1
    assert receiver.state(feed[0])["current"]["body"]["result_version"] == 2


def test_huge_version_uses_ranges_not_unbounded_enumeration(receiver, feed):
    value = feed[1] | {"message_id": "high-version", "result_version": 1000000}
    receiver.receive(value)
    assert receiver.state(value)["missing_version_ranges"] == [
        {"from_version": 1, "to_version": 999999}
    ]


def test_http_lost_ack_and_restart(receiver, feed):
    token = uuid4().hex

    def request(server, method, path, body=None, *, auth=True, fault=None, raw=None):
        headers = {"Content-Type": "application/json"}
        if auth:
            headers["Authorization"] = f"Bearer {token}"
        if fault:
            headers["X-Emulator-Fault"] = fault
        data = (
            raw
            if raw is not None
            else json.dumps(body).encode()
            if body is not None
            else None
        )
        req = Request(
            f"http://127.0.0.1:{server.server_port}{path}",
            method=method,
            headers=headers,
            data=data,
        )
        try:
            with urlopen(req, timeout=5) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            return error.code, json.load(error)

    server = make_server(receiver, token, allow_faults=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, _ = request(
            server, "POST", "/external/v2/quality-results", feed[1], auth=False
        )
        assert status == 401
        status, _ = request(
            server,
            "POST",
            "/external/v2/quality-results",
            feed[1],
            fault="before-commit",
        )
        assert status == 503
        with pytest.raises(Rejected):
            receiver.saved("msg-2")
        with pytest.raises(Exception) as error:
            request(
                server,
                "POST",
                "/external/v2/quality-results",
                feed[1],
                fault="lost-ack",
            )
        assert not isinstance(error.value, HTTPError)
        status, _ = request(
            server, "POST", "/external/v2/quality-results", raw=b'{"x":1,"x":2}'
        )
        assert status == 422
        status, _ = request(server, "GET", "/external/v1/work-orders")
        assert status == 404  # no silent v1 alias
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    restarted = make_server(Receiver(receiver.path, receiver.orders), token)
    thread = threading.Thread(target=restarted.serve_forever, daemon=True)
    thread.start()
    try:
        status, ack = request(
            restarted, "POST", "/external/v2/quality-results", feed[1]
        )
        assert status == 200 and ack["status"] == "duplicate"
        key = {field: feed[1][field] for field in STREAM_FIELDS}
        status, current = request(
            restarted, "GET", "/external/v2/quality-state?" + urlencode(key)
        )
        assert status == 200 and current["current"]["body"] == feed[1]
        status, page = request(restarted, "GET", "/external/v2/work-orders")
        assert status == 200
        validate("WorkOrderPage", page, external=True)
    finally:
        restarted.shutdown()
        restarted.server_close()
        thread.join()
