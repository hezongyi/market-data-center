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
        from data_center.catalog.snapshot import Catalog
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
        payload = job.model_copy(update={"input_snapshot_id": snapshot.snapshot_id}).model_dump(mode="json")
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

    def _publish(self, directory, receipt):
        staged_root = directory / "parts"
        manifest = json.loads(manifest_path(staged_root, receipt["run_id"]).read_text())
        paths = validate_manifest(staged_root, manifest)
        if any(manifest[key] != receipt[key] for key in
               ("run_id", "dataset_id", "schema_version", "row_count", "quality_summary")):
            raise PublicationError("receipt does not match staged manifest")
        if {Path(p).resolve() for p in receipt.get("paths", [receipt.get("path")])} != {p.resolve() for p in paths}:
            raise PublicationError("receipt part list does not match staged manifest")
        published = []
        for source, item in zip(paths, manifest["parts"]):
            target = self.root / item["path"]
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
        published_manifest = write_manifest(self.root, manifest)
        return {**receipt, **({"paths": published} if "paths" in receipt else {"path": published[0]}),
                "manifest": str(published_manifest)}

    def _recover(self):
        # The child inherits the lock so a replacement cannot recover a live child.
        for job in self.ledger.running_jobs():
            directory = self.root / ".ingest-staging" / job["run_id"] / str(job["attempts"])
            result_path = directory / "result.json"
            result = json.loads(result_path.read_text()) if result_path.exists() else {}
            if "receipt" in result:
                try:
                    receipt = self._publish(directory, result["receipt"])
                    self.ledger.finish_job(job["job_id"], job["run_id"], receipt)
                except PublicationError:
                    if manifest_path(self.root, job["run_id"]).exists():
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
            directory = self.root / ".ingest-staging" / claimed["run_id"] / str(claimed["attempts"])
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "request.json").write_text(json.dumps({**claimed, "canonical_root": str(self.root)}))
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
                if "receipt" in result:
                    receipt = self._publish(directory, result["receipt"])
                    self.ledger.finish_job(claimed["job_id"], claimed["run_id"], receipt)
                else:
                    self.ledger.fail_job(claimed["job_id"], claimed["run_id"], result["error"],
                                         error_type=result["error_type"], failure_stage=result.get("failure_stage", "execute"), retryable=result["retryable"],
                                         quality_summary=result.get("quality_summary"),
                                         delay_seconds=self.retry_delay_seconds)
            except Exception as exc:
                if process is not None and process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                if isinstance(exc, PublicationError) and not manifest_path(self.root, claimed["run_id"]).exists():
                    self.ledger.fail_job(claimed["job_id"], claimed["run_id"], "publication validation failed",
                                         error_type="PublicationError", failure_stage="publish", retryable=False)
                    self.ledger.heartbeat()
                    return True
                if "receipt" in result:
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
