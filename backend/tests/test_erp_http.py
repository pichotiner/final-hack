import httpx
import pytest
import rfc8785

from app.adapters.erp_http import HttpErpReceiver
from app.application.outbox import DeliveryFailure


@pytest.mark.parametrize("status,retryable", [(429, True), (503, True), (403, False), (409, False)])
def test_http_delivery_failure_classification(status, retryable):
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(status, text="secret or external payload")
        )
    ) as client:
        receiver = HttpErpReceiver(
            "http://erp/external/v2/quality-results", "secret", client=client
        )
        with pytest.raises(DeliveryFailure) as caught:
            receiver({"message_id": "m1"})
        assert caught.value.code == f"http_{status}"
        assert caught.value.retryable is retryable
        assert "secret" not in str(caught.value)


def test_canonical_body_and_duplicate_receipt():
    payload = {"z": "данные", "a": 1}
    bodies = []

    def handle(request):
        bodies.append(request.content)
        assert request.headers["authorization"] == "Bearer token"
        return httpx.Response(200, json={"status": "duplicate"})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        receiver = HttpErpReceiver("http://erp/results", "token", client=client)
        receiver(payload)
        receiver(dict(reversed(list(payload.items()))))
    assert bodies == [rfc8785.dumps(payload)] * 2


@pytest.mark.parametrize(
    "body", [b'{"status":"accepted"}', b"{", b"[]", b'{"status":"duplicate","status":"duplicate"}']
)
def test_invalid_success_response_is_not_acknowledgement(body):
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=body))
    ) as client:
        receiver = HttpErpReceiver("http://erp/results", "token", client=client)
        with pytest.raises(DeliveryFailure) as caught:
            receiver({})
        assert caught.value.retryable is False
