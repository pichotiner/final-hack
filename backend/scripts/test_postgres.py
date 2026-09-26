"""Run integration tests in an isolated disposable PostgreSQL Compose project."""

import os
import secrets
import subprocess
import sys
from pathlib import Path
from uuid import uuid4


def main() -> int:
    backend = Path(__file__).resolve().parents[1]
    compose = backend.parent / "deploy" / "compose.dev.yaml"
    environment = dict(os.environ)
    password = secrets.token_hex(24)
    environment.update(POSTGRES_PASSWORD=password, POSTGRES_PORT="0")
    project = f"qc-journal-test-{uuid4().hex[:12]}"
    command = ["docker", "compose", "-f", str(compose), "-p", project]
    try:
        subprocess.run(
            [*command, "up", "-d", "--wait", "--wait-timeout", "120"],
            env=environment,
            cwd=backend,
            check=True,
        )
        address = subprocess.check_output(
            [*command, "port", "postgres", "5432"],
            env=environment,
            cwd=backend,
            text=True,
        ).strip()
        port = int(address.rsplit(":", 1)[1])
        environment["TEST_DATABASE_URL"] = (
            f"postgresql+psycopg://qc_admin:{password}@127.0.0.1:{port}/final_hack_test"
        )
        return subprocess.run(
            [sys.executable, "-m", "pytest", "-m", "integration", *sys.argv[1:]],
            env=environment,
            cwd=backend,
            check=False,
        ).returncode
    finally:
        # Only remove the unique project created above, never the team's dev volume.
        subprocess.run(
            [*command, "down", "--volumes", "--remove-orphans"],
            env=environment,
            cwd=backend,
            check=False,
        )


if __name__ == "__main__":
    raise SystemExit(main())
