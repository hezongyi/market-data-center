"""Read-only retention audit and bounded daily backfill via the ingest API."""
import argparse
import json
import os
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

from data_center.settings import Settings


def retention_audit(root: Path, keep_days=30):
    cutoff = time.time() - keep_days * 86400
    canonical = {"files": 0, "bytes": 0, "older_than_days": keep_days, "old_files": 0, "old_bytes": 0}
    staging = {"files": 0, "bytes": 0}
    for path in root.rglob("*.parquet"):
        stat = path.stat()
        target = staging if ".ingest-staging" in path.relative_to(root).parts else canonical
        target["files"] += 1
        target["bytes"] += stat.st_size
        if target is canonical and stat.st_mtime < cutoff:
            target["old_files"] += 1
            target["old_bytes"] += stat.st_size
    return {"event": "retention_audit", "checked_at": datetime.now(timezone.utc).isoformat(),
            "policy": "audit_only_no_deletion", "canonical": canonical, "staging": staging}


def daily_chunks(start: date, end: date):
    if end <= start or (end - start).days > 3660:
        raise ValueError("backfill requires 0 < end-start <= 3660 days")
    while start < end:
        stop = min(start + timedelta(days=365), end)
        yield start, stop
        start = stop


def backfill(base_url, provider, symbol, asset_class, start, end, output):
    chunks = list(daily_chunks(start, end))
    report = {"status": "running", "provider": provider, "symbol": symbol, "runs": []}
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x") as stream:
        json.dump(report, stream)
    with requests.Session() as session:
        session.trust_env = False
        if os.getenv("DATACENTER_API_KEY"):
            session.headers["X-API-Key"] = os.environ["DATACENTER_API_KEY"]

        def call(method, path, **kwargs):
            response = session.request(method, base_url.rstrip("/") + "/api/v1" + path, timeout=15, **kwargs)
            response.raise_for_status()
            return response.json()["data"]

        try:
            for first, last in chunks:
                job = {"job_id": "backfill-" + output.stem, "provider": provider, "symbol": symbol,
                       "asset_class": asset_class, "timeframe": "1d",
                       "start": first.isoformat() + "T00:00:00Z", "end": last.isoformat() + "T00:00:00Z"}
                receipt = call("POST", "/ingest/runs", json=job)
                report["runs"].append({"request": job, "receipt": receipt})
                output.write_text(json.dumps(report, indent=2))
                deadline = time.monotonic() + 480
                while receipt["status"] in {"queued", "running"}:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("backfill polling deadline; inspect recorded run before resubmitting")
                    time.sleep(1)
                    receipt = call("GET", "/runs/" + receipt["run_id"])
                report["runs"][-1]["receipt"] = receipt
                output.write_text(json.dumps(report, indent=2))
                if receipt["status"] != "pass" or not receipt.get("row_count"):
                    raise ValueError("backfill run did not pass")
                time.sleep(5)
            report["status"] = "pass"
        except Exception as exc:
            report.update(status="failed", error_type=type(exc).__name__)
            raise
        finally:
            output.write_text(json.dumps(report, indent=2))
    return report


def main():
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    audit = commands.add_parser("retention-audit")
    audit.add_argument("--root", type=Path, default=Settings().canonical_root)
    fill = commands.add_parser("backfill")
    fill.add_argument("--base-url", default="http://127.0.0.1:18380")
    fill.add_argument("--provider", choices=["binance", "yfinance", "fixture"], required=True)
    fill.add_argument("--symbol", required=True)
    fill.add_argument("--asset-class", required=True)
    fill.add_argument("--start", type=date.fromisoformat, required=True)
    fill.add_argument("--end", type=date.fromisoformat, required=True)
    fill.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "retention-audit":
        print(json.dumps(retention_audit(args.root)), flush=True)
    else:
        report = backfill(args.base_url, args.provider, args.symbol, args.asset_class, args.start, args.end, args.output)
        print(json.dumps({"status": report["status"], "run_count": len(report["runs"])}), flush=True)


if __name__ == "__main__":
    main()
