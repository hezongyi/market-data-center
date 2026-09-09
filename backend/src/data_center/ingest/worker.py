import fcntl
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

from data_center.domain.models import IngestJob


class LocalWorker:
    """Durable submitter and a single supervisor per ledger, guarded by flock."""

    def __init__(self, root, ledger, timeout_seconds=120.0, retry_delay_seconds=30.0):
        self.root = Path(root)
        self.ledger = ledger
        self.timeout_seconds = timeout_seconds
        self.retry_delay_seconds = retry_delay_seconds

    def submit(self, job: IngestJob):
        return self.ledger.enqueue_job(job.model_dump(mode="json"))

    def submit_economic(self, *, series_id, start=None, end=None):
        return self.ledger.enqueue_job({"job_id": f"fred-{series_id}", "dataset_id": "economic_observations",
                                        "provider": "fred", "series_id": series_id, "start": start, "end": end})

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
        paths = receipt.get("paths") or [receipt["path"]]
        published = []
        for value in paths:
            source = Path(value)
            relative = source.resolve().relative_to(staged_root.resolve())
            target = self.root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.link(source, target)
            except FileExistsError:
                if hashlib.sha256(source.read_bytes()).digest() != hashlib.sha256(target.read_bytes()).digest():
                    raise ValueError("existing part differs from staged result")
            published.append(str(target))
        return {**receipt, **({"paths": published} if "paths" in receipt else {"path": published[0]})}

    def _recover(self):
        # The child inherits the lock so a replacement cannot recover a live child.
        for job in self.ledger.running_jobs():
            directory = self.root / ".ingest-staging" / job["run_id"] / str(job["attempts"])
            result_path = directory / "result.json"
            result = json.loads(result_path.read_text()) if result_path.exists() else {}
            if "receipt" in result:
                receipt = self._publish(directory, result["receipt"])
                self.ledger.finish_job(job["job_id"], job["run_id"], receipt)
            else:
                self.ledger.fail_job(job["job_id"], job["run_id"], "worker interrupted", error_type="WorkerInterrupted",
                                     failure_stage="recovery", delay_seconds=self.retry_delay_seconds)

    def run_next(self):
        with self._ownership() as lock:
            if lock is None:
                return False
            self._recover()
            self.ledger.heartbeat()
            claimed = self.ledger.claim_next_job()
            if claimed is None:
                return False
            directory = self.root / ".ingest-staging" / claimed["run_id"] / str(claimed["attempts"])
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "request.json").write_text(json.dumps(claimed))
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
            print(json.dumps({"event": "ingest_finished", "run_id": claimed["run_id"],
                              "request_id": claimed["payload"].get("request_id"), "attempt": claimed["attempts"],
                              "status": self.ledger.get(claimed["run_id"])["status"],
                              "duration_seconds": round(time.monotonic() - started, 3)}), flush=True)
            return True
