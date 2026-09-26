"""Isolated P6 stand: PostgreSQL, API, ERP, Vite and recorded Chromium acceptance.

Run from backend with its installed Python. No team database or credentials are used.
Generated credentials/logs live under .local; only evidence/ is suitable for sharing.
"""

import argparse
import copy
import json
import os
import platform
import secrets
import socket
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
VARIANTS = (
    "duplicate",
    "second",
    "two-signs",
    "late",
    "warning",
    "incoming",
    "missing",
    "disputed",
    "a01",
    "a03",
    "a05-unfinished",
    "a05-no-control",
    "a05-before-repair",
    "a06",
    "a12",
    "a14",
    "retry-ui",
)


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def fixtures(directory):
    profile = json.loads((BACKEND / "examples/erp-import-profile.json").read_text("utf-8"))
    order = json.loads((ROOT / "data/v2/work-orders.json").read_text("utf-8"))[0]
    for name in VARIANTS:
        prefix = name + "-"
        mapping = copy.deepcopy(profile["items"]["assembly-001"])
        item = prefix + "assembly-001"
        mapping["item_id"] = item
        mapping["component_mapping"] = {
            prefix + key: prefix + value for key, value in mapping["component_mapping"].items()
        }
        context = mapping["context"]
        context["required_operations"] = [prefix + run for run in context["required_operations"]]
        for rule in context["controls"]:
            rule["component_id"] = prefix + rule["component_id"]
            rule["control_session_id"] = prefix + rule["control_session_id"]
            if rule.get("operation_run_id"):
                rule["operation_run_id"] = prefix + rule["operation_run_id"]
        profile["items"][item] = mapping
        order["item_ids"].append(item)
        order["components"][item] = list(mapping["component_mapping"])
    write_json(directory / "profile.json", profile)
    write_json(directory / "orders.json", [order])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--serve", action="store_true", help="Keep stand alive after acceptance.")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument(
        "--publish", action="store_true", help="Publish passing redacted evidence to docs/demo."
    )
    parser.add_argument(
        "--slow-ms", type=int, default=0, help="Pause at video chapters, not test waits."
    )
    args = parser.parse_args()
    directory = (args.output or ROOT / ".local" / f"p6-{uuid4().hex[:10]}").resolve()
    # Never put credentials in a tracked directory, or overwrite an earlier run.
    if not directory.is_relative_to(ROOT / ".local"):
        parser.error("--output must be a NEW directory inside the ignored .local directory")
    directory.mkdir(parents=True, exist_ok=False)
    evidence = directory / "evidence"
    evidence.mkdir()
    fixtures(directory)
    env = dict(os.environ, PYTHONUTF8="1", PYTHONPATH=str(ROOT) + os.pathsep + str(BACKEND))
    project = f"qc-p6-{uuid4().hex[:10]}"
    env.update(POSTGRES_PASSWORD=secrets.token_hex(24), POSTGRES_PORT="0")
    compose = ["docker", "compose", "-f", str(ROOT / "deploy/compose.dev.yaml"), "-p", project]
    processes, logs = [], []

    def run(command, name, cwd=BACKEND):
        with (directory / f"{name}.log").open("w", encoding="utf-8") as log:
            result = subprocess.run(command, cwd=cwd, env=env, stdout=log, stderr=log)
        if result.returncode:
            raise RuntimeError(f"{name} failed; see {directory / (name + '.log')}")

    def start(command, name, cwd=BACKEND):
        log = (directory / f"{name}.log").open("w", encoding="utf-8")
        logs.append(log)
        proc = subprocess.Popen(command, cwd=cwd, env=env, stdout=log, stderr=log)
        processes.append(proc)
        return proc

    def wait(url):
        with httpx.Client(trust_env=False) as client:
            for _ in range(300):
                if any(proc.poll() is not None for proc in processes):
                    raise RuntimeError(f"A stand process exited; see logs in {directory}")
                try:
                    if client.get(url, timeout=1).status_code == 200:
                        return
                except httpx.TransportError:
                    pass
                time.sleep(0.2)
        raise RuntimeError(f"Readiness timed out: {url}")

    try:
        run([*compose, "up", "-d", "--wait", "--wait-timeout", "120"], "postgres")
        port = (
            subprocess.check_output([*compose, "port", "postgres", "5432"], env=env, text=True)
            .strip()
            .rsplit(":", 1)[1]
        )
        env["DATABASE_URL"] = (
            f"postgresql+psycopg://qc_admin:{env['POSTGRES_PASSWORD']}@127.0.0.1:{port}/final_hack"
        )
        env["QC_APP_PASSWORD"] = secrets.token_hex(24)
        creds = directory / "credentials"
        run([sys.executable, "-m", "app.manage", "init", "--directory", str(creds)], "credentials")
        run([sys.executable, "-m", "app.manage", "migrate"], "migrate")
        env.update(
            DATABASE_URL=f"postgresql+psycopg://qc_app:{env['QC_APP_PASSWORD']}@127.0.0.1:{port}/final_hack",
            QC_EVENT_KEY_FILE=str(creds / "event.key"),
            QC_EVENT_KEY_ID="p6-demo",
            QC_EVENT_KEY_VERSION="1",
            QC_IDENTITIES_FILE=str(creds / "identities.json"),
            QC_CONTRACT_ROOT=str(ROOT / "contracts"),
        )
        api_port, erp_port, ui_port = free_port(), free_port(), free_port()
        api_url, erp_url, ui_url = [
            f"http://127.0.0.1:{port}" for port in (api_port, erp_port, ui_port)
        ]
        env.update(
            ERP_EMULATOR_TOKEN=(creds / "erp.token").read_text().strip(),
            QC_ERP_RESULTS_URL=erp_url + "/external/v2/quality-results",
            QC_ERP_TOKEN_FILE=str(creds / "erp.token"),
            QC_API_URL=api_url,
            QC_UI_URL=ui_url,
            QC_ERP_URL=erp_url,
            QC_DEMO_DIRECTORY=str(directory),
            QC_PYTHON=sys.executable,
            QC_HEADED=str(int(args.headed)),
            QC_SLOW_MS=str(args.slow_ms),
            QC_PUBLISH_DEMO=str(int(args.publish)),
        )
        erp_args = [
            sys.executable,
            "-m",
            "emulator.server",
            "--port",
            str(erp_port),
            "--state",
            str(directory / "erp.sqlite"),
            "--orders",
            str(directory / "orders.json"),
            "--allow-faults",
        ]
        erp = start(erp_args, "erp", ROOT)
        env["QC_ERP_PID"] = str(erp.pid)
        env["QC_ERP_ARGS"] = json.dumps(erp_args)
        start(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "app.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(api_port),
            ],
            "api",
        )
        start(
            [
                "node",
                str(ROOT / "frontend/node_modules/vite/bin/vite.js"),
                "--host",
                "127.0.0.1",
                "--port",
                str(ui_port),
                "--strictPort",
            ],
            "ui",
            ROOT / "frontend",
        )
        wait(erp_url + "/health")
        wait(api_url + "/health/ready")
        wait(ui_url)
        run(
            [
                sys.executable,
                "-m",
                "app.import_erp",
                "--url",
                erp_url + "/external/v2/work-orders",
                "--contract",
                str(ROOT / "contracts/external-openapi.yaml"),
                "--profile",
                str(directory / "profile.json"),
                "--erp-token-file",
                str(creds / "erp.token"),
                "--integration-token-file",
                str(creds / "import.token"),
            ],
            "import",
        )
        # Import receipts contain no credentials and are evidence of the inbound leg.
        (evidence / "import.jsonl").write_bytes((directory / "import.log").read_bytes())
        write_json(
            evidence / "environment.json",
            {
                "base_commit": subprocess.check_output(
                    ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
                ).strip(),
                "working_tree": subprocess.check_output(
                    ["git", "status", "--short"], cwd=ROOT, text=True
                ),
                "python": platform.python_version(),
                "os": platform.platform(),
                "node": subprocess.check_output(["node", "--version"], text=True).strip(),
                "contract": "0.4.0",
                "synthetic": True,
                "entry": "scripts/acceptance_demo.py",
            },
        )
        print(f"Stand ready: {ui_url}; evidence: {evidence}", flush=True)
        run(["node", "scripts/acceptance.mjs"], "acceptance", ROOT / "frontend")
        print(f"Acceptance finished: {evidence / 'results.json'}", flush=True)
        if args.serve:
            start([sys.executable, "-m", "app.worker"], "worker-interactive")
            print(
                f"Stand kept alive. Tokens: {creds / 'demo-tokens.json'}. Ctrl+C to stop.",
                flush=True,
            )
            while True:
                time.sleep(1)
        return 0
    except KeyboardInterrupt:
        return 0
    finally:
        for process in reversed(processes):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        for log in logs:
            log.close()
        subprocess.run([*compose, "down", "--volumes", "--remove-orphans"], env=env, check=False)


if __name__ == "__main__":
    raise SystemExit(main())
