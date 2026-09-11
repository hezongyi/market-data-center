"""Deterministic Dukascopy D3 safe-failure acceptance for immutable releases."""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

from data_center.connectors.dukascopy import DukascopyConnector
from data_center.deployment import runtime_identity
from data_center.domain.models import IngestJob
from data_center.evidence import operation_receipt, utc_now, write_receipt
from data_center.ingest.process import safe_failure_result
from data_center.settings import Settings

SECRET_MARKERS = ("credential-for-safety-test", "provider.example/private", "/private/runtime/path")


def _frame(*timestamps: str) -> pd.DataFrame:
    return pd.DataFrame(
        [[1.0, 2.0, 0.5, 1.5, 10.0] for _ in timestamps],
        columns=["open", "high", "low", "close", "volume"],
        index=pd.to_datetime(list(timestamps)),
    )


def _job(**overrides) -> IngestJob:
    payload = {
        "job_id": "dukascopy-d3-safe-failure",
        "provider": "dukascopy",
        "symbol": "EURUSD",
        "asset_class": "fx",
        "timeframe": "1d",
        "start": datetime(2026, 9, 1, tzinfo=timezone.utc),
        "end": datetime(2026, 9, 4, tzinfo=timezone.utc),
        "run_scope": "acceptance",
    }
    return IngestJob(**{**payload, **overrides})


def _raise(exc: Exception):
    def fetch(**_kwargs):
        raise exc

    return fetch


def run_failure_matrix(evidence_root: Path, manifest_path: Path) -> dict:
    started = utc_now()
    identity = runtime_identity(manifest_path)
    empty = pd.DataFrame(columns=["open", "high", "low", "close", "volume"],
                         index=pd.DatetimeIndex([], tz="UTC"))
    duplicate = _frame("2026-09-01T00:00:00Z", "2026-09-01T00:00:00Z")
    unordered = _frame("2026-09-02T00:00:00Z", "2026-09-01T00:00:00Z")
    missing_ohlc = _frame("2026-09-01T00:00:00Z")
    missing_ohlc.loc[:, "open"] = float("nan")
    cases = {
        "empty": (lambda **_kwargs: empty, _job(), "ValueError"),
        "timeout": (_raise(requests.Timeout(
            "https://provider.example/private?token=credential-for-safety-test /private/runtime/path"
        )), _job(), "Timeout"),
        "http_status": (_raise(requests.HTTPError(
            "503 https://provider.example/private?token=credential-for-safety-test"
        )), _job(), "HTTPError"),
        "duplicate": (lambda **_kwargs: duplicate, _job(), "ValueError"),
        "out_of_order": (lambda **_kwargs: unordered, _job(), "ValueError"),
        "missing_ohlc": (lambda **_kwargs: missing_ohlc, _job(), "ValueError"),
        "unsupported_selector": (lambda **_kwargs: _frame("2026-09-01T00:00:00Z"),
                                 _job(symbol="BAD"), "ValueError"),
    }
    results = []
    result = "pass"
    for name, (fetch, job, expected_type) in cases.items():
        try:
            DukascopyConnector(fetch=fetch, now_func=lambda: datetime(2026, 9, 10, tzinfo=timezone.utc)).fetch_bars(job)
        except Exception as exc:  # noqa: BLE001 - acceptance verifies all provider boundaries
            safe = safe_failure_result(exc)
            serialized = json.dumps(safe, sort_keys=True)
            passed = safe["error_type"] == expected_type and not any(marker in serialized for marker in SECRET_MARKERS)
            results.append({"case": name, "status": "pass" if passed else "failed",
                            "error_type": safe["error_type"], "failure_stage": safe["failure_stage"],
                            "retryable": safe["retryable"], "safe_error": safe["error"]})
            if not passed:
                result = "failed"
        else:
            results.append({"case": name, "status": "failed", "error_type": None})
            result = "failed"
    receipt = operation_receipt(
        action="dukascopy_d3_failure_matrix",
        command="python -m data_center.dukascopy_d3",
        started_at=started,
        result=result,
        failure_stage=None if result == "pass" else "safe_failure_matrix",
        details={"deployment_identity": identity, "run_scope": "acceptance",
                 "network_mode": "deterministic_no_network", "publication_attempted": False,
                 "cases": results},
    )
    path = write_receipt(evidence_root, receipt)
    return {**receipt, "receipt": str(path) if path else None}


def main() -> None:
    settings = Settings()
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence-root", type=Path, default=settings.evidence_root)
    parser.add_argument("--deployment-manifest", type=Path,
                        default=settings.deployment_manifest or os.getenv("DATACENTER_DEPLOYMENT_MANIFEST"))
    args = parser.parse_args()
    if args.deployment_manifest is None:
        parser.error("an immutable deployment manifest is required")
    report = run_failure_matrix(args.evidence_root, args.deployment_manifest)
    print(json.dumps(report), flush=True)
    raise SystemExit(0 if report["result"] == "pass" else 1)


if __name__ == "__main__":
    main()
