from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
from uuid import uuid4

from data_center.connectors.fixture import fetch_bars
from data_center.domain.models import IngestJob
from data_center.storage.parquet import write_provider_bars


def run_fixture_ingest(job: IngestJob, root: Path, ledger=None) -> dict:
    rows = fetch_bars(job)
    path = write_provider_bars(root, rows)
    output_hash = sha256(json.dumps([row.model_dump(mode="json") for row in rows], sort_keys=True).encode()).hexdigest()
    payload = {"run_id": str(uuid4()), "job_id": job.job_id, "status": "pass", "dataset_id": job.dataset_id, "row_count": len(rows), "path": str(path), "output_hash": output_hash, "created_at": datetime.now(timezone.utc).isoformat()}
    if ledger is not None:
        ledger.put(payload["run_id"], payload)
    return payload
