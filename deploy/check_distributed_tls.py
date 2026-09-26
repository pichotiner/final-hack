"""Exercise all TLS hops and negative trust/hostname/plaintext checks on isolated nodes."""

import json
import time
from uuid import uuid4

from check_pilot import free_port
from pilot import ROOT, Pilot, write


def main():
    pilot = Pilot(
        ROOT / ".local" / ("p8-tls-" + uuid4().hex[:10]), free_port(), distributed_tls=True
    )
    try:
        pilot.initialize("localhost")
        # --quiet never prints interpolated database credentials.
        pilot.compose("config", "--quiet")
        pilot.compose("up", "--build", "-d", "--wait", "--wait-timeout", "180")
        pilot.http("/health/ready")
        pilot.seed()
        tokens = json.loads((pilot.directory / "credentials/demo-tokens.json").read_text())
        for _ in range(100):
            deliveries = pilot.http(
                "/api/v2/integrations/deliveries?limit=200", tokens["integration"]
            )
            if deliveries["items"] and all(
                row["status"] == "acknowledged" for row in deliveries["items"]
            ):
                break
            time.sleep(0.25)
        else:
            raise AssertionError("TLS ERP delivery did not acknowledge all messages")
        result = pilot.compose(
            "exec", "-T", "api", "python", "-m", "app.tls_probe", capture_output=True
        )
        probes = json.loads(result.stdout)
        assert all(probes.values()), probes
        report = {
            "tls_probes": probes,
            "erp_acknowledged": len(deliveries["items"]),
            "evidence": pilot.verify(),
            "synthetic_nodes": True,
        }
        write(pilot.directory / "tls-report.json", json.dumps(report, indent=2))
        print(f"TLS verified: {pilot.directory / 'tls-report.json'}")
    finally:
        if (pilot.directory / "credentials/compose.env").exists():
            pilot.compose("down")


if __name__ == "__main__":
    main()
