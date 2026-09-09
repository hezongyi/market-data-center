#!/usr/bin/env python3
"""Run bounded provider acceptance checks and persist receipts."""
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend" / "src"))
from data_center.connectors.binance import BinanceConnector
from data_center.connectors.yfinance import YFinanceConnector
from data_center.connectors.fred import FredConnector
from data_center.domain.models import IngestJob

OUT = Path(os.getenv("DATACENTER_ACCEPTANCE_DIR", "acceptance-receipts"))

def run(name, fn):
    started = datetime.now(timezone.utc).isoformat()
    receipt = {"provider": name, "started_at": started, "status": "pass", "retryable": False}
    try:
        rows = fn()
        if not rows:
            raise RuntimeError("provider returned no rows")
        receipt.update(row_count=len(rows), status="pass")
    except Exception as exc:
        receipt.update(status="failed", error_type=type(exc).__name__, error=str(exc), retryable=True)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"{name}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    print(json.dumps(receipt, ensure_ascii=False))

now = datetime.now(timezone.utc)
job = lambda provider, symbol: IngestJob(job_id=f"acceptance-{provider}", provider=provider, symbol=symbol, asset_class="etf" if provider == "yfinance" else "crypto", start=(now - timedelta(days=14)).replace(hour=0, minute=0, second=0, microsecond=0), end=now.replace(hour=0, minute=0, second=0, microsecond=0))
run("binance", lambda: BinanceConnector().fetch_bars(job("binance", "BTCUSDT")))
run("yfinance", lambda: YFinanceConnector().fetch_bars(job("yfinance", "SPY")))
run("fred", lambda: FredConnector().fetch_observations("PAYEMS", start=(now - timedelta(days=30)).date().isoformat(), end=now.date().isoformat()))
