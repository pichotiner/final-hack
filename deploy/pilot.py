"""Single-host pilot: HTTPS launch, quiesced backup, isolated restore and reconciliation.

Run with the installed backend Python environment, from any working directory.
Secrets are generated under ignored .local. Existing installations are never reset.
"""

import argparse
import hashlib
import io
import ipaddress
import json
import os
import shutil
import ssl
import subprocess
import sys
import tarfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.request import Request, urlopen

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
from cryptography.x509.oid import NameOID

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from app.manage import initialize, rotate_identity, rotate_key  # noqa: E402


def write(path, value):
    with path.open("xb") as stream:
        stream.write(value if isinstance(value, bytes) else value.encode("utf-8"))
    path.chmod(0o600)


def certificates(directory, hostname):
    directory.mkdir(mode=0o700)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "EFQA local pilot CA")])
    now = datetime.now(UTC)
    ca = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=365))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(False, False, False, False, False, True, True, False, False),
            critical=True,
        )
        .sign(ca_key, hashes.SHA256())
    )
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    sans = [x509.DNSName(name) for name in ("localhost", "api-tls", "erp-tls", "web")] + [
        x509.IPAddress(ipaddress.ip_address("127.0.0.1"))
    ]
    if hostname != "localhost":
        try:
            sans.append(x509.IPAddress(ipaddress.ip_address(hostname)))
        except ValueError:
            sans.append(x509.DNSName(hostname))
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)]))
        .issuer_name(ca_name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=90))
        .add_extension(x509.SubjectAlternativeName(sans), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
        )
        .sign(ca_key, hashes.SHA256())
    )
    for name, obj in (("ca", ca_key), ("server", key)):
        write(
            directory / f"{name}.key",
            obj.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            ),
        )
    for name, obj in (("ca", ca), ("server", cert)):
        write(directory / f"{name}.crt", obj.public_bytes(serialization.Encoding.PEM))


def node_certificates(directory, hosts=None):
    """Issue separate leaf keys; only the issuer directory contains the CA private key."""
    hosts = hosts or {"api": "api-tls", "erp": "erp-tls", "postgres": "postgres"}
    ca = x509.load_pem_x509_certificate((directory / "ca.crt").read_bytes())
    ca_key = serialization.load_pem_private_key((directory / "ca.key").read_bytes(), None)
    now = datetime.now(UTC)
    for node, hostname in hosts.items():
        output = directory / node
        if output.exists():
            continue
        output.mkdir(mode=0o700)
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        try:
            identity = x509.IPAddress(ipaddress.ip_address(hostname))
        except ValueError:
            identity = x509.DNSName(hostname)
        names = [
            identity,
            x509.DNSName("localhost"),
            x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
        ]
        cert = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)]))
            .issuer_name(ca.subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5))
            .not_valid_after(now + timedelta(days=90))
            .add_extension(x509.SubjectAlternativeName(names), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(
                x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
            )
            .sign(ca_key, hashes.SHA256())
        )
        write(
            output / "server.key",
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            ),
        )
        write(output / "server.crt", cert.public_bytes(serialization.Encoding.PEM))
        write(output / "ca.crt", ca.public_bytes(serialization.Encoding.PEM))


