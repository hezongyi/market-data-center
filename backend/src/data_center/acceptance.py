"""Scheduled production acceptance through the public API; receipts and local alerts."""
import argparse
import fcntl
import gzip
import json
import os
import platform
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import requests

from data_center.observability import AlertSink
from data_center.settings import Settings


def evidence_context() -> dict:
    repo_root = Path(__file__).resolve().parents[3]
    try:
        commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo_root, text=True,
                                         stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.CalledProcessError):
        commit = os.getenv("GIT_COMMIT", "unknown")
    return {"commit": commit, "python": platform.python_version(), "host": platform.node()}


def archive_receipts(root: Path, keep_days: int = 90):
    if keep_days < 90:
        raise ValueError("evidence retention must be at least 90 days")
    cutoff = time.time() - keep_days * 86400
    archived = []
    for path in root.glob("receipt-*.json"):
        if path.stat().st_mtime >= cutoff:
            continue
        target = root / "archive" / (path.name + ".gz")
        target.parent.mkdir(exist_ok=True)
        data = path.read_bytes()
        if target.exists():
            if gzip.decompress(target.read_bytes()) != data:
                raise ValueError("archive differs from receipt")
        else:
            with target.open("xb") as stream:
                stream.write(gzip.compress(data, mtime=0))
        if gzip.decompress(target.read_bytes()) != data:
            raise ValueError("archive verification failed")
        path.unlink()
        archived.append(target.name)
    return archived


def run_acceptance(base_url, root, interval_seconds=3600, spacing_seconds=5, deadline_seconds=480):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(root, 0o700)
    except OSError:
        pass
    with (root / ".lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"status": "skipped", "reason": "acceptance_busy"}
        last = root / "last-attempt"
        if last.exists() and time.time() - float(last.read_text()) < interval_seconds:
            return {"status": "skipped", "reason": "minimum_interval"}
        last.write_text(str(time.time()))
        session = requests.Session()
        session.trust_env = False
        if os.getenv("DATACENTER_API_KEY"):
            session.headers["X-API-Key"] = os.environ["DATACENTER_API_KEY"]

        def call(method, path, **kwargs):
            response = session.request(method, base_url.rstrip("/") + "/api/v1" + path, timeout=10, **kwargs)
            response.raise_for_status()
            return response.json()["data"]

        now = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        report = {"acceptance_id": uuid4().hex, "checked_at": datetime.now(timezone.utc).isoformat(),
                  "validation_command": "python -m data_center.acceptance", "base_url": base_url,
                  "environment": evidence_context(),
                  "status": "pass", "providers": []}
        providers = (("binance", "BTCUSDT", "crypto"), ("yfinance", "SPY", "etf"),
                     ("dukascopy", "EURUSD", "fx"), ("fred", "PAYEMS", None))
        for index, (provider, symbol, asset_class) in enumerate(providers):
            if index:
                time.sleep(spacing_seconds)
            result = {"provider": provider, "symbol": symbol}
            try:
                if provider == "fred":
                    query = {"series_id": symbol, "start": (now - timedelta(days=90)).date().replace(day=1).isoformat(),
                             "end": now.date().isoformat(), "run_scope": "acceptance"}
                    submitted = call("POST", "/economic/ingest", params=query)
                    read_path = "/economic/observations"
                else:
                    query = {"provider": provider, "symbol": symbol, "timeframe": "1d",
                             "start": (now - timedelta(days=14)).isoformat(), "end": now.isoformat()}
                    submitted = call("POST", "/ingest/runs", json={**query, "job_id": "acceptance-" + report["acceptance_id"],
                                                                    "run_scope": "acceptance",
                                      "asset_class": asset_class})
                    read_path = "/bars"
                result["run_id"] = submitted["run_id"]
                deadline = time.monotonic() + deadline_seconds
                while True:
                    receipt = call("GET", "/runs/" + submitted["run_id"])
                    if receipt["status"] in {"pass", "failed", "dead_letter"}:
                        break
                    if time.monotonic() >= deadline:
                        raise TimeoutError("run polling deadline")
                    time.sleep(1)
                result["receipt"] = receipt
                quality = receipt.get("quality_summary") or {}
                if receipt["status"] != "pass" or not receipt.get("row_count") or quality.get("status") != "pass":
                    raise ValueError("non-pass receipt")
                rows = call("GET", read_path, params=query)
                if len(rows) < receipt["row_count"]:
                    raise ValueError("API readback incomplete")
                started = datetime.fromisoformat(receipt["started_at"])
                fresh = [row for row in rows if datetime.fromisoformat(row["ingest_ts"].replace("Z", "+00:00")) >= started]
                if len(fresh) < receipt["row_count"]:
                    raise ValueError("API readback does not include newly ingested rows")
                result.update(status="pass", read_count=len(rows), fresh_read_count=len(fresh))
            except Exception as exc:  # noqa: BLE001 - acceptance must record every provider failure
                report["status"] = "failed"
                result.update(status="failed", error_type=type(exc).__name__)
            report["providers"].append(result)
        path = root / ("receipt-" + report["acceptance_id"] + ".json")
        path.write_text(json.dumps(report, indent=2))
        if report["status"] != "pass":
            settings = Settings()
            AlertSink(settings.evidence_root / "alerts", settings.alerts_enabled).emit(
                "provider_acceptance_failed", identity=report["acceptance_id"],
                fields={"receipt": str(path), "status": "failed",
                        "providers": [r["provider"] for r in report["providers"] if r["status"] != "pass"]})
            with (root / "alerts.jsonl").open("a") as stream:
                stream.write(json.dumps({"event": "provider_acceptance_failed", "at": report["checked_at"],
                                         "receipt": path.name, "providers": [r["provider"] for r in report["providers"] if r["status"] != "pass"]}) + "\n")
        report["archived_receipts"] = archive_receipts(root)
        session.close()
        return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:18380")
    parser.add_argument("--output", type=Path, default=Settings().evidence_root / "acceptance")
    parser.add_argument("--interval-seconds", type=float, default=3600)
    args = parser.parse_args()
    report = run_acceptance(args.base_url, args.output, args.interval_seconds)
    print(json.dumps({key: value for key, value in report.items() if key != "providers"}), flush=True)
    raise SystemExit(1 if report["status"] == "failed" else 0)


if __name__ == "__main__":
    main()
