from concurrent.futures import ThreadPoolExecutor

from data_center.domain.models import IngestJob
from data_center.ingest.service import run_fixture_ingest


class LocalWorker:
    def __init__(self, root, ledger):
        self.root = root
        self.ledger = ledger
        self.executor = ThreadPoolExecutor(max_workers=2)

    def submit(self, job: IngestJob):
        return self.executor.submit(run_fixture_ingest, job, self.root, self.ledger)

