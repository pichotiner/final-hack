"""Read-only TLS trust probes for the isolated P8 node simulation."""

import json
import os
import ssl
from pathlib import Path

import httpx
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError


def main():
    ca = "/run/credentials/tls/ca.crt"
    checked = {}
    with httpx.Client(verify=ssl.create_default_context(cafile=ca), trust_env=False) as client:
        checked["api_ca_hostname"] = (
            client.get("https://api-tls:8443/health/ready").status_code == 200
        )
        checked["erp_ca_hostname"] = client.get("https://erp-tls:8443/health").status_code == 200
        token = Path("/run/credentials/erp.token").read_text().strip()
        checked["erp_bearer_required"] = (
            client.get("https://erp-tls:8443/external/v2/work-orders").status_code == 401
        )
        checked["erp_authenticated"] = (
            client.get(
                "https://erp-tls:8443/external/v2/work-orders",
                headers={"Authorization": f"Bearer {token}"},
            ).status_code
            == 200
        )
    try:
        with httpx.Client(trust_env=False) as client:
            client.get("https://erp-tls:8443/health")
    except httpx.ConnectError:
        checked["untrusted_ca_rejected"] = True
    with httpx.Client(verify=ssl.create_default_context(cafile=ca), trust_env=False) as client:
        # Wrong SNI forces a hostname mismatch without disabling CA verification.
        try:
            client.get("https://erp-tls:8443/health", extensions={"sni_hostname": "wrong.invalid"})
        except httpx.ConnectError:
            checked["wrong_hostname_rejected"] = True
    url = make_url(os.environ["DATABASE_URL"])
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            checked["postgres_tls"] = connection.scalar(
                text("SELECT ssl FROM pg_stat_ssl WHERE pid=pg_backend_pid()")
            )
    finally:
        engine.dispose()
    plain = create_engine(url.update_query_dict({"sslmode": "disable"}))
    try:
        try:
            with plain.connect():
                checked["postgres_plaintext_rejected"] = False
        except DBAPIError:
            checked["postgres_plaintext_rejected"] = True
    finally:
        plain.dispose()
    required = {
        "api_ca_hostname",
        "erp_ca_hostname",
        "erp_bearer_required",
        "erp_authenticated",
        "untrusted_ca_rejected",
        "wrong_hostname_rejected",
        "postgres_tls",
        "postgres_plaintext_rejected",
    }
    assert set(checked) == required and all(checked.values()), checked
    print(json.dumps(checked))


if __name__ == "__main__":
    main()
