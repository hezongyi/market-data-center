import json
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

from data_center.catalog.manifest import build_manifest, manifest_path, write_manifest
from data_center.connectors.fred import FredConnector
from data_center.domain.schema import validate_economic_observations
from data_center.lineage import compact_source_hashes
from data_center.quality.checks import check_economic_observations
from data_center.quality.errors import QualityError
from data_center.storage.economic import write_economic_observations

ECONOMIC_OBSERVATIONS_SCHEMA_VERSION = "economic_observations.v1"
ECONOMIC_PIT_SCHEMA_VERSION = "economic_observations.v2"


def default_fred_connector() -> FredConnector:
    """Build the configured FRED connector.

    Isolated acceptance runs point the client at a local fixture through the
    same settings path production uses, so a test never needs a credential or a
    real provider call.
    """
    import os

    from data_center.settings import Settings

    settings = Settings()
    allowed = {item.strip() for item in os.getenv("DATACENTER_PROVIDER_ALLOWLIST", "").split(",") if item.strip()}
    if allowed and "fred" not in allowed:
        raise ValueError("provider disabled in this environment: fred")
    overrides = {}
    if settings.fred_endpoint:
        overrides["endpoint"] = settings.fred_endpoint
    if settings.fred_metadata_endpoint:
        overrides["metadata_endpoint"] = settings.fred_metadata_endpoint
    return FredConnector(**overrides)


def run_fred_ingest(*, series_id: str, root: Path, connector: FredConnector | None = None, start: str | None = None,
                    end: str | None = None, ledger=None, run_id: str | None = None,
                    schema_version: str = ECONOMIC_PIT_SCHEMA_VERSION, run_kind: str = "ingest",
                    run_scope: str = "production") -> dict:
    run_id = run_id or str(uuid4())
    resolved_connector = connector or default_fred_connector()
    rows = resolved_connector.fetch_observations(series_id, start=start, end=end)
    if not rows:
        raise ValueError("FRED returned no observations")
    for row in rows:
        row["asof_ts"] = row["ingest_ts"]
        if schema_version == ECONOMIC_PIT_SCHEMA_VERSION:
            row["source"] = "fred"
            row["missing_reason"] = None
            if row.get("value") is None:
                row["missing_reason"] = "provider_missing"
        row["source_hash"] = sha256(json.dumps(row, sort_keys=True).encode()).hexdigest()
    validate_economic_observations(rows, schema_version=schema_version)
    findings = check_economic_observations(rows)
    if findings:
        if ledger is not None:
            ledger.add_findings([{**finding, "run_id": run_id, "dataset_id": "economic_observations", "series_id": series_id} for finding in findings])
        raise QualityError(f"economic quality check failed: {findings[0]['code']}", findings)
    path = write_economic_observations(root, rows, part_id=run_id)
    output_hash = sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()
    input_hash = sha256(json.dumps({"series_id": series_id, "start": start, "end": end}, sort_keys=True).encode()).hexdigest()
    source_lineage = compact_source_hashes(row["source_hash"] for row in rows)
    payload = {"run_id": run_id, "dataset_id": "economic_observations", "schema_version": schema_version,
               "series_id": series_id, "provider": "fred", "run_kind": run_kind, "run_scope": run_scope,
               "connector_version": getattr(resolved_connector, "version", "1"), "input_hash": input_hash,
               "status": "pass", "row_count": len(rows), "min_date": min(r["observation_date"] for r in rows),
               "max_date": max(r["observation_date"] for r in rows), "path": str(path), "paths": [str(path)],
               "manifest": str(manifest_path(root, run_id)), "output_hash": output_hash,
               "lineage": {"input_kind": "provider_request", "source_snapshot_id": None,
                           **source_lineage},
               "quality_summary": {"status": "pass", "finding_count": 0, "findings": []},
               "created_at": datetime.now(timezone.utc).isoformat()}
    write_manifest(root, build_manifest(
        root, run_id=run_id, dataset_id=payload["dataset_id"], schema_version=payload["schema_version"],
        paths=[path], row_count=len(rows), quality_summary=payload["quality_summary"],
        lineage=payload["lineage"], run_kind=run_kind, run_scope=run_scope,
    ))
    if ledger is not None:
        ledger.put(payload["run_id"], payload)
    return payload
