"""Generic, bounded 1m maintenance runner.

The runner owns scheduling policy only.  It resolves approved instruments and
current coverage from the catalog, asks the control plane for windows, submits
those windows through the public ingest API, and records one operational
receipt.  Provider requests remain inside the connector and worker boundary.
"""
from __future__ import annotations

import argparse
import json
import os
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from data_center.evidence import operation_receipt, write_receipt
from data_center.platform import coverage_from_catalog, plan_maintenance_from_catalog
from data_center.platform_registry import REGISTRY, maintenance_policy_for
from data_center.settings import Settings


@dataclass(frozen=True)
class MaintenanceTarget:
    provider: str
    symbol: str
    asset_class: str


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("maintenance timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)


def _closed_minute_boundary(value: datetime) -> datetime:
    """Return the first not-yet-closed minute, suitable as a half-open end."""
    return _utc(value).replace(second=0, microsecond=0)


def approved_targets(provider: str, symbols: Iterable[str] | None = None) -> tuple[MaintenanceTarget, ...]:
    requested = {item.strip().upper() for item in symbols or () if item.strip()}
    instruments = REGISTRY.instruments(provider)
    if requested:
        unknown = requested - {item.symbol for item in instruments}
        if unknown:
            raise ValueError(f"symbols are not approved for {provider}: {sorted(unknown)}")
        instruments = tuple(item for item in instruments if item.symbol in requested)
    return tuple(MaintenanceTarget(item.provider, item.symbol, item.asset_class) for item in instruments)


def _request(session: requests.Session, base_url: str, method: str, path: str, **kwargs) -> dict:
    response = session.request(method, base_url.rstrip("/") + "/api/v1" + path, timeout=30, **kwargs)
    response.raise_for_status()
    payload = response.json()
    if payload.get("errors"):
        raise RuntimeError(f"Data Center API returned errors for {path}")
    return payload["data"]


def _wait_run(session: requests.Session, base_url: str, run_id: str, deadline: float) -> dict:
    while True:
        receipt = _request(session, base_url, "GET", f"/runs/{run_id}")
        if receipt.get("status") in {"pass", "failed", "dead_letter"}:
            return receipt
        if time.monotonic() >= deadline:
            raise TimeoutError(f"maintenance run polling deadline exceeded: {run_id}")
        time.sleep(1.0)


def run_maintenance(*, base_url: str, root: Path, evidence_root: Path, provider: str = "dukascopy",
                    symbols: Iterable[str] | None = None, start: datetime, end: datetime,
                    run_scope: str = "maintenance", poll_deadline_seconds: float = 900.0) -> dict:
    """Run one bounded maintenance pass and persist an operational receipt."""
    start, end = _utc(start), _utc(end)
    if end <= start:
        raise ValueError("maintenance end must be after start")
    policy = maintenance_policy_for(provider, "1m")
    capability = REGISTRY.capability(provider)
    if end - start > timedelta(days=min(policy.max_window_days, capability.max_window_days)):
        raise ValueError("maintenance request exceeds the registered provider window")
    targets = approved_targets(provider, symbols)
    if not targets:
        raise ValueError(f"no approved instruments selected for {provider}")

    started = datetime.now(timezone.utc).isoformat()
    settings = Settings()
    capacity_before = settings.capacity_policy().inspect(root).as_dict()
    session = requests.Session()
    session.trust_env = False
    api_key = os.getenv("DATACENTER_API_KEY")
    if api_key:
        session.headers["X-API-Key"] = api_key
    results: list[dict] = []
    failures = 0
    try:
        for target in targets:
            result = {"provider": target.provider, "symbol": target.symbol,
                      "asset_class": target.asset_class, "requested_start": start.isoformat(),
                      "requested_end": end.isoformat(), "runs": []}
            try:
                from data_center.domain.models import IngestJob

                job = IngestJob(
                    job_id=f"maintenance-{provider.lower()}-{target.symbol.lower()}",
                    dataset_id="provider_bars", provider=provider, symbol=target.symbol,
                    asset_class=target.asset_class, timeframe="1m", start=start, end=end,
                    run_scope=run_scope, run_kind="ingest",
                )
                planned = plan_maintenance_from_catalog(root=root, job=job, policy=policy)
                result["coverage_before"] = planned["coverage"]
                windows = planned["plan"]["windows"]
                result["window_count"] = len(windows)
                for ordinal, window in enumerate(windows):
                    window_start = datetime.fromisoformat(window["start"])
                    window_end = datetime.fromisoformat(window["end"])
                    reason = window.get("reason", "ingest")
                    run_kind = "gap_repair" if reason == "gap_repair" else "ingest"
                    payload = {
                        "job_id": f"{job.job_id}-w{ordinal:04d}", "dataset_id": "provider_bars",
                        "provider": provider, "symbol": target.symbol, "asset_class": target.asset_class,
                        "timeframe": "1m", "start": window_start.isoformat(), "end": window_end.isoformat(),
                        "run_scope": run_scope, "run_kind": run_kind,
                    }
                    submitted = _request(session, base_url, "POST", "/ingest/runs", json=payload)
                    receipt = _wait_run(session, base_url, submitted["run_id"],
                                        time.monotonic() + poll_deadline_seconds)
                    result["runs"].append({"reason": reason, "run_id": submitted["run_id"],
                                           "status": receipt.get("status"),
                                           "row_count": receipt.get("row_count"),
                                           "coverage": receipt.get("coverage"),
                                           "error_type": receipt.get("error_type")})
                    if receipt.get("status") != "pass":
                        raise RuntimeError(f"maintenance run failed: {submitted['run_id']}")
                result["coverage_after"] = coverage_from_catalog(root=root, job=job).as_dict()
                result["status"] = "pass"
            except Exception as exc:  # noqa: BLE001 - one symbol must not hide the others
                failures += 1
                result.update(status="failed", error_type=type(exc).__name__)
            results.append(result)
    finally:
        session.close()

    details = {
        "provider": provider, "run_scope": run_scope, "timeframe": "1m",
        "requested_start": start.isoformat(), "requested_end": end.isoformat(),
        "target_count": len(targets), "failed_target_count": failures,
        "targets": results, "capacity_before": capacity_before,
        "capacity_after": settings.capacity_policy().inspect(root).as_dict(),
    }
    receipt = operation_receipt(
        action="market_data_1m_maintenance", command="python -m data_center.maintenance_runner",
        started_at=started, result="failed" if failures else "pass",
        failure_stage="maintenance" if failures else None,
        error_category="MaintenanceTargetError" if failures else None, details=details,
    )
    receipt_path = write_receipt(evidence_root, receipt)
    return {**receipt, "receipt": str(receipt_path) if receipt_path else None}


def main() -> None:
    settings = Settings()
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:18380")
    parser.add_argument("--provider", default="dukascopy")
    parser.add_argument("--symbols", nargs="*", default=None)
    parser.add_argument("--start", type=datetime.fromisoformat)
    parser.add_argument("--end", type=datetime.fromisoformat)
    parser.add_argument("--run-scope", choices=("maintenance", "production"), default="maintenance")
    args = parser.parse_args()
    end = _closed_minute_boundary(args.end or datetime.now(timezone.utc))
    policy = maintenance_policy_for(args.provider, "1m")
    start = _utc(args.start or (end - timedelta(days=policy.tail_days)))
    configured_symbols = settings.maintenance_symbol_list()
    selected_symbols = args.symbols if args.symbols is not None else configured_symbols or None
    report = run_maintenance(
        base_url=args.base_url, root=settings.canonical_root, evidence_root=settings.evidence_root,
        provider=args.provider, symbols=selected_symbols, start=start, end=end, run_scope=args.run_scope,
    )
    print(json.dumps({"result": report["result"], "receipt": report["receipt"],
                      "target_count": report["details"]["target_count"],
                      "failed_target_count": report["details"]["failed_target_count"]}, sort_keys=True))
    raise SystemExit(0 if report["result"] == "pass" else 1)


if __name__ == "__main__":
    main()
