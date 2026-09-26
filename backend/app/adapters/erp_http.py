"""HTTP is outside core transactions; canonical bytes are stable on every retry."""

import json

import httpx
import rfc8785

from app.adapters.json_events import _invalid_constant, _object
from app.application.outbox import DeliveryFailure
from app.domain.errors import InvalidEvent


class HttpErpReceiver:
    def __init__(self, url, token, *, client=None):
        if not url.startswith(("https://", "http://")) or not token.strip():
            raise ValueError("ERP endpoint and bearer credential are required.")
        self.url = url
        self.token = token
        self.client = client or httpx.Client(timeout=15, follow_redirects=False, trust_env=False)
        self.owns_client = client is None

    def __call__(self, payload):
        try:
            with self.client.stream(
                "POST",
                self.url,
                content=rfc8785.dumps(payload),
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Content-Type": "application/json",
                },
            ) as response:
                if response.status_code not in (200, 201):
                    raise DeliveryFailure(
                        f"http_{response.status_code}",
                        retryable=response.status_code == 429 or response.status_code >= 500,
                    )
                raw = bytearray()
                for chunk in response.iter_bytes():
                    raw.extend(chunk)
                    if len(raw) > 65_536:
                        raise DeliveryFailure("ack_too_large", retryable=False)
                receipt = json.loads(
                    raw.decode("utf-8"), object_pairs_hook=_object, parse_constant=_invalid_constant
                )
                if not isinstance(receipt, dict) or receipt.get("status") != (
                    "duplicate" if response.status_code == 200 else "accepted"
                ):
                    raise DeliveryFailure("invalid_http_ack", retryable=False)
                return receipt
        except httpx.TransportError as error:
            raise DeliveryFailure("transport_unavailable", retryable=True) from error
        except (ValueError, UnicodeError, InvalidEvent) as error:
            raise DeliveryFailure("invalid_ack_json", retryable=False) from error

    def close(self):
        if self.owns_client:
            self.client.close()
