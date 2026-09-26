"""After-commit HTTP delivery; ACK binds the complete immutable wire message."""

import json
from urllib.request import Request, urlopen

import rfc8785


def deliver(chain, url, token):
    acknowledged = 0
    for body in chain.pending():
        request = Request(
            url.rstrip("/") + "/external/v2/quality-results",
            method="POST",
            data=rfc8785.dumps(body),
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        with urlopen(request, timeout=10) as response:
            if response.status not in {200, 201}:
                raise ValueError("No valid ACK")
            ack = json.load(response)
        chain.acknowledge(ack)
        acknowledged += 1
    return {"acknowledged": acknowledged}
