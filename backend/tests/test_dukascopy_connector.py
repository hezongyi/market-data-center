import json
import lzma
import struct
from datetime import datetime, timezone
from pathlib import Path

import dukascopy_python
import pandas as pd
import pytest

from data_center.connectors.dukascopy import DukascopyConnector
from data_center.connectors.registry import CONNECTORS
from data_center.domain.models import IngestJob
from data_center.dukascopy_d3 import SECRET_MARKERS, run_failure_matrix
from data_center.ingest.process import safe_failure_result
from data_center.ingest.service import run_fixture_ingest
from data_center.storage.query import query_provider_bars


def frame(*timestamps: str) -> pd.DataFrame:
    return pd.DataFrame(
        [[1.0 + index, 2.0 + index, 0.5 + index, 1.5 + index, 10.0 + index]
         for index, _ in enumerate(timestamps)],
        columns=["open", "high", "low", "close", "volume"],
        index=pd.to_datetime(list(timestamps)),
    )


def job(**overrides) -> IngestJob:
    values = {
        "job_id": "dukascopy-eurusd",
        "provider": "dukascopy",
        "symbol": "EURUSD",
        "asset_class": "fx",
        "timeframe": "1d",
        "start": datetime(2026, 9, 1, tzinfo=timezone.utc),
        "end": datetime(2026, 9, 3, tzinfo=timezone.utc),
    }
    return IngestJob(**{**values, **overrides})


def test_dukascopy_connector_returns_half_open_bid_bars() -> None:
    captured = {}

    def fetch(**kwargs):
        captured.update(kwargs)
        return frame("2026-09-01T00:00:00Z", "2026-09-02T00:00:00Z", "2026-09-03T00:00:00Z")

    rows = DukascopyConnector(fetch=fetch).fetch_bars(job())

    assert captured["instrument"] == "EUR/USD"
    assert captured["interval"] == "1DAY"
    assert captured["offer_side"] == "B"
    assert captured["start"] == datetime(2026, 9, 1, tzinfo=timezone.utc).replace(tzinfo=None)
    assert captured["end"] == datetime(2026, 9, 3, tzinfo=timezone.utc).replace(tzinfo=None)
    assert captured["max_retries"] == 0
    assert [row.bar_ts for row in rows] == [
        datetime(2026, 9, 1, tzinfo=timezone.utc),
        datetime(2026, 9, 2, tzinfo=timezone.utc),
    ]
    assert all(row.provider == "dukascopy" and row.price_type == "bid" for row in rows)
    assert all(row.symbol == "EURUSD" and row.currency == "USD" for row in rows)
    assert len({row.source_hash for row in rows}) == 2


def test_dukascopy_connector_governs_http_timeout_and_proxy(monkeypatch) -> None:
    captured = {}

    class Response:
        def raise_for_status(self):
            captured["status_checked"] = True

    def get(url, **kwargs):
        captured.update(url=url, **kwargs)
        return Response()

    def fetch(**_kwargs):
        dukascopy_python.requests.get("https://provider.example")
        return frame("2026-09-01T00:00:00Z")

    monkeypatch.setattr(dukascopy_python.requests, "get", get)
    monkeypatch.setenv("DUKASCOPY_PROXY_URL", "http://proxy.example:7890")

    DukascopyConnector(fetch=fetch, request_timeout_seconds=7).fetch_bars(job(end=datetime(2026, 9, 2, tzinfo=timezone.utc)))

    assert captured["timeout"] == 7
    assert captured["proxies"] == {
        "http": "http://proxy.example:7890",
        "https": "http://proxy.example:7890",
    }
    assert captured["status_checked"] is True


def test_dukascopy_connector_uses_official_bi5_for_eurusd_minute(monkeypatch) -> None:
    requested = {}
    payload = lzma.compress(b"".join([
        struct.pack(">IIIff", 1_000, 110_010, 110_000, 1.0, 2.0),
        struct.pack(">IIIff", 30_000, 110_030, 110_020, 1.0, 3.0),
        struct.pack(">IIIff", 61_000, 110_020, 110_010, 1.0, 4.0),
    ]))

    class Response:
        content = payload

        def raise_for_status(self):
            requested["status_checked"] = True

    def get(url, **kwargs):
        requested.update(url=url, kwargs=kwargs)
        return Response()

    monkeypatch.setattr(dukascopy_python.requests, "get", get)
    connector = DukascopyConnector(fetch=lambda **_: pytest.fail("library endpoint must not be used"))
    requests_claimed = []

    rows = connector.fetch_bars(job(
        timeframe="1m",
        start=datetime(2026, 9, 14, 2, tzinfo=timezone.utc),
        end=datetime(2026, 9, 14, 3, tzinfo=timezone.utc),
    ), request_guard=lambda: requests_claimed.append(True))

    assert requested["url"].endswith("/EURUSD/2026/08/14/02h_ticks.bi5")
    assert requested["status_checked"] is True
    assert len(requests_claimed) == 1
    assert [row.bar_ts for row in rows] == [
        datetime(2026, 9, 14, 2, 0, tzinfo=timezone.utc),
        datetime(2026, 9, 14, 2, 1, tzinfo=timezone.utc),
    ]
    assert (rows[0].open, rows[0].high, rows[0].low, rows[0].close, rows[0].volume) == (
        1.1, 1.1002, 1.1, 1.1002, 5.0,
    )


