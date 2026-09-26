"""Generate Pydantic DTOs from the two OpenAPI sources; --check compares in temp storage."""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    output_dir = ROOT / "generated"
    output_dir.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="efqa-dto-") as temp:
        for name, output in [
            ("openapi.yaml", "models.py"),
            ("external-openapi.yaml", "external.py"),
        ]:
            dest = Path(temp) / output if args.check else output_dir / output
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "datamodel_code_generator",
                    "--input",
                    name,
                    "--input-file-type",
                    "openapi",
                    "--output",
                    str(dest),
                    "--output-model-type",
                    "pydantic_v2.BaseModel",
                    "--target-python-version",
                    "3.11",
                    "--disable-timestamp",
                    "--use-standard-collections",
                    "--use-union-operator",
                    "--enum-field-as-literal",
                    "all",
                    "--field-constraints",
                    "--use-double-quotes",
                ],
                cwd=ROOT,
                check=True,
            )
            if args.check and dest.read_bytes() != (output_dir / output).read_bytes():
                raise SystemExit(f"Outdated generated DTO: {output}")
    print(
        "Pydantic generation matches sources"
        if args.check
        else "Pydantic DTOs generated"
    )


if __name__ == "__main__":
    main()
