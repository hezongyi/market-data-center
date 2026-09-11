"""Real-environment consumer flag cutover and rollback acceptance."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import time
from pathlib import Path

import requests

from data_center.deployment import runtime_identity
from data_center.evidence import operation_receipt, utc_now, write_receipt


def _hash(rows: list[dict]) -> str:
    return hashlib.sha256(json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True,
                                   stderr=subprocess.DEVNULL).strip()


def run_consumer_acceptance(*, consumer_repo: Path, data_root: Path, base_url: str,
                            api_key: str | None, deployment_manifest: Path, evidence_root: Path) -> dict:
    started = utc_now()
    deployment = runtime_identity(deployment_manifest)
    session = requests.Session()
    if api_key:
        session.headers["X-API-Key"] = api_key
    metrics_url = base_url.rstrip("/") + "/api/v1/metrics"
    ready_url = base_url.rstrip("/") + "/api/v1/health/ready"
    before_metrics_response = session.get(metrics_url, timeout=10)
    before_metrics_response.raise_for_status()
    before_metrics = before_metrics_response.json()["data"]
    source = consumer_repo / "src" / "macro_market_lab" / "cli" / "query_preview.py"
    source_text = source.read_text()
    flag_default_off = 'os.getenv("MACRO_MARKET_USE_DATA_CENTER_BARS", "").lower()' in source_text
    bid_gate_present = "price_type=bid" in source_text
    env = os.environ.copy()
    env["MACRO_MARKET_DATA_CENTER_URL"] = base_url
    if api_key:
        env["MACRO_MARKET_DATA_CENTER_API_KEY"] = api_key
    env["PYTHONPATH"] = str(consumer_repo / "src")
    consumer_python = os.getenv("MACRO_MARKET_PYTHON", "/home/quant/miniforge3/envs/macro-market-lab/bin/python")
    command = [consumer_python, "-m",
               "macro_market_lab.cli.app", "query", "preview", "dataset",
               "--provider", "dukascopy", "--asset-class", "fx", "--symbol", "EURUSD",
               "--timeframe", "1d", "--mode", "summary", "--limit", "5",
               "--data-root", str(data_root)]
    on_env = {**env, "MACRO_MARKET_USE_DATA_CENTER_BARS": "1"}
    on_started = time.monotonic()
    on = subprocess.run(command, cwd=consumer_repo, env=on_env, capture_output=True, text=True, check=True)
    on_duration = time.monotonic() - on_started
    on_payload = json.loads(on.stdout)
    off_env = {key: value for key, value in env.items() if key != "MACRO_MARKET_USE_DATA_CENTER_BARS"}
    off_started = time.monotonic()
    off = subprocess.run(command, cwd=consumer_repo, env=off_env, capture_output=True, text=True, check=True)
    off_duration = time.monotonic() - off_started
    off_payload = json.loads(off.stdout)
    windows = {
        "first": ("2026-08-28T00:00:00+00:00", "2026-09-01T00:00:00+00:00"),
        "middle": ("2026-09-01T00:00:00+00:00", "2026-09-05T00:00:00+00:00"),
        "last": ("2026-09-05T00:00:00+00:00", "2026-09-11T00:00:00+00:00"),
    }
    rows = []
    window_checks = {}
    for name, (start, end) in windows.items():
        window_rows = []
        cursor = None
        snapshots = set()
        while True:
            params = {"provider": "dukascopy", "symbol": "EURUSD", "timeframe": "1d",
                      "start": start, "end": end, "page_size": 5}
            if cursor:
                params["cursor"] = cursor
            response = session.get(base_url.rstrip("/") + "/api/v1/bars", params=params, timeout=10)
            response.raise_for_status()
            payload = response.json()
            window_rows.extend(payload["data"])
            snapshots.add(payload["meta"]["snapshot_id"])
            cursor = payload["meta"].get("next_cursor")
            if not cursor:
                break
        rows.extend(window_rows)
        timestamps = [row["bar_ts"] for row in window_rows]
        window_checks[name] = {
            "start": start, "end": end, "row_count": len(window_rows),
            "min_ts": min(timestamps) if timestamps else None,
            "max_ts": max(timestamps) if timestamps else None,
            "price_types": sorted({row.get("price_type") for row in window_rows}),
            "duplicate_count": len(timestamps) - len(set(timestamps)),
            "snapshot_count": len(snapshots), "rows_hash": _hash(window_rows),
        }
    full_start, full_end = windows["first"][0], windows["last"][1]
    coverage_response = session.get(
        base_url.rstrip("/") + "/api/v1/provider-bars/coverage",
        params={"provider": "dukascopy", "symbol": "EURUSD", "timeframe": "1d"}, timeout=10,
    )
    coverage_response.raise_for_status()
    coverage = coverage_response.json()["data"]
    ready_response = session.get(ready_url, timeout=10)
    ready_response.raise_for_status()
    readiness = ready_response.json()["data"]
    after_metrics_response = session.get(metrics_url, timeout=10)
    after_metrics_response.raise_for_status()
    after_metrics = after_metrics_response.json()["data"]
    consumer_commit = _git(consumer_repo, "rev-parse", "HEAD")
    checks = {
        "flag_default_off": flag_default_off,
        "bid_gate_present": bid_gate_present,
        "flag_on_source": on_payload.get("source"),
        "flag_on_row_count": on_payload.get("row_count"),
        "flag_off_is_legacy": "source" not in off_payload and off_payload.get("dataset_kind") == "provider_bars",
        "api_row_count": len(rows),
        "api_price_types": sorted({row.get("price_type") for row in rows}),
        "api_snapshot_count": sum(item["snapshot_count"] for item in window_checks.values()),
        "api_rows_hash": _hash(rows),
        "windows": window_checks,
        "coverage": coverage,
        "consumer_commit": consumer_commit,
        "consumer_pr": 3,
        "consumer_worktree_clean": not bool(_git(consumer_repo, "status", "--porcelain")),
        "cutover": "feature_flag_on_data_center",
        "rollback": "feature_flag_off_legacy_reader",
        "deployment": deployment,
        "readiness": readiness,
        "latency_seconds": {"cutover_cli": on_duration, "rollback_cli": off_duration,
                            "query": after_metrics["query"]["duration_seconds"]},
        "capacity_before": before_metrics["capacity"],
        "capacity_after": after_metrics["capacity"],
    }
    result = "pass" if all((checks["flag_default_off"], checks["bid_gate_present"],
                             checks["flag_on_source"] == "data_center", checks["flag_on_row_count"] == len(rows),
                             checks["flag_off_is_legacy"], checks["api_price_types"] == ["bid"],
                             all(item["price_types"] == ["bid"] and item["duplicate_count"] == 0
                                 and item["snapshot_count"] == 1 for item in window_checks.values()))) else "failed"
    common_details = {"consumer_commit": consumer_commit, "consumer_pr": 3, "deployment": deployment}
    cutover_result = "pass" if checks["flag_on_source"] == "data_center" else "failed"
    cutover_receipt = operation_receipt(
        action="dukascopy_consumer_cutover", command="MACRO_MARKET_USE_DATA_CENTER_BARS=1 query preview",
        started_at=started, result=cutover_result,
        failure_stage=None if cutover_result == "pass" else "feature_flag_on",
        details={**common_details, "source": checks["flag_on_source"],
                 "row_count": checks["flag_on_row_count"], "price_type": "bid"},
    )
    cutover_path = write_receipt(evidence_root, cutover_receipt)
    rollback_result = "pass" if checks["flag_off_is_legacy"] else "failed"
    rollback_receipt = operation_receipt(
        action="dukascopy_consumer_rollback", command="MACRO_MARKET_USE_DATA_CENTER_BARS=0 query preview",
        started_at=started, result=rollback_result,
        failure_stage=None if rollback_result == "pass" else "feature_flag_off",
        details={**common_details, "source": "legacy_reader", "dataset_kind": off_payload.get("dataset_kind")},
    )
    rollback_path = write_receipt(evidence_root, rollback_receipt)
    checks["cutover_receipt"] = str(cutover_path) if cutover_path else None
    checks["rollback_receipt"] = str(rollback_path) if rollback_path else None
    receipt = operation_receipt(action="dukascopy_consumer_parity",
        command="python -m data_center.consumer_acceptance", started_at=started, result=result,
        failure_stage=None if result == "pass" else "consumer_cutover",
        details={"provider": "dukascopy", "symbol": "EURUSD", "timeframe": "1d",
                 "window": {"start": full_start, "end": full_end, "semantics": "half-open"},
                 "checks": checks})
    path = write_receipt(evidence_root, receipt)
    return {**receipt, "receipt": str(path) if path else None}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--consumer-repo", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:18380")
    parser.add_argument("--api-key", default=os.getenv("DATACENTER_API_KEY"), required=False)
    parser.add_argument("--deployment-manifest", type=Path, required=True)
    parser.add_argument("--evidence-root", type=Path, required=True)
    args = parser.parse_args()
    report = run_consumer_acceptance(consumer_repo=args.consumer_repo, data_root=args.data_root,
                                     base_url=args.base_url, api_key=args.api_key,
                                     deployment_manifest=args.deployment_manifest, evidence_root=args.evidence_root)
    print(json.dumps({key: value for key, value in report.items() if key != "details"}), flush=True)
    raise SystemExit(0 if report["result"] == "pass" else 1)


if __name__ == "__main__":
    main()
