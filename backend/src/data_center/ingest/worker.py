from data_center.domain.models import IngestJob
from data_center.ingest.service import run_fixture_ingest
from data_center.ingest.economic import run_fred_ingest
from concurrent.futures import ThreadPoolExecutor, TimeoutError


class LocalWorker:
    """Queue submitter and one-job executor used by the standalone worker process."""

    def __init__(self, root, ledger, timeout_seconds: float = 120.0):
        self.root = root
        self.ledger = ledger
        self.timeout_seconds = timeout_seconds
        self.ledger.recover_running_jobs()

    def submit(self, job: IngestJob):
        return self.ledger.enqueue_provider_bars(job.model_dump(mode="json"))

    def submit_economic(self, *, series_id: str, start: str | None = None, end: str | None = None) -> str:
        return self.ledger.enqueue_job({"job_id": f"fred-{series_id}", "dataset_id": "economic_observations", "provider": "fred", "series_id": series_id, "start": start, "end": end})

    def run_next(self) -> bool:
        self.ledger.heartbeat()
        claimed = self.ledger.claim_next_job()
        if claimed is None:
            return False
        try:
            payload = claimed["payload"]
            def execute():
                if payload["dataset_id"] == "economic_observations":
                    return run_fred_ingest(series_id=payload["series_id"], root=self.root, start=payload.get("start"), end=payload.get("end"), ledger=self.ledger, run_id=claimed["run_id"])
                return run_fixture_ingest(IngestJob.model_validate(payload), self.root, self.ledger, claimed["run_id"])
            with ThreadPoolExecutor(max_workers=1) as pool:
                pool.submit(execute).result(timeout=self.timeout_seconds)
            self.ledger.complete_job(claimed["job_id"])
        except TimeoutError:
            self.ledger.fail_job(claimed["job_id"], claimed["run_id"], "ingest timeout")
        except Exception as exc:
            self.ledger.fail_job(claimed["job_id"], claimed["run_id"], str(exc))
        return True
