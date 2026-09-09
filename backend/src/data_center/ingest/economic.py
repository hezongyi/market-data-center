import json
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

from data_center.catalog.manifest import build_manifest, manifest_path, write_manifest
from data_center.connectors.fred import FredConnector
from data_center.domain.schema import validate_economic_observations
from data_center.quality.checks import check_economic_observations
from data_center.quality.errors import QualityError
from data_center.storage.economic import write_economic_observations

ECONOMIC_OBSERVATIONS_SCHEMA_VERSION = "economic_observations.v1"


def run_fred_ingest(*, series_id: str, root: Path, connector: FredConnector | None = None, start: str | None = None, end: str | None = None, ledger=None, run_id: str | None = None) -> dict:
    run_id = run_id or str(uuid4())
    resolved_connector = connector or FredConnector()
    rows = resolved_connector.fetch_observations(series_id, start=start, end=end)
    if not rows:
        raise ValueError("FRED returned no observations")
    for row in rows:
        row.update({"asof_ts": row["ingest_ts"], "source_hash": sha256(json.dumps(row, sort_keys=True).encode()).hexdigest()})
    validate_economic_observations(rows)
    findings = check_economic_observations(rows)
    if findings:
        if ledger is not None:
            ledger.add_findings([{**finding, "run_id": run_id, "dataset_id": "economic_observations", "series_id": series_id} for finding in findings])
        raise QualityError(f"economic quality check failed: {findings[0]['code']}", findings)
    path = write_economic_observations(root, rows, part_id=run_id)
    output_hash = sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()
    input_hash = sha256(json.dumps({"series_id": series_id, "start": start, "end": end}, sort_keys=True).encode()).hexdigest()
    payload = {"run_id": run_id, "dataset_id": "economic_observations", "schema_version": ECONOMIC_OBSERVATIONS_SCHEMA_VERSION, "series_id": series_id, "provider": "fred", "connector_version": getattr(resolved_connector, "version", "1"), "input_hash": input_hash, "status": "pass", "row_count": len(rows), "min_date": min(r["observation_date"] for r in rows), "max_date": max(r["observation_date"] for r in rows), "path": str(path), "manifest": str(manifest_path(root, run_id)), "output_hash": output_hash, "quality_summary": {"status": "pass", "finding_count": 0, "findings": []}, "created_at": datetime.now(timezone.utc).isoformat()}
    write_manifest(root, build_manifest(root, run_id=run_id, dataset_id=payload["dataset_id"],
                                       schema_version=payload["schema_version"], paths=[path],
                                       row_count=len(rows), quality_summary=payload["quality_summary"]))
    if ledger is not None:
        ledger.put(payload["run_id"], payload)
    return payload
