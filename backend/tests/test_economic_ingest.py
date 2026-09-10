from data_center.ingest.economic import run_fred_ingest
from data_center.runs.ledger import RunLedger


class FakeFredConnector:
    def fetch_observations(self, series_id, start=None, end=None):
        return [{"series_id": series_id, "provider": "fred", "observation_date": "2026-01-01", "release_ts": None, "asof_ts": "2026-01-02T00:00:00+00:00", "value": 123.0, "frequency": "Monthly", "units": "Thousands of Persons", "seasonal_adjustment": "Seasonally Adjusted", "vintage_start": "2026-01-01", "vintage_end": "9999-12-31", "availability_policy": "realtime_vintage", "availability_lag_days": None, "ingest_ts": "2026-01-02T00:00:00+00:00"}]


def test_fred_ingest_writes_receipt_and_canonical_part(tmp_path) -> None:
    ledger = RunLedger(tmp_path / "audit.sqlite")
    receipt = run_fred_ingest(series_id="PAYEMS", root=tmp_path / "lake", connector=FakeFredConnector(), ledger=ledger)
    assert receipt["status"] == "pass"
    assert receipt["quality_summary"] == {"status": "pass", "finding_count": 0, "findings": []}
    assert receipt["schema_version"] == "economic_observations.v2"
    assert receipt["output_hash"]
    assert receipt["connector_version"] == "1"
    assert receipt["input_hash"]
    assert ledger.get(receipt["run_id"]) == receipt


def test_fred_ingest_v2_records_source_and_missing_semantics(tmp_path) -> None:
    ledger = RunLedger(tmp_path / "audit.sqlite")
    receipt = run_fred_ingest(series_id="PAYEMS", root=tmp_path / "lake", connector=FakeFredConnector(),
                              ledger=ledger, schema_version="economic_observations.v2")
    assert receipt["schema_version"] == "economic_observations.v2"
    assert receipt["paths"] == [receipt["path"]]
    import polars as pl
    rows = pl.read_parquet(receipt["path"]).to_dicts()
    assert rows[0]["source"] == "fred"
    assert rows[0]["missing_reason"] is None


def test_fred_ingest_v2_preserves_provider_missing_reason(tmp_path) -> None:
    class MissingFredConnector(FakeFredConnector):
        def fetch_observations(self, series_id, start=None, end=None):
            row = super().fetch_observations(series_id, start, end)[0]
            row["value"] = None
            return [row]

    receipt = run_fred_ingest(series_id="PAYEMS", root=tmp_path / "lake", connector=MissingFredConnector(),
                              schema_version="economic_observations.v2")
    import polars as pl
    row = pl.read_parquet(receipt["path"]).to_dicts()[0]
    assert row["value"] is None and row["missing_reason"] == "provider_missing"
