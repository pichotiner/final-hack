"""Bounded SQL read pages. One MVCC snapshot covers selection and DTO hydration."""

import base64
import json
from collections import defaultdict
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select, text

from app.adapters.postgres import item_projections, quality_items
from app.domain.errors import IntegrityViolation, InvalidEvent, StaleRevision, UnknownEntity


def pack(value):
    return base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode()).decode()


def unpack(cursor):
    try:
        if len(cursor) > 2048:
            raise ValueError
        return json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
    except (ValueError, UnicodeError, TypeError) as error:
        raise InvalidEvent("Invalid page cursor.") from error


class ReadPages:
    def __init__(self, views):
        self.views = views
        self.quality = views.quality

    def connection(self):
        return self.quality.engine.connect().execution_options(isolation_level="REPEATABLE READ")

    def items(self, cursor, size):
        with self.connection() as connection:
            keys = list(
                connection.scalars(
                    select(quality_items.c.item_id)
                    .where(quality_items.c.item_id > cursor)
                    .order_by(quality_items.c.item_id)
                    .limit(size + 1)
                )
            )
            return {
                "items": [self.views.item(key, connection=connection) for key in keys[:size]],
                "next_cursor": keys[size - 1] if len(keys) > size else None,
            }

    def deliveries(self, cursor, size):
        from app.adapters.postgres import quality_outbox

        with self.connection() as connection:
            rows = list(
                connection.execute(
                    self.quality._delivery_query()
                    .where(quality_outbox.c.message_id > cursor)
                    .order_by(quality_outbox.c.message_id)
                    .limit(size + 1)
                ).mappings()
            )
            return {
                "items": [dict(row) for row in rows[:size]],
                "next_cursor": rows[size - 1]["message_id"] if len(rows) > size else None,
            }

    def cards(self, cursor, size):
        with self.connection() as connection:
            rows = list(
                connection.execute(
                    text("""
                SELECT q.item_id, card.id FROM quality_items q
                CROSS JOIN LATERAL jsonb_object_keys(q.state->'nonconformities') AS card(id)
                WHERE card.id > :cursor ORDER BY card.id LIMIT :limit
            """),
                    {"cursor": cursor, "limit": size + 1},
                ).mappings()
            )
            by_item = defaultdict(set)
            for row in rows[:size]:
                by_item[row["item_id"]].add(row["id"])
            cards = {}
            for item, targets in by_item.items():
                value = self.views.item(item, connection=connection, target_ids=targets)
                cards.update({card["id"]: card for card in value["nonconformities"]})
            return {
                "items": [cards[row["id"]] for row in rows[:size]],
                "next_cursor": rows[size - 1]["id"] if len(rows) > size else None,
            }

    def orders(self, cursor, size):
        after = ["", ""]
        if cursor:
            after = unpack(cursor)
            if (
                not isinstance(after, list)
                or len(after) != 2
                or any(not isinstance(value, str) or len(value) > 128 for value in after)
            ):
                raise InvalidEvent("Invalid work-order cursor.")
        with self.connection() as connection:
            rows = list(
                connection.execute(
                    text("""
                SELECT * FROM (
                    SELECT DISTINCT ON (external_system, external_order_id)
                        external_system, external_order_id, document, receipt
                    FROM quality_erp_imports
                    WHERE (external_system, external_order_id) > (:system, :order)
                    ORDER BY external_system, external_order_id, revision DESC
                ) latest ORDER BY external_system, external_order_id LIMIT :limit
            """),
                    {"system": after[0], "order": after[1], "limit": size + 1},
                ).mappings()
            )
            return {
                "items": [self.order_view(row) for row in rows[:size]],
                "next_cursor": pack(
                    [rows[size - 1]["external_system"], rows[size - 1]["external_order_id"]]
                )
                if len(rows) > size
                else None,
            }

    @staticmethod
    def order_view(row):
        key = str(uuid5(NAMESPACE_URL, repr((row["external_system"], row["external_order_id"]))))
        return {
            **row["document"],
            "id": key,
            "external_system": row["external_system"],
            "item_ids": row["receipt"]["item_ids"],
        }

    def timeline(self, item_id, cursor, size):
        saved = unpack(cursor) if cursor else None
        if cursor and (
            not isinstance(saved, dict)
            or set(saved) != {"item", "revision", "offset"}
            or saved["item"] != item_id
            or type(saved["revision"]) is not int
            or type(saved["offset"]) is not int
            or saved["offset"] < 0
            or saved["offset"] > 2**31 - 1
        ):
            raise InvalidEvent("Invalid timeline cursor.")
        with self.connection() as connection:
            verified = connection.scalar(
                text("""
                SELECT q.state = v.snapshot FROM quality_items q
                LEFT JOIN quality_state_versions v ON v.item_id = q.item_id
                  AND v.storage_revision = q.storage_revision
                WHERE q.item_id = :item
            """),
                {"item": item_id},
            )
            if (
                verified is None
                and connection.scalar(
                    select(quality_items.c.item_id).where(quality_items.c.item_id == item_id)
                )
                is None
            ):
                raise UnknownEntity("Unknown item.")
            if verified is not True:
                raise IntegrityViolation(
                    "The mutable quality projection diverged from its version."
                )
            revision = (
                connection.scalar(
                    select(item_projections.c.revision).where(item_projections.c.item_id == item_id)
                )
                or 0
            )
            if saved and saved["revision"] != revision:
                raise StaleRevision("Timeline changed; restart pagination from its first page.")
            offset = saved["offset"] if saved else 0
            rows = list(
                connection.scalars(
                    text("""
                SELECT entry FROM item_projections p
                CROSS JOIN LATERAL jsonb_array_elements(p.snapshot->'timeline') AS e(entry)
                WHERE p.item_id = :item
                ORDER BY (entry->>'occurred_at')::timestamptz,
                         entry->>'source_id', entry->>'event_id', entry->>'record_id'
                LIMIT :limit OFFSET :offset
            """),
                    {"item": item_id, "limit": size + 1, "offset": offset},
                )
            )
            return {
                "items": rows[:size],
                "next_cursor": pack(
                    {"item": item_id, "revision": revision, "offset": offset + size}
                )
                if len(rows) > size
                else None,
            }
