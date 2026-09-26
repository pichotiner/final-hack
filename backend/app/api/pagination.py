"""Revision-pinned timeline pages and shared bounded list parameters."""

import base64
import json
from datetime import datetime

from app.domain.errors import InvalidEvent, StaleRevision
from app.domain.quality import _digest


def limit(request):
    try:
        value = int(request.query_params.get("limit", "50"))
        if not 1 <= value <= 200:
            raise ValueError
        return value
    except ValueError as error:
        raise InvalidEvent("limit must be between 1 and 200.") from error


def timeline_page(request, item_id, entries):
    size = limit(request)
    values = sorted(
        entries,
        key=lambda e: (
            datetime.fromisoformat(e["occurred_at"]),
            e["source_id"],
            e["event_id"],
            e["record_id"],
        ),
    )
    revision = _digest({"item_id": item_id, "entries": values})
    cursor = request.query_params.get("cursor")
    offset = 0
    if cursor:
        try:
            if len(cursor) > 1024:
                raise ValueError
            saved = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
            if not isinstance(saved, dict) or set(saved) != {"revision", "offset"}:
                raise ValueError
            offset = saved["offset"]
            if type(offset) is not int or offset < 0:
                raise ValueError
        except (ValueError, TypeError, UnicodeError) as error:
            raise InvalidEvent("Invalid timeline cursor.") from error
        if saved["revision"] != revision:
            raise StaleRevision("Timeline changed; restart pagination from its first page.")
    end = offset + size
    return {
        "items": values[offset:end],
        "next_cursor": base64.urlsafe_b64encode(
            json.dumps({"revision": revision, "offset": end}).encode()
        ).decode()
        if len(values) > end
        else None,
    }