def test_dukascopy_bi5_rejects_invalid_payload(monkeypatch) -> None:
    class Response:
        content = b"not-lzma"

        def raise_for_status(self):
            pass

    monkeypatch.setattr(dukascopy_python.requests, "get", lambda *_args, **_kwargs: Response())
    connector = DukascopyConnector(fetch=lambda **_: pytest.fail("library endpoint must not be used"))

    with pytest.raises(ValueError, match="not valid LZMA"):
        connector.fetch_bars(job(
            timeframe="1m",
            start=datetime(2026, 9, 14, 2, tzinfo=timezone.utc),
            end=datetime(2026, 9, 14, 3, tzinfo=timezone.utc),
        ))


def test_dukascopy_bi5_supports_partial_multi_hour_windows(monkeypatch) -> None:
    requested = []

    class Response:
        def __init__(self, hour):
            self.content = lzma.compress(b"".join([
                struct.pack(">IIIff", 15 * 60_000, 110_010, 110_000 + hour, 1.0, 2.0),
                struct.pack(">IIIff", 45 * 60_000, 110_020, 110_010 + hour, 1.0, 3.0),
            ]))

        def raise_for_status(self):
            pass

    def get(url, **_kwargs):
        requested.append(url)
        return Response(int(url.rsplit("/", 1)[-1][:2]))

    monkeypatch.setattr(dukascopy_python.requests, "get", get)
    rows = DukascopyConnector(fetch=lambda **_: pytest.fail("library endpoint must not be used")).fetch_bars(job(
        timeframe="1m",
        start=datetime(2026, 9, 14, 2, 15, tzinfo=timezone.utc),
        end=datetime(2026, 9, 14, 3, 16, tzinfo=timezone.utc),
    ))

    assert [url.rsplit("/", 1)[-1] for url in requested] == ["02h_ticks.bi5", "03h_ticks.bi5"]
    assert [row.bar_ts for row in rows] == [
        datetime(2026, 9, 14, 2, 15, tzinfo=timezone.utc),
        datetime(2026, 9, 14, 2, 45, tzinfo=timezone.utc),
        datetime(2026, 9, 14, 3, 15, tzinfo=timezone.utc),
    ]


def test_dukascopy_connector_rejects_unbounded_minute_request() -> None:
    with pytest.raises(ValueError, match="exceeds bounded range"):
        DukascopyConnector(fetch=lambda **_: frame("2026-01-01T00:00:00Z")).fetch_bars(
            job(
                timeframe="1m",
                start=datetime(2026, 1, 1, tzinfo=timezone.utc),
                end=datetime(2026, 2, 2, tzinfo=timezone.utc),
            )
        )


def test_dukascopy_connector_rejects_invalid_provider_payload() -> None:
    duplicate = frame("2026-09-01T00:00:00Z", "2026-09-01T00:00:00Z")
    with pytest.raises(ValueError, match="duplicate timestamps"):
        DukascopyConnector(fetch=lambda **_: duplicate).fetch_bars(job())


def test_dukascopy_ingest_publishes_manifest_and_readback(tmp_path, monkeypatch) -> None:
    root = tmp_path / "lake"
    connector = DukascopyConnector(
        fetch=lambda **_: frame("2026-09-01T00:00:00Z", "2026-09-02T00:00:00Z")
    )
    monkeypatch.setitem(CONNECTORS, "dukascopy", connector)

    receipt = run_fixture_ingest(job(), root, run_id="dukascopy-ingest")

    assert receipt["status"] == "pass"
    assert receipt["provider"] == "dukascopy"
    assert receipt["connector_version"] == "dukascopy-python-4.0.1-official-bi5-bid-v3"
    assert receipt["row_count"] == 2
    manifest = json.loads(Path(receipt["manifest"]).read_text())
    assert manifest["dataset_id"] == "provider_bars"
    assert manifest["schema_version"] == "provider_bars.v1"
    rows = query_provider_bars(root, provider="dukascopy", symbol="EURUSD", timeframe="1d")
    assert len(rows) == 2
    assert {row["price_type"] for row in rows} == {"bid"}


def test_worker_safe_failure_result_never_persists_provider_details() -> None:
    import requests

    marker = "https://provider.example/private?token=credential-for-safety-test"
    result = safe_failure_result(requests.Timeout(marker))
    assert result["error_type"] == "Timeout"
    assert result["error"] == "ingest failed"
    assert marker not in json.dumps(result)


def test_dukascopy_d3_failure_matrix_is_complete_and_safe(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("data_center.dukascopy_d3.runtime_identity", lambda _path: {
        "deployment_id": "release-1", "software_version": "0.2.0", "source_commit": "a" * 40,
    })
    report = run_failure_matrix(tmp_path, tmp_path / "deployment.json")
    cases = report["details"]["cases"]
    assert report["result"] == "pass"
    assert {case["case"] for case in cases} == {
        "empty", "timeout", "http_status", "duplicate", "out_of_order", "missing_ohlc",
        "unsupported_selector",
    }
    assert all(case["status"] == "pass" for case in cases)
    serialized = json.dumps(report)
    assert all(marker not in serialized for marker in SECRET_MARKERS)
