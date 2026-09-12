"""Real-network Dukascopy acceptance through the durable worker boundary."""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from data_center.catalog.manifest import immutable_json
from data_center.domain.models import DeriveJob, IngestJob
from data_center.evidence import operation_receipt
from data_center.ingest.worker import LocalWorker
from data_center.runs.ledger import RunLedger
from data_center.settings import Settings
from data_center.storage.query import query_market_bars, query_provider_bars


def _await(worker: LocalWorker, ledger: RunLedger, run_id: str) -> dict:
    for _ in range(3):
        worker.run_next()
        receipt = ledger.get(run_id)
        if receipt["status"] in {"pass", "failed", "dead_letter"}:
            return receipt
    return ledger.get(run_id)


def run(*, root: Path, output: Path, symbol: str, start: datetime, end: datetime) -> dict:
    production_root = Settings().canonical_root.resolve()
    root = root.resolve()
    if root == production_root or production_root in root.parents or root in production_root.parents:
        raise ValueError("real acceptance requires an isolated non-production canonical root")
    if list((root / ".manifests").glob("*.json")):
        raise ValueError("real acceptance canonical root must not contain existing manifests")
    if output.exists():
        raise ValueError("real acceptance receipt path must not already exist")
    started = datetime.now(timezone.utc).isoformat()
    ledger = RunLedger(root / "audit" / "acceptance.sqlite")
    worker = LocalWorker(root, ledger, timeout_seconds=90, retry_delay_seconds=0)
    try:
        raw_run_id = worker.submit(IngestJob(
            job_id=f"real-dukascopy-{symbol.lower()}-1m", provider="dukascopy", symbol=symbol,
            asset_class="fx", timeframe="1m", start=start, end=end,
            run_scope="acceptance", run_kind="backfill",
        ))
        raw = _await(worker, ledger, raw_run_id)
        if raw["status"] != "pass":
            raise RuntimeError(f"raw worker run did not pass: {raw.get('error_type')}")
        raw_rows = query_provider_bars(root, provider="dukascopy", symbol=symbol, timeframe="1m")
        if not raw_rows or {row["price_type"] for row in raw_rows} != {"bid"}:
            raise RuntimeError("raw BID readback failed")
        derive_run_id = worker.submit_derive(DeriveJob(
            job_id=f"real-dukascopy-{symbol.lower()}-5m", provider="dukascopy", symbol=symbol,
            recipe_id="utc-24x7-1m-to-5m-ohlcv", recipe_version="1",
            start=start, end=end, run_scope="acceptance",
        ))
        derived = _await(worker, ledger, derive_run_id)
        if derived["status"] != "pass":
            raise RuntimeError(f"derived worker run did not pass: {derived.get('error_type')}")
        derived_rows = query_market_bars(
            root, provider="dukascopy", symbol=symbol, timeframe="5m", price_basis="bid",
            recipe_id="utc-24x7-1m-to-5m-ohlcv", recipe_version="1",
        )
        expected = int((end - start) / timedelta(minutes=5))
        if len(derived_rows) != expected:
            raise RuntimeError("derived readback row count mismatch")
        details = {
            "network_mode": "real", "provider": "dukascopy", "symbol": symbol,
            "canonical_root": str(root), "requested_start": start.isoformat(),
            "requested_end": end.isoformat(), "raw_run_id": raw_run_id,
            "raw_row_count": len(raw_rows), "raw_price_basis": "bid",
            "raw_manifest": raw["manifest"], "input_snapshot_id": derived["input_snapshot_id"],
            "derive_run_id": derive_run_id, "derived_row_count": len(derived_rows),
            "derived_manifest": derived["manifest"], "coverage": derived["coverage"],
        }
        receipt = operation_receipt(
            action="real_dukascopy_platform_acceptance",
            command="scripts/real_dukascopy_acceptance.py", started_at=started,
            result="pass", details=details,
        )
    except Exception as exc:  # noqa: BLE001 - acceptance must persist a safe failure receipt
        receipt = operation_receipt(
            action="real_dukascopy_platform_acceptance",
            command="scripts/real_dukascopy_acceptance.py", started_at=started,
            result="failed", failure_stage="real_provider_pipeline",
            error_category=type(exc).__name__, details={
                "network_mode": "real", "provider": "dukascopy", "symbol": symbol,
                "canonical_root": str(root), "requested_start": start.isoformat(),
                "requested_end": end.isoformat(),
            },
        )
    immutable_json(output, receipt)
    return receipt


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--canonical-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--symbol", default="EURUSD")
    parser.add_argument("--start", type=datetime.fromisoformat,
                        default=datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc))
    parser.add_argument("--end", type=datetime.fromisoformat,
                        default=datetime(2026, 9, 10, 12, 10, tzinfo=timezone.utc))
    args = parser.parse_args()
    receipt = run(root=args.canonical_root, output=args.output, symbol=args.symbol.upper(),
                  start=args.start, end=args.end)
    print(json.dumps({"result": receipt["result"], "receipt": str(args.output)}, sort_keys=True))
    raise SystemExit(0 if receipt["result"] == "pass" else 1)


if __name__ == "__main__":
    main()
