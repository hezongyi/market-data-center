from data_center.domain.models import IngestJob
from data_center.ingest.service import run_fixture_ingest


class LocalWorker:
    """Queue submitter and one-job executor used by the standalone worker process."""

    def __init__(self, root, ledger):
        self.root = root
        self.ledger = ledger

    def submit(self, job: IngestJob):
        return self.ledger.enqueue_provider_bars(job.model_dump(mode="json"))

    def run_next(self) -> bool:
        claimed = self.ledger.claim_next_job()
        if claimed is None:
            return False
        try:
            job = IngestJob.model_validate(claimed["payload"])
            run_fixture_ingest(job, self.root, self.ledger, claimed["run_id"])
            self.ledger.complete_job(claimed["job_id"])
        except Exception as exc:
            self.ledger.fail_job(claimed["job_id"], claimed["run_id"], str(exc))
        return True
