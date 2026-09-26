import pytest
from fastapi.testclient import TestClient

from app.api.server import create_app
from app.application.ingestion import IngestEvent
from app.domain.errors import SourceMismatch


class NoWritesAllowed:
    def append_if_absent(self, event):
        pytest.fail("A mismatched source must be rejected before persistence.")


def test_source_binding_precedes_persistence(protector, codec, event_bytes):
    service = IngestEvent(NoWritesAllowed(), protector, codec)
    with pytest.raises(SourceMismatch):
        service.execute(event_bytes, authenticated_source_id="other-source")


class BoundaryOnlyService:
    quality = None

    def _require(self, token, roles):
        assert token == "boundary-test" and roles == {"source"}

    def ingest(self, token, raw):
        pytest.fail("An invalid machine state must be rejected before ingestion.")


@pytest.mark.parametrize("state", [[], {}, None, 42, True, "unexpected-state"])
def test_shared_api_rejects_invalid_machine_state_with_422(trace_document, state):
    event = trace_document("invalid-state", "machine_state")
    event["payload"]["state"] = state
    with TestClient(
        create_app(BoundaryOnlyService(), shared=True), raise_server_exceptions=False
    ) as client:
        response = client.post(
            "/api/v2/events",
            json=event,
            headers={"Authorization": "Bearer boundary-test"},
        )
    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == (
        "invalid_enum" if isinstance(state, str) else "validation_error"
    )
    assert body["error"]["details"] == []
    assert body["request_id"]
