"""Pull external work orders into the existing PostgreSQL production core."""

import argparse
import json
import os
from pathlib import Path

from sqlalchemy.exc import SQLAlchemyError

from app.adapters.erp_orders import ErpContract, HttpWorkOrders, WorkOrderMapper, strict_document
from app.application.bootstrap import ProductionSettings, build_runtime
from app.domain.errors import QualityError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, help="Full /external/v2/work-orders URL")
    parser.add_argument("--contract", required=True, type=Path, help="Role 2 external-openapi.yaml")
    parser.add_argument("--profile", required=True, type=Path)
    parser.add_argument("--erp-token-file", required=True, type=Path)
    parser.add_argument("--integration-token-file", required=True, type=Path)
    args = parser.parse_args()
    runtime, reader = None, None
    try:
        contract = ErpContract(args.contract)
        mapper = WorkOrderMapper(strict_document(args.profile.read_bytes()), contract)
        token = args.integration_token_file.read_text(encoding="utf-8").strip()
        runtime = build_runtime(ProductionSettings.from_environment(os.environ))
        runtime.service._require(token, {"integration"})
        reader = HttpWorkOrders(
            args.url,
            args.erp_token_file.read_text(encoding="utf-8").strip(),
            contract,
            verify=os.environ.get("QC_ERP_CA_FILE", True),
        )
        counts = {"accepted": 0, "duplicate": 0}
        for order in reader.orders():
            result = runtime.service.import_work_order(token, order, mapper)
            counts[result["status"]] += 1
            # Each line reports only a completed transaction, even if a later page fails.
            print(json.dumps(result, ensure_ascii=False), flush=True)
        print(json.dumps({"completed": True, **counts}), flush=True)
        return 0
    except QualityError as error:
        print(json.dumps({"completed": False, "error": error.code}), flush=True)
        return 1
    except (OSError, ValueError, SQLAlchemyError):
        # Do not echo configuration, remote response bodies, connection strings or credentials.
        print(json.dumps({"completed": False, "error": "import_unavailable"}), flush=True)
        return 1
    finally:
        if reader is not None:
            reader.close()
        if runtime is not None:
            runtime.close()


if __name__ == "__main__":
    raise SystemExit(main())
