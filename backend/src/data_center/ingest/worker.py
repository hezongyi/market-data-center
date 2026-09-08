from concurrent.futures import ThreadPoolExecutor

from data_center.domain.models import IngestJob
from data_center.ingest.service import run_fixture_ingest


class LocalWorker:
    def __init__(self, root, ledger):
        self.root = root
        self.ledger = ledger
        self.executor = ThreadPoolExecutor(max_workers=2)

    def submit(self, job: IngestJob):
        future = self.executor.submit(run_fixture_ingest, job, self.root, self.ledger)
        def finish(done):
            try:
                done.result()
            except Exception as exc:
                self.ledger.put(job.job_id, {"run_id": job.job_id, "job_id": job.job_id, "status": "failed", "error": str(exc)})
        future.add_done_callback(finish)
        return future
