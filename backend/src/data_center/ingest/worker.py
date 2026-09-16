import fcntl
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

from data_center.catalog.manifest import (
    PublicationError,
    file_hash,
    manifest_path,
    validate_manifest,
    write_manifest,
)
from data_center.dataset_center import (
    DatasetCenter,
    DatasetStateError,
    managed_dataset_root,
)
from data_center.domain.models import DeriveJob, IngestJob
from data_center.observability import check_alerts


class LocalWorker:
    """Durable submitter and a single supervisor per ledger, guarded by flock."""

    def __init__(self, root, ledger, timeout_seconds=120.0, retry_delay_seconds=30.0, alert_sink=None,
                 capacity_policy=None):
        self.root = Path(root)
        self.ledger = ledger
        self.timeout_seconds = timeout_seconds
        self.retry_delay_seconds = retry_delay_seconds
        self.alert_sink = alert_sink
        self.capacity_policy = capacity_policy

    def submit(self, job: IngestJob):
        from data_center.platform import enqueue_ingest_plan

        run_ids = enqueue_ingest_plan(ledger=self.ledger, job=job)
        # Preserve the historical single-run API.  Multi-window callers can
        # use submit_many to retain every child run id.
        return run_ids[0]

    def submit_many(self, job: IngestJob) -> list[str]:
        from data_center.platform import enqueue_ingest_plan

        return enqueue_ingest_plan(ledger=self.ledger, job=job)

    def submit_economic(self, *, series_id, start=None, end=None, schema_version="economic_observations.v2"):
        return self.ledger.enqueue_job({"job_id": f"fred-{series_id}", "dataset_id": "economic_observations",
                                        "provider": "fred", "series_id": series_id, "start": start, "end": end,
                                        "schema_version": schema_version})

    def submit_derive(self, job: DeriveJob) -> str:
        from data_center.catalog.snapshot import Catalog, snapshot_reference
        from data_center.platform_registry import REGISTRY

        recipe = REGISTRY.recipe(job.recipe_id, job.recipe_version)
        snapshot = Catalog(self.root).resolve(
            recipe.input_dataset,
            {"provider": job.provider, "symbol": job.symbol, "timeframe": recipe.source_timeframe},
        )
        if not snapshot.parts:
            raise ValueError("derive input snapshot is empty")
        if job.input_snapshot_id is not None and job.input_snapshot_id != snapshot.snapshot_id:
            raise ValueError("derive input snapshot does not match current catalog")
        # Persist the parts this execution is accepted against.  The row stays
        # one shared record per snapshot, and the run only carries its id, so a
        # later publication cannot invalidate the accepted input (spec 6.3).
        input_id = self.ledger.store_production_input(snapshot_reference(self.root, snapshot))
        # The fixed-input id is carried beside the model dump, not inside the
        # model: a pydantic dump would silently drop an undeclared field and the
        # run would fall back to comparing against the current catalog.
        payload = {**job.model_dump(mode="json"), "input_snapshot_id": snapshot.snapshot_id,
                   "input_id": input_id}
        return self.ledger.enqueue_job(payload)

    @contextmanager
    def _ownership(self):
        with self.ledger.path.with_suffix(".worker.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield None
                return
            yield lock

    def _command(self, directory):
        return [sys.executable, "-m", "data_center.ingest.process", str(directory)]

    def _publication_root(self, managed_dataset_id: str | None) -> Path:
        return managed_dataset_root(self.root, managed_dataset_id)

    def _ensure_managed_writable(self, managed_dataset_id: str | None) -> None:
        if managed_dataset_id is None:
            return
        try:
            item = DatasetCenter(self.root).get(managed_dataset_id)
        except (KeyError, DatasetStateError) as exc:
            raise PublicationError("managed dataset ownership is unavailable") from exc
        if item.status == "archived":
            raise PublicationError("archived managed dataset is not writable")

    def _publish(self, directory, receipt, managed_dataset_id=None):
        staged_root = directory / "parts"
        managed_id = managed_dataset_id or receipt.get("managed_dataset_id")
        publication_root = self._publication_root(managed_id)
        if not manifest_path(publication_root, receipt["run_id"]).exists():
            self._ensure_managed_writable(managed_id)
        manifest = json.loads(manifest_path(staged_root, receipt["run_id"]).read_text())
        paths = validate_manifest(staged_root, manifest)
        if any(manifest[key] != receipt[key] for key in
               ("run_id", "dataset_id", "schema_version", "row_count", "quality_summary")):
            raise PublicationError("receipt does not match staged manifest")
        if {Path(p).resolve() for p in receipt.get("paths", [receipt.get("path")])} != {p.resolve() for p in paths}:
            raise PublicationError("receipt part list does not match staged manifest")
        published = []
        for source, item in zip(paths, manifest["parts"]):
            target = publication_root / item["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            # Copy to a fresh inode: staged files must not mutate a published part.
            if not target.exists():
                with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as stream:
                    temporary = Path(stream.name)
                    with source.open("rb") as input_stream:
                        shutil.copyfileobj(input_stream, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                try:
                    if target.exists():
                        if file_hash(target) != item["sha256"]:
                            raise PublicationError("existing part differs from staged result")
                    else:
                        os.rename(temporary, target)
                        temporary = None
                finally:
                    if temporary is not None:
                        temporary.unlink(missing_ok=True)
            elif file_hash(target) != item["sha256"]:
                raise PublicationError("existing part differs from staged result")
            published.append(str(target))
        # Same bytes on every recovery; generated_at comes from the staged manifest.
        published_manifest = write_manifest(publication_root, manifest)
        return {**receipt, **({"paths": published} if "paths" in receipt else {"path": published[0]}),
                "manifest": str(published_manifest)}

    def _finding_records(self, claimed: dict, payload: dict) -> list[dict]:
        """Attach run and selector linkage to every finding the run produced."""
        summary = payload.get("quality_summary") if isinstance(payload.get("quality_summary"), dict) else {}
        selector = {key: (claimed.get("payload") or {}).get(key)
                    for key in ("provider", "symbol", "timeframe", "series_id")}
        selector = {key: value for key, value in selector.items() if value}
        records = []
        for finding in summary.get("findings") or []:
            records.append({**finding, "run_id": claimed["run_id"],
                            "job_id": (claimed.get("payload") or {}).get("job_id"),
                            "dataset_id": payload.get("dataset_id") or (claimed.get("payload") or {}).get("dataset_id"),
                            "selector": selector})
        return records

    def _persist_findings(self, claimed: dict, payload: dict) -> None:
        records = self._finding_records(claimed, payload)
        if records:
            self.ledger.record_findings(records)

    def _recover(self):
        # The child inherits the lock so a replacement cannot recover a live child.
        for job in self.ledger.running_jobs():
            managed_id = (job.get("payload") or {}).get("managed_dataset_id")
            execution_root = self._publication_root(managed_id)
            directory = execution_root / ".ingest-staging" / job["run_id"] / str(job["attempts"])
            legacy_directory = self.root / ".ingest-staging" / job["run_id"] / str(job["attempts"])
            if not directory.exists() and legacy_directory.exists():
                directory = legacy_directory
            result_path = directory / "result.json"
            result = json.loads(result_path.read_text()) if result_path.exists() else {}
            if "verification" in result:
                # A verification run owns no canonical part; publish nothing.
                payload = result["verification"]
                self.ledger.finish_job(job["job_id"], job["run_id"], payload)
                self._persist_findings(job, payload)
            elif "receipt" in result:
                try:
                    pending_receipt = {**result["receipt"], "managed_dataset_id":
                                       (job.get("payload") or {}).get("managed_dataset_id")}
                    receipt = self._publish(directory, pending_receipt)
                    self.ledger.finish_job(job["job_id"], job["run_id"], receipt)
                    self._persist_findings(job, receipt)
                except PublicationError:
                    if manifest_path(execution_root, job["run_id"]).exists():
                        raise
                    self.ledger.fail_job(job["job_id"], job["run_id"], "publication validation failed",
                                         error_type="PublicationError", failure_stage="publish", retryable=False)
            else:
                self.ledger.fail_job(job["job_id"], job["run_id"], "worker interrupted", error_type="WorkerInterrupted",
                                     failure_stage="recovery", delay_seconds=self.retry_delay_seconds)

    def run_next(self):
        with self._ownership() as lock:
            if lock is None:
                return False
            self._recover()
            self.ledger.heartbeat()
            if self.alert_sink is not None:
                check_alerts(self.ledger, self.alert_sink, canonical_root=self.root,
                             capacity_policy=self.capacity_policy)
            if self.capacity_policy is not None and self.capacity_policy.inspect(self.root).status == "critical":
                return False
            claimed = self.ledger.claim_next_job()
            if claimed is None:
                return False
            execution_root = managed_dataset_root(
                self.root, (claimed.get("payload") or {}).get("managed_dataset_id"))
            managed_id = (claimed.get("payload") or {}).get("managed_dataset_id")
            try:
                self._ensure_managed_writable(managed_id)
            except PublicationError as exc:
                self.ledger.fail_job(
                    claimed["job_id"], claimed["run_id"], str(exc),
                    error_type="PublicationError", failure_stage="ownership", retryable=False)
                self.ledger.heartbeat()
                return True
            directory = execution_root / ".ingest-staging" / claimed["run_id"] / str(claimed["attempts"])
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "request.json").write_text(json.dumps({
                **claimed, "canonical_root": str(execution_root),
                # The child reads its fixed input from the ledger read-only; it
                # never opens it for writing (spec 6.3).
                "ledger_path": str(self.ledger.path)}))
            started = time.monotonic()
            process = None
            result = {}
            try:
                process = subprocess.Popen(self._command(directory), start_new_session=True,
                                           pass_fds=(lock.fileno(),), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                while process.poll() is None:
                    if time.monotonic() - started >= self.timeout_seconds:
                        raise TimeoutError("ingest timeout")
                    self.ledger.heartbeat()
                    time.sleep(min(0.2, self.timeout_seconds / 10))
                result = json.loads((directory / "result.json").read_text())
                if "verification" in result:
                    payload = result["verification"]
                    self.ledger.finish_job(claimed["job_id"], claimed["run_id"], payload)
                    self._persist_findings(claimed, payload)
                elif "receipt" in result:
                    pending_receipt = {**result["receipt"], "managed_dataset_id":
                                       (claimed.get("payload") or {}).get("managed_dataset_id")}
                    receipt = self._publish(directory, pending_receipt)
                    self.ledger.finish_job(claimed["job_id"], claimed["run_id"], receipt)
                    self._persist_findings(claimed, receipt)
                else:
                    self.ledger.fail_job(claimed["job_id"], claimed["run_id"], result["error"],
                                         error_type=result["error_type"], failure_stage=result.get("failure_stage", "execute"), retryable=result["retryable"],
                                         quality_summary=result.get("quality_summary"),
                                         delay_seconds=self.retry_delay_seconds)
                    self._persist_findings(claimed, {"dataset_id": claimed["payload"].get("dataset_id"),
                                                     "quality_summary": result.get("quality_summary")})
            except Exception as exc:
                if process is not None and process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                if isinstance(exc, PublicationError):
                    publication_root = self._publication_root(managed_id)
                    if manifest_path(publication_root, claimed["run_id"]).exists():
                        raise
                    self.ledger.fail_job(claimed["job_id"], claimed["run_id"], "publication validation failed",
                                         error_type="PublicationError", failure_stage="publish", retryable=False)
                    self.ledger.heartbeat()
                    return True
                if "receipt" in result or "verification" in result:
                    # Preserve the staged result for recovery after publication/ledger failure.
                    raise
                self.ledger.fail_job(claimed["job_id"], claimed["run_id"], "ingest execution failed",
                                     error_type=type(exc).__name__, failure_stage="supervise", delay_seconds=self.retry_delay_seconds)
            finally:
                if process is not None and process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
            self.ledger.heartbeat()
            if self.alert_sink is not None:
                check_alerts(self.ledger, self.alert_sink, canonical_root=self.root,
                             capacity_policy=self.capacity_policy)
            print(json.dumps({"event": "ingest_finished", "run_id": claimed["run_id"],
                              "request_id": claimed["payload"].get("request_id"), "attempt": claimed["attempts"],
                              "job_id": claimed["payload"]["job_id"], "provider": claimed["payload"].get("provider"),
                              "status": self.ledger.get(claimed["run_id"])["status"],
                              "duration_seconds": round(time.monotonic() - started, 3)}), flush=True)
            return True
