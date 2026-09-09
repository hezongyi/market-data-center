import json
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

from data_center.catalog.manifest import build_manifest, manifest_path, write_manifest
from data_center.connectors.registry import get_connector
from data_center.domain.models import IngestJob
from data_center.domain.schema import SCHEMA_VERSION, validate_provider_bars
from data_center.quality.checks import check_provider_bars
from data_center.quality.errors import QualityError
from data_center.storage.parquet import write_provider_bars


def run_fixture_ingest(job: IngestJob, root: Path, ledger=None, run_id: str | None = None) -> dict:
    resolved_run_id = run_id or str(uuid4())
    connector = get_connector(job.provider)
    rows = connector.fetch_bars(job)
    validate_provider_bars(rows)
    findings = check_provider_bars(rows)
    if findings:
        raise QualityError(f"provider quality check failed: {findings[0]['code']}", findings)
    paths = write_provider_bars(root, rows, part_id=resolved_run_id)
    output_hash = sha256(json.dumps([row.model_dump(mode="json") for row in rows], sort_keys=True).encode()).hexdigest()
    input_hash = sha256(job.model_dump_json().encode()).hexdigest()
    payload = {"run_id": resolved_run_id, "job_id": job.job_id, "status": "pass", "dataset_id": job.dataset_id, "schema_version": SCHEMA_VERSION, "provider": job.provider, "connector_version": getattr(connector, "version", "1"), "input_hash": input_hash, "row_count": len(rows), "min_ts": min(row.bar_ts for row in rows).isoformat(), "max_ts": max(row.bar_ts for row in rows).isoformat(), "paths": [str(path) for path in paths], "manifest": str(manifest_path(root, resolved_run_id)), "output_hash": output_hash, "quality_summary": {"status": "pass", "finding_count": 0, "findings": []}, "created_at": datetime.now(timezone.utc).isoformat()}
    write_manifest(root, build_manifest(root, run_id=resolved_run_id, dataset_id=payload["dataset_id"],
                                       schema_version=payload["schema_version"], paths=paths,
                                       row_count=len(rows), quality_summary=payload["quality_summary"]))
    if ledger is not None:
        ledger.put(payload["run_id"], payload)
    return payload