class Pilot:
    def __init__(self, directory, port=8443, project=None, distributed_tls=False):
        self.directory = directory.resolve()
        if not self.directory.is_relative_to(ROOT / ".local"):
            raise ValueError(
                "Pilot working directories must be inside the ignored .local directory."
            )
        self.port = port
        self.distributed_tls = distributed_tls or (self.directory / "distributed-tls").exists()
        self.project = (
            project or "efqa-" + hashlib.sha256(str(self.directory).encode()).hexdigest()[:10]
        )

    def initialize(self, hostname):
        if self.directory.exists():
            return
        self.directory.mkdir(mode=0o700, parents=True)
        initialize(self.directory / "credentials")
        certificates(self.directory / "credentials/tls", hostname)
        (self.directory / "erp").mkdir(mode=0o700)
        if self.distributed_tls:
            node_certificates(self.directory / "credentials/tls")
            write(self.directory / "distributed-tls", "P8 TLS profile\n")

    def compose(self, *args, **kwargs):
        env = dict(os.environ)
        for line in (self.directory / "credentials/compose.env").read_text().splitlines():
            if line.strip():
                key, value = line.split("=", 1)
                env[key] = value
        env.update(QC_PILOT_DIR=self.directory.as_posix(), QC_HTTPS_PORT=str(self.port))
        files = [str(ROOT / "deploy/compose.pilot.yaml")]
        if self.distributed_tls:
            files.append(str(ROOT / "deploy/compose.distributed-tls.yaml"))
        compose_files = [part for file in files for part in ("-f", file)]
        return subprocess.run(
            [
                "docker",
                "compose",
                "-p",
                self.project,
                *compose_files,
                *args,
            ],
            cwd=ROOT,
            env=env,
            check=True,
            **kwargs,
        )

    def verify(self, redeliver=False):
        result = self.compose(
            "exec",
            "-T",
            "api",
            "python",
            "-m",
            "app.pilot_verify",
            *(["--redeliver"] if redeliver else []),
            capture_output=True,
        )
        return json.loads(result.stdout)

    def http(self, path, token=None, body=None):
        context = ssl.create_default_context(cafile=str(self.directory / "credentials/tls/ca.crt"))
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = Request(
            f"https://localhost:{self.port}" + path,
            headers=headers,
            data=json.dumps(body).encode() if body is not None else None,
        )
        with urlopen(req, context=context, timeout=30) as response:
            return json.load(response)

    def seed(self):
        dataset = json.loads((ROOT / "data/v2/source-events.json").read_text("utf-8"))
        tokens = json.loads((self.directory / "credentials/demo-tokens.json").read_text())
        for event_id in dataset["base_order"]:
            event = dataset["events"][event_id]
            self.http(
                "/api/v2/events",
                tokens["vision-demo" if event["source_id"] == "vision-emulator-1" else "line-demo"],
                event,
            )

    def backup(self, output, password):
        output.mkdir(mode=0o700, parents=False, exist_ok=False)
        self.compose("stop", "worker", "importer")
        try:
            # Freeze public writers before verifying and dumping; verifier runs in a one-off core.
            self.compose("stop", "web")
            if self.distributed_tls:
                self.compose("stop", "api-tls")
            evidence = self.verify()
            self.compose("stop", "api", "erp")
            with (output / "postgres.dump").open("xb") as stream:
                self.compose(
                    "exec",
                    "-T",
                    "postgres",
                    "pg_dump",
                    "-U",
                    "qc_admin",
                    "-Fc",
                    "final_hack",
                    stdout=stream,
                )
            shutil.copyfile(self.directory / "erp/erp.sqlite", output / "erp.sqlite")
            data = io.BytesIO()
            with tarfile.open(fileobj=data, mode="w") as archive:
                archive.add(self.directory / "credentials", arcname="credentials")
            salt, nonce = os.urandom(16), os.urandom(12)
            key = Scrypt(salt=salt, length=32, n=2**15, r=8, p=1).derive(password.encode())
            write(
                output / "secrets.aesgcm",
                salt + nonce + AESGCM(key).encrypt(nonce, data.getvalue(), b"efqa-pilot-backup-v1"),
            )
            manifest = {
                "format": 1,
                "distributed_tls": self.distributed_tls,
                "created_at": datetime.now(UTC).isoformat(),
                "evidence": evidence,
                "files": {
                    name: hashlib.sha256((output / name).read_bytes()).hexdigest()
                    for name in ("postgres.dump", "erp.sqlite", "secrets.aesgcm")
                },
            }
            write(output / "manifest.json", json.dumps(manifest, indent=2))
            for path in output.iterdir():
                path.chmod(0o600)
        finally:
            self.compose("start", "api", "erp", "worker", "web")

    def restore(self, backup, password):
        if self.directory.exists():
            raise ValueError("Restore requires a new isolated directory/project.")
        manifest = json.loads((backup / "manifest.json").read_text())
        self.distributed_tls = manifest.get("distributed_tls", False)
        if manifest["format"] != 1 or set(manifest["files"]) != {
            "postgres.dump",
            "erp.sqlite",
            "secrets.aesgcm",
        }:
            raise ValueError("Unsupported backup manifest.")
        for name, digest in manifest["files"].items():
            if hashlib.sha256((backup / name).read_bytes()).hexdigest() != digest:
                raise ValueError(f"Backup digest mismatch: {name}")
        blob = (backup / "secrets.aesgcm").read_bytes()
        key = Scrypt(salt=blob[:16], length=32, n=2**15, r=8, p=1).derive(password.encode())
        plain = AESGCM(key).decrypt(blob[16:28], blob[28:], b"efqa-pilot-backup-v1")
        self.directory.mkdir(mode=0o700, parents=True)
        if self.distributed_tls:
            write(self.directory / "distributed-tls", "P8 TLS profile\n")
        with tarfile.open(fileobj=io.BytesIO(plain), mode="r") as archive:
            for member in archive.getmembers():
                destination = (self.directory / member.name).resolve()
                if not destination.is_relative_to(self.directory / "credentials") or not (
                    member.isfile() or member.isdir()
                ):
                    raise ValueError("Invalid secret archive entry.")
            archive.extractall(self.directory, filter="data")
        (self.directory / "erp").mkdir(mode=0o700)
        shutil.copyfile(backup / "erp.sqlite", self.directory / "erp/erp.sqlite")
        self.compose(
            "build", "api", "erp", "web", *(["api-tls", "erp-tls"] if self.distributed_tls else [])
        )
        self.compose("up", "-d", "--wait", "postgres")
        with (backup / "postgres.dump").open("rb") as stream:
            self.compose(
                "exec",
                "-T",
                "postgres",
                "pg_restore",
                "-U",
                "qc_admin",
                "--no-owner",
                "--no-privileges",
                "--exit-on-error",
                "-d",
                "final_hack",
                stdin=stream,
            )
        self.compose(
            "up", "-d", "--wait", "api", "erp", *(["erp-tls"] if self.distributed_tls else [])
        )
        before = self.verify()
        if before["fingerprints"] != manifest["evidence"]["fingerprints"]:
            raise ValueError("Restored evidence differs from the backup manifest.")
        reconciled = self.verify(redeliver=True)
        if reconciled["fingerprints"] != before["fingerprints"]:
            raise ValueError("Reconciliation changed immutable evidence.")
        self.compose("up", "-d", "--wait")
        self.http("/health/ready")
        write(
            self.directory / "restore-report.json",
            json.dumps({"before": before, "after": reconciled}, indent=2),
        )
        return reconciled


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=["up", "down", "verify", "backup", "restore", "rotate-key", "rotate-identity"],
    )
    parser.add_argument("--directory", type=Path, default=ROOT / ".local/pilot")
    parser.add_argument("--port", type=int, default=8443)
    parser.add_argument("--hostname", default="localhost")
    parser.add_argument("--seed", action="store_true")
    parser.add_argument("--backup", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--actor")
    parser.add_argument("--distributed-tls", action="store_true")
    args = parser.parse_args()
    pilot = Pilot(args.directory, args.port, distributed_tls=args.distributed_tls)
    if args.command == "up":
        pilot.initialize(args.hostname)
        pilot.compose("up", "--build", "-d", "--wait", "--wait-timeout", "180")
        pilot.http("/health/ready")
        if args.seed:
            pilot.seed()
        print(
            f"HTTPS verified: https://localhost:{args.port}; "
            f"credentials: {pilot.directory / 'credentials'}"
        )
    elif args.command == "down":
        pilot.compose("down")
    elif args.command == "verify":
        print(json.dumps(pilot.verify(), indent=2))
    elif args.command in {"backup", "restore"}:
        password = os.environ.get("QC_BACKUP_PASSPHRASE")
        if not password:
            parser.error("Set QC_BACKUP_PASSPHRASE; it is never stored in the backup.")
        if args.command == "backup":
            if args.output is None:
                parser.error("backup requires --output pointing to a new directory")
            pilot.backup(args.output, password)
            print(f"Backup verified and written to {args.output}")
        else:
            if args.backup is None:
                parser.error("restore requires --backup")
            print(json.dumps(pilot.restore(args.backup, password), indent=2))
    elif args.command == "rotate-key":
        print(f"Active key version: {rotate_key(pilot.directory / 'credentials')}")
        pilot.compose("restart", "api", "worker")
    else:
        if not args.actor:
            parser.error("rotate-identity requires --actor")
        print(f"New credential: {rotate_identity(pilot.directory / 'credentials', args.actor)}")
        pilot.compose("restart", "api", "worker")


if __name__ == "__main__":
    main()
