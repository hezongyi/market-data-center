import time

from data_center.ingest.worker import LocalWorker
from data_center.runs.ledger import RunLedger
from data_center.settings import Settings


def main() -> None:
    print("market-data-center worker ready", flush=True)
    settings = Settings()
    worker = LocalWorker(settings.canonical_root, RunLedger(settings.ledger_path))
    while True:
        if not worker.run_next():
            time.sleep(1)


if __name__ == "__main__":
    main()
