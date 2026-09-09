from data_center.domain.models import IngestJob
from data_center.ingest.service import run_fixture_ingest
from data_center.ingest.economic import run_fred_ingest


class LocalWorker:
    """Queue submitter and one-job executor used by the standalone worker process."""

    def __init__(self, root, ledger):
        self.root = root
        self.ledger = ledger

    def submit(self, job: IngestJob):
        return self.ledger.enqueue_provider_bars(job.model_dump(mode="json"))

    def submit_economic(self, *, series_id: str, start: str | None = None, end: str | None = None) -> str:
        return self.ledger.enqueue_job({"job_id": f"fred-{series_id}", "dataset_id": "economic_observations", "provider": "fred", "series_id": series_id, "start": start, "end": end})

    def run_next(self) -> bool:
        claimed = self.ledger.claim_next_job()
        if claimed is None:
            return False
        try:
            payload = claimed["payload"]
            if payload["dataset_id"] == "economic_observations":
                run_fred_ingest(series_id=payload["series_id"], root=self.root, start=payload.get("start"), end=payload.get("end"), ledger=self.ledger, run_id=claimed["run_id"])
            else:
                job = IngestJob.model_validate(payload)
                run_fixture_ingest(job, self.root, self.ledger, claimed["run_id"])
            self.ledger.complete_job(claimed["job_id"])
        except Exception as exc:
            self.ledger.fail_job(claimed["job_id"], claimed["run_id"], str(exc))
        return True
