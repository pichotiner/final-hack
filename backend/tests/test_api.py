import pytest

from app.api.server import _openapi, _validate, create_app
from app.domain.errors import InvalidEvent


@pytest.mark.parametrize(
    "change",
    [
        {"actor_id": "forged"},
        {"expected_revision": True},
        {"reason": " "},
        {"action": "confirm"},
        {"expected_basis_id": None},
    ],
)
def test_disposition_schema_rejects_unsafe_commands(change):
    command = {
        "action": "released",
        "reason": "reviewed",
        "expected_revision": 2,
        "expected_basis_id": "B1",
        "idempotency_key": "key1",
    }
    _validate("Disposition", command)
    with pytest.raises(InvalidEvent):
        _validate("Disposition", command | change)


def test_contract_paths_and_runtime_are_identical():
    app = create_app(object())
    actual = {
        (route.path, method.lower())
        for route in app.routes
        for method in getattr(route, "methods", ())
        if route.path.startswith("/api/v2")
    }
    expected = {
        (path, method) for path, methods in _openapi()["paths"].items() for method in methods
    }
    assert actual == expected
    for methods in _openapi()["paths"].values():
        for method, operation in methods.items():
            assert operation["responses"]["409"]
            if method == "post":
                assert operation["requestBody"]["required"] is True
