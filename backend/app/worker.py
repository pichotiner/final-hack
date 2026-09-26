"""Run with python -m app.worker [--once]."""

import argparse
import json
import os
import time
from pathlib import Path

from app.adapters.erp_http import HttpErpReceiver
from app.application.bootstrap import ProductionSettings, build_runtime
from app.application.outbox import OutboxDeliveryWorker


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--message-id", help="Deliver one specific persisted message when due.")
    args = parser.parse_args()
    token = Path(os.environ["QC_ERP_TOKEN_FILE"]).read_text(encoding="utf-8").strip()
    receiver = HttpErpReceiver(os.environ["QC_ERP_RESULTS_URL"], token)
    runtime = build_runtime(ProductionSettings.from_environment(os.environ))
    worker = OutboxDeliveryWorker(runtime.service.journal, receiver)
    try:
        while True:
            result = worker.deliver_once(args.message_id)
            if result is not None:
                print(json.dumps(result), flush=True)
            if args.once:
                break
            if result is None:
                time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        receiver.close()
        runtime.close()


if __name__ == "__main__":
    main()
