import base64
import hashlib
import json
import os

import pytest

from app.application.bootstrap import (
    ProductionSettings,
    _read_identities,
    _read_key,
)


def test_settings_require_database_secret_mount_and_identity_registry(tmp_path):
    values = {
        "DATABASE_URL": "postgresql+psycopg://user:secret@localhost/db",
        "QC_EVENT_KEY_FILE": str(tmp_path / "key"),
        "QC_EVENT_KEY_ID": "journal",
        "QC_EVENT_KEY_VERSION": "2",
        "QC_IDENTITIES_FILE": str(tmp_path / "identities.json"),
    }
    settings = ProductionSettings.from_environment(values)
    assert settings.event_key_version == 2
    for name in values:
        with pytest.raises(ValueError):
            ProductionSettings.from_environment(
                {key: value for key, value in values.items() if key != name}
            )


def test_secret_and_identity_files_are_validated_without_defaults(tmp_path):
    key = os.urandom(32)
    key_file = tmp_path / "key"
    key_file.write_text(base64.b64encode(key).decode("ascii"), encoding="ascii")
    assert _read_key(key_file) == key
    token = "not-stored-in-registry"
    registry = tmp_path / "identities.json"
    registry.write_text(
        json.dumps(
            [
                {
                    "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
                    "actor_id": "controller-1",
                    "roles": ["controller"],
                }
            ]
        ),
        encoding="utf-8",
    )
    identities = _read_identities(registry)
    assert identities[hashlib.sha256(token.encode()).hexdigest()].actor_id == "controller-1"
    key_file.write_text("not-base64!", encoding="ascii")
    with pytest.raises(ValueError, match="cannot be loaded"):
        _read_key(key_file)
    registry.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="non-empty"):
        _read_identities(registry)
