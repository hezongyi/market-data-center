from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
from uuid import uuid4

from data_center.connectors.fred import FredConnector
from data_center.storage.economic import write_economic_observations


def run_fred_ingest(*, series_id: str, root: Path, connector: FredConnector | None = None, start: str | None = None, end: str | None = None, ledger=None) -> dict:
    rows = (connector or FredConnector()).fetch_observations(series_id, start=start, end=end)
    if not rows:
        raise ValueError("FRED returned no observations")
    for row in rows:
        row.update({"availability_policy": "provider_release", "asof_ts": row["ingest_ts"], "source_hash": sha256(json.dumps(row, sort_keys=True).encode()).hexdigest()})
    path = write_economic_observations(root, rows)
    payload = {"run_id": str(uuid4()), "dataset_id": "economic_observations", "series_id": series_id, "provider": "fred", "status": "pass", "row_count": len(rows), "min_date": min(r["observation_date"] for r in rows), "max_date": max(r["observation_date"] for r in rows), "path": str(path), "created_at": datetime.now(timezone.utc).isoformat()}
    if ledger is not None:
        ledger.put(payload["run_id"], payload)
    return payload

