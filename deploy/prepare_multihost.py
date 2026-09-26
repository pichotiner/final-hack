"""Create fresh per-host bundles; distribute only the matching node directory.

CA private key and original provisioning secrets remain in issuer/, never in a node bundle.
For existing installations, use enterprise-issued certificates and controlled data migration.
"""

import argparse
import json
import re
import shutil
from pathlib import Path

from pilot import ROOT, certificates, initialize, node_certificates, write


def prepare(directory, hosts, ports):
    directory = directory.resolve()
    if not directory.is_relative_to(ROOT / ".local"):
        raise ValueError("Bundles must be in ignored .local")
    if any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}", host) for host in hosts.values()):
        raise ValueError("Use DNS hostnames (no URL, credentials, whitespace or shell syntax)")
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    issuer = directory / "issuer"
    initialize(issuer)
    certificates(issuer / "tls", hosts["web"])
    node_certificates(issuer / "tls", hosts)
    values = dict(
        line.split("=", 1)
        for line in (issuer / "compose.env").read_text().splitlines()
        if line.strip()
    )
    for node in hosts:
        bundle = directory / node
        bundle.mkdir(mode=0o700)
        shutil.copytree(issuer / "tls" / node, bundle / "tls")
        if node == "api":
            credentials = bundle / "credentials"
            credentials.mkdir(mode=0o700)
            for name in (
                "event.key",
                "keyring.json",
                "identities.json",
                "erp.token",
                "import.token",
            ):
                shutil.copyfile(issuer / name, credentials / name)
                (credentials / name).chmod(0o600)
            (credentials / "tls").mkdir(mode=0o700)
            shutil.copyfile(issuer / "tls/ca.crt", credentials / "tls/ca.crt")
            database = (
                f"@{hosts['postgres']}:{ports['postgres']}/final_hack?sslmode=verify-full"
                "&sslrootcert=/run/credentials/tls/ca.crt"
            )
            runtime = {
                "DATABASE_URL": "postgresql+psycopg://qc_app:"
                + values["QC_APP_PASSWORD"]
                + database,
                "QC_REQUIRE_TLS": "1",
                "QC_KEYRING_FILE": "/run/credentials/keyring.json",
                "QC_IDENTITIES_FILE": "/run/credentials/identities.json",
                "QC_CONTRACT_ROOT": "/srv/contracts",
                "QC_ERP_CA_FILE": "/run/credentials/tls/ca.crt",
                "QC_ERP_TOKEN_FILE": "/run/credentials/erp.token",
                "QC_ERP_RESULTS_URL": f"https://{hosts['erp']}:{ports['erp']}/external/v2/quality-results",
                "QC_ERP_ORDERS_URL": f"https://{hosts['erp']}:{ports['erp']}/external/v2/work-orders",
            }
            write(bundle / "runtime.env", "".join(f"{k}={v}\n" for k, v in runtime.items()))
            write(
                bundle / "migrate.env",
                f"DATABASE_URL=postgresql+psycopg://qc_admin:{values['POSTGRES_PASSWORD']}{database}\nQC_APP_PASSWORD={values['QC_APP_PASSWORD']}\n",
            )
        elif node == "postgres":
            write(
                bundle / "runtime.env",
                f"POSTGRES_USER=qc_admin\nPOSTGRES_DB=final_hack\nPOSTGRES_PASSWORD={values['POSTGRES_PASSWORD']}\n",
            )
        elif node == "erp":
            (bundle / "state").mkdir(mode=0o700)
            shutil.copyfile(issuer / "erp.token", bundle / "erp.token")
            (bundle / "erp.token").chmod(0o600)
        elif node == "web":
            config = (ROOT / "deploy/nginx.distributed.conf").read_text()
            config = config.replace(
                "https://api-tls:8443", f"https://{hosts['api']}:{ports['api']}"
            )
            config = config.replace("proxy_ssl_name api-tls", "proxy_ssl_name " + hosts["api"])
            write(bundle / "nginx.conf", config)
        compose_vars = {
            "QC_UID": values["QC_UID"],
            "QC_GID": values["QC_GID"],
            {
                "api": "QC_API_PORT",
                "erp": "QC_ERP_PORT",
                "postgres": "QC_DB_PORT",
                "web": "QC_WEB_PORT",
            }[node]: ports[node],
        }
        write(bundle / "compose.env", "".join(f"{k}={v}\n" for k, v in compose_vars.items()))
    write(directory / "manifest.json", json.dumps({"hosts": hosts, "ports": ports}, indent=2))
    return directory


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    for node in ("api", "erp", "postgres", "web"):
        parser.add_argument(f"--{node}-host", required=True)
        parser.add_argument(
            f"--{node}-port", type=int, default=5432 if node == "postgres" else 8443
        )
    args = parser.parse_args()
    hosts = {node: getattr(args, f"{node}_host") for node in ("api", "erp", "postgres", "web")}
    ports = {node: getattr(args, f"{node}_port") for node in hosts}
    if any(not 1 <= port <= 65535 for port in ports.values()):
        parser.error("Ports must be in 1..65535")
    print(f"Created isolated node bundles: {prepare(args.directory, hosts, ports)}")


if __name__ == "__main__":
    main()
