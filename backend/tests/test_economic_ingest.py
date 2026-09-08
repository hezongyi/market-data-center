from data_center.ingest.economic import run_fred_ingest
from data_center.runs.ledger import RunLedger


class FakeFredConnector:
    def fetch_observations(self, series_id, start=None, end=None):
        return [{"series_id": series_id, "provider": "fred", "observation_date": "2026-01-01", "value": 123.0, "vintage_start": "2026-01-01", "vintage_end": "9999-12-31", "availability_policy": "fred_realtime_vintage", "ingest_ts": "2026-01-02T00:00:00+00:00"}]


def test_fred_ingest_writes_receipt_and_canonical_part(tmp_path) -> None:
    ledger = RunLedger(tmp_path / "audit.sqlite")
    receipt = run_fred_ingest(series_id="PAYEMS", root=tmp_path / "lake", connector=FakeFredConnector(), ledger=ledger)
    assert receipt["status"] == "pass"
    assert receipt["schema_version"] == "economic_observations.v1"
    assert receipt["output_hash"]
    assert ledger.get(receipt["run_id"]) == receipt
