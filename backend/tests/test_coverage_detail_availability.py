"""Detailed coverage says when it was not computed, and why (issue #85).

The governance fields (readiness, gap count, ready intervals) are only produced for one selector shape.
Every other shape used to answer with a bare summary, which a console renders as "not published" - the
same thing it renders for "checked and healthy". The response now names the reason, so the difference
between "the platform did not evaluate this" and "the data is fine" is explicit.
"""
from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from data_center.api.app import create_app
from data_center.settings import Settings


def client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(Settings(
        canonical_root=tmp_path / "lake", ledger_path=tmp_path / "ledger.sqlite",
        evidence_root=tmp_path / "evidence", auth_cookie_secure=False,
    )))


def test_unrelated_selector_reports_that_details_were_not_computed(tmp_path: Path) -> None:
    payload = client(tmp_path).get("/api/v1/provider-bars/coverage", params={
        "provider": "binance", "symbol": "BTCUSDT", "timeframe": "1d",
    }).json()["data"]

    assert "readiness_status" not in payload
    assert payload["coverage_detail_unavailable"] == (
        "detailed coverage is computed for provider=dukascopy timeframe=1m with start and end; "
        "binance 1d answers with the summary only"
    )


def test_dukascopy_window_still_asks_for_the_window(tmp_path: Path) -> None:
    payload = client(tmp_path).get("/api/v1/provider-bars/coverage", params={
        "provider": "dukascopy", "symbol": "EURUSD", "timeframe": "1m",
    }).json()["data"]

    assert payload["coverage_scope"] == "summary"
    assert payload["readiness_status"] == "unknown"
    assert payload["coverage_detail_unavailable"] == (
        "detailed coverage requires start and end, because readiness is only evaluated over the "
        "requested window"
    )


def test_dukascopy_window_without_published_rows_explains_the_summary(tmp_path: Path) -> None:
    payload = client(tmp_path).get("/api/v1/provider-bars/coverage", params={
        "provider": "dukascopy", "symbol": "EURUSD", "timeframe": "1m",
        "start": "2026-01-01T00:00:00+00:00", "end": "2026-01-02T00:00:00+00:00",
    }).json()["data"]

    assert payload["coverage_detail_unavailable"] == (
        "no canonical rows are published for this selector, so readiness cannot be evaluated"
    )
