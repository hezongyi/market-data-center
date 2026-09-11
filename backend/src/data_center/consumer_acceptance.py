"""Real-environment consumer flag cutover and rollback acceptance."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
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
                            api_key: str, deployment_manifest: Path, evidence_root: Path) -> dict:
    started = utc_now()
    deployment = runtime_identity(deployment_manifest)
    source = consumer_repo / "src" / "macro_market_lab" / "cli" / "query_preview.py"
    source_text = source.read_text()
    flag_default_off = 'os.getenv("MACRO_MARKET_USE_DATA_CENTER_BARS", "").lower()' in source_text
    bid_gate_present = "price_type=bid" in source_text
    env = os.environ.copy()
    env["MACRO_MARKET_DATA_CENTER_URL"] = base_url
    env["MACRO_MARKET_DATA_CENTER_API_KEY"] = api_key
    env["PYTHONPATH"] = str(consumer_repo / "src")
    command = ["/home/quant/miniforge3/envs/macro-market-lab/bin/python", "-m",
               "macro_market_lab.cli.app", "query", "preview", "dataset",
               "--provider", "dukascopy", "--asset-class", "fx", "--symbol", "EURUSD",
               "--timeframe", "1d", "--mode", "summary", "--limit", "5",
               "--data-root", str(data_root)]
    on_env = {**env, "MACRO_MARKET_USE_DATA_CENTER_BARS": "1"}
    on = subprocess.run(command, cwd=consumer_repo, env=on_env, capture_output=True, text=True, check=True)
    on_payload = json.loads(on.stdout)
    off_env = {key: value for key, value in env.items() if key != "MACRO_MARKET_USE_DATA_CENTER_BARS"}
    off = subprocess.run(command, cwd=consumer_repo, env=off_env, capture_output=True, text=True, check=True)
    off_payload = json.loads(off.stdout)
    session = requests.Session()
    session.headers["X-API-Key"] = api_key
    rows = []
    cursor = None
    snapshots = set()
    while True:
        params = {"provider": "dukascopy", "symbol": "EURUSD", "timeframe": "1d",
                  "start": "2026-08-28T00:00:00+00:00", "end": "2026-09-11T00:00:00+00:00",
                  "page_size": 5}
        if cursor:
            params["cursor"] = cursor
        payload = session.get(base_url.rstrip("/") + "/api/v1/bars", params=params, timeout=10).json()
        rows.extend(payload["data"])
        snapshots.add(payload["meta"]["snapshot_id"])
        cursor = payload["meta"].get("next_cursor")
        if not cursor:
            break
    checks = {
        "flag_default_off": flag_default_off,
        "bid_gate_present": bid_gate_present,
        "flag_on_source": on_payload.get("source"),
        "flag_on_row_count": on_payload.get("row_count"),
        "flag_off_is_legacy": "source" not in off_payload and off_payload.get("dataset_kind") == "provider_bars",
        "api_row_count": len(rows),
        "api_price_types": sorted({row.get("price_type") for row in rows}),
        "api_snapshot_count": len(snapshots),
        "api_rows_hash": _hash(rows),
        "consumer_commit": _git(consumer_repo, "rev-parse", "HEAD"),
        "consumer_pr": 3,
        "consumer_worktree_clean": not bool(_git(consumer_repo, "status", "--porcelain")),
        "cutover": "feature_flag_on_data_center",
        "rollback": "feature_flag_off_legacy_reader",
        "deployment": deployment,
    }
    result = "pass" if all((checks["flag_default_off"], checks["bid_gate_present"],
                             checks["flag_on_source"] == "data_center", checks["flag_on_row_count"] == len(rows),
                             checks["flag_off_is_legacy"], checks["api_price_types"] == ["bid"],
                             checks["api_snapshot_count"] == 1)) else "failed"
    receipt = operation_receipt(action="dukascopy_consumer_parity",
        command="python -m data_center.consumer_acceptance", started_at=started, result=result,
        failure_stage=None if result == "pass" else "consumer_cutover",
        details={"provider": "dukascopy", "symbol": "EURUSD", "timeframe": "1d",
                 "window": {"start": params["start"], "end": params["end"], "semantics": "half-open"},
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
    if not args.api_key:
        parser.error("--api-key or DATACENTER_API_KEY is required")
    report = run_consumer_acceptance(consumer_repo=args.consumer_repo, data_root=args.data_root,
                                     base_url=args.base_url, api_key=args.api_key,
                                     deployment_manifest=args.deployment_manifest, evidence_root=args.evidence_root)
    print(json.dumps({key: value for key, value in report.items() if key != "details"}), flush=True)
    raise SystemExit(0 if report["result"] == "pass" else 1)


if __name__ == "__main__":
    main()
