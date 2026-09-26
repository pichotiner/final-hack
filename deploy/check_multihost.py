"""Exercise separate Compose node projects through published TCP/TLS endpoints.

Docker Desktop uses host.docker.internal as the shared host gateway in this test.
No shared Docker network exists between node projects. Real hosts use their DNS.
"""

import json
import os
import ssl
import subprocess
import time
from urllib.request import Request, urlopen
from uuid import uuid4

from check_pilot import free_port
from pilot import ROOT, write
from prepare_multihost import prepare


def main():
    run_id = uuid4().hex[:10]
    ports = {node: free_port() for node in ("api", "erp", "postgres", "web")}
    bundle = prepare(
        ROOT / ".local" / ("p8-hosts-" + run_id),
        dict.fromkeys(ports, "host.docker.internal"),
        ports,
    )
    services = {"postgres": "database", "api": "core", "erp": "erp", "web": "web"}

    def compose(node, *args, **kwargs):
        env = dict(os.environ, QC_NODE_DIR=(bundle / node).as_posix())
        return subprocess.run(
            [
                "docker",
                "compose",
                "-p",
                f"p8-{run_id}-{node}",
                "--env-file",
                str(bundle / node / "compose.env"),
                "-f",
                str(ROOT / "deploy/multihost" / (services[node] + ".yaml")),
                *args,
            ],
            env=env,
            cwd=ROOT,
            check=True,
            **kwargs,
        )

    context = ssl.create_default_context(cafile=str(bundle / "web/tls/ca.crt"))
    tokens = json.loads((bundle / "issuer/demo-tokens.json").read_text())

    def request(path, token=None, body=None):
        req = Request(
            f"https://localhost:{ports['web']}" + path,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {token or ''}"},
            data=json.dumps(body).encode() if body else None,
        )
        with urlopen(req, context=context, timeout=30) as response:
            return json.load(response)

    try:
        for node in ("postgres", "erp", "api", "web"):
            compose(node, "config", "--quiet")
            compose(node, "up", "--build", "-d", "--wait", "--wait-timeout", "180")
        request("/health/ready")
        data = json.loads((ROOT / "data/v2/source-events.json").read_text("utf-8"))
        for key in data["base_order"]:
            event = data["events"][key]
            request(
                "/api/v2/events",
                tokens["vision-demo" if event["source_id"] == "vision-emulator-1" else "line-demo"],
                event,
            )
        for _ in range(100):
            deliveries = request("/api/v2/integrations/deliveries", tokens["integration"])
            if deliveries["items"] and all(
                row["status"] == "acknowledged" for row in deliveries["items"]
            ):
                break
            time.sleep(0.25)
        else:
            raise AssertionError("Multi-project TLS delivery did not finish")
        verified = compose(
            "api", "exec", "-T", "api", "python", "-m", "app.pilot_verify", capture_output=True
        )
        report = {
            "isolated_node_projects": 4,
            "shared_docker_network": False,
            "transport": "published TCP ports via host.docker.internal; HTTPS and PostgreSQL TLS",
            "evidence": json.loads(verified.stdout),
            "erp_acknowledged": len(deliveries["items"]),
        }
        write(bundle / "report.json", json.dumps(report, indent=2))
        print(f"Separate node projects verified: {bundle / 'report.json'}")
    finally:
        for node in ("web", "api", "erp", "postgres"):
            compose(node, "down")


if __name__ == "__main__":
    main()
