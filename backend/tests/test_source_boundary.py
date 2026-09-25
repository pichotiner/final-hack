import pytest

from app.application.ingestion import IngestEvent
from app.domain.errors import SourceMismatch


class NoWritesAllowed:
    def append_if_absent(self, event):
        pytest.fail("A mismatched source must be rejected before persistence.")


def test_source_binding_precedes_persistence(protector, codec, event_bytes):
    service = IngestEvent(NoWritesAllowed(), protector, codec)
    with pytest.raises(SourceMismatch):
        service.execute(event_bytes, authenticated_source_id="other-source")
