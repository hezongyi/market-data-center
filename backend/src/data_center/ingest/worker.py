from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

from data_center.domain.models import IngestJob
from data_center.ingest.service import run_fixture_ingest


class LocalWorker:
    def __init__(self, root, ledger):
        self.root = root
        self.ledger = ledger
        self.executor = ThreadPoolExecutor(max_workers=2)

    def submit(self, job: IngestJob):
        run_id = str(uuid4())
        self.ledger.put(run_id, {"run_id": run_id, "job_id": job.job_id, "dataset_id": job.dataset_id, "status": "queued"})
        future = self.executor.submit(self._run, job, run_id)
        future.run_id = run_id
        def finish(done):
            try:
                done.result()
            except Exception as exc:
                self.ledger.update(run_id, status="failed", error=str(exc))
        future.add_done_callback(finish)
        return future

    def _run(self, job: IngestJob, run_id: str):
        self.ledger.update(run_id, status="running")
        return run_fixture_ingest(job, self.root, self.ledger, run_id)
