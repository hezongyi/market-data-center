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

from data_center.control_plane import plan_maintenance as plan_windows
from data_center.evidence import operation_receipt, write_receipt
from data_center.platform import build_ingest_plan, coverage_from_catalog
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


def _scheduled_end(value: datetime, *, lag_minutes: int) -> datetime:
    """Bound an unattended pass to data the provider is expected to expose.

    Explicit historical ``--end`` values are never passed through this helper;
    only the timer's implicit ``now`` uses the provider availability lag.
    """
    return _closed_minute_boundary(_utc(value) - timedelta(minutes=lag_minutes))


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


def _window_key(window: dict) -> tuple[str, str]:
    """Normalize a half-open window for exact gap de-duplication."""
    return (
        _utc(datetime.fromisoformat(str(window["start"]))).isoformat(),
        _utc(datetime.fromisoformat(str(window["end"]))).isoformat(),
    )


def _coverage_failure(receipt: dict) -> dict | None:
    """Return the structured coverage finding from a failed worker run."""
    findings = (receipt.get("quality_summary") or {}).get("findings") or ()
    for finding in findings:
        if finding.get("code") == "coverage_not_ready" and isinstance(finding.get("coverage"), dict):
            return finding["coverage"]
    return None


def _isolate_incomplete_window(*, start: datetime, end: datetime,
                               receipt: dict) -> tuple[datetime, datetime,
                                                       list[tuple[datetime, datetime]]] | None:
    """Split a failed response around one observed provider gap.

    This only returns segments when the provider response contains data after
    the first missing cadence.  A tail that simply has not arrived yet is left
    as one failed bounded run, avoiding speculative retries or synthetic bars.
    """
    coverage = _coverage_failure(receipt)
    if coverage is None:
        return None
    seconds = int(coverage.get("timeframe_seconds") or 0)
    if seconds <= 0:
        return None
    cadence = timedelta(seconds=seconds)
    missing_text = coverage.get("first_missing_ts")
    complete_text = coverage.get("latest_complete_boundary")
    if not missing_text and complete_text:
        missing_text = (_utc(datetime.fromisoformat(str(complete_text))) + cadence).isoformat()
    if not missing_text:
        return None
    try:
        missing_start = _utc(datetime.fromisoformat(str(missing_text)))
        observed_max = _utc(datetime.fromisoformat(str(coverage["max_ts"])))
    except (KeyError, TypeError, ValueError):
        return None
    start, end = _utc(start), _utc(end)
    missing_end = missing_start + cadence
    if not start <= missing_start < end or observed_max < missing_end:
        return None
    segments = []
    if start < missing_start:
        segments.append((start, missing_start))
    if missing_end < end:
        segments.append((missing_end, end))
    if not segments:
        return None
    return missing_start, missing_end, segments


def _recent_gap_windows(*, runs: Iterable[dict], provider: str, symbol: str,
                        run_scope: str, now: datetime,
                        cooldown_minutes: int) -> set[tuple[str, str]]:
    """Find terminal gap windows still inside the configured retry cooldown."""
    if cooldown_minutes <= 0:
        return set()
    cutoff = _utc(now) - timedelta(minutes=cooldown_minutes)
    recent: set[tuple[str, str]] = set()
    for run in runs:
        if (run.get("status") != "dead_letter" or run.get("provider") != provider
                or run.get("symbol") != symbol or run.get("run_scope") != run_scope
                or run.get("run_kind") != "gap_repair"):
            continue
        finished_text = run.get("finished_at") or run.get("at")
        if not finished_text:
            continue
        try:
            if _utc(datetime.fromisoformat(str(finished_text))) < cutoff:
                continue
        except (TypeError, ValueError):
            continue
        execution_plan = run.get("execution_plan") or {}
        for window in execution_plan.get("windows") or ():
            if window.get("reason") == "gap_repair":
                try:
                    recent.add(_window_key(window))
                except (KeyError, TypeError, ValueError):
                    continue
        for finding in (run.get("quality_summary") or {}).get("findings") or ():
            if finding.get("code") != "coverage_not_ready":
                continue
            coverage = finding.get("coverage") or {}
            seconds = int(coverage.get("timeframe_seconds") or 0)
            missing_text = coverage.get("first_missing_ts")
            complete_text = coverage.get("latest_complete_boundary")
            if not missing_text and complete_text and seconds > 0:
                missing_text = (_utc(datetime.fromisoformat(str(complete_text)))
                                + timedelta(seconds=seconds)).isoformat()
            if not missing_text or seconds <= 0:
                continue
            try:
                missing = _utc(datetime.fromisoformat(str(missing_text)))
            except (TypeError, ValueError):
                continue
            recent.add((missing.isoformat(), (missing + timedelta(seconds=seconds)).isoformat()))
        findings = (run.get("quality_summary") or {}).get("findings") or ()
        for finding in findings:
            if finding.get("code") != "coverage_not_ready":
                continue
            coverage = finding.get("coverage") or {}
            seconds = int(coverage.get("timeframe_seconds") or 0)
            missing_text = coverage.get("first_missing_ts")
            complete_text = coverage.get("latest_complete_boundary")
            if not missing_text and complete_text and seconds > 0:
                missing_text = (_utc(datetime.fromisoformat(str(complete_text)))
                                + timedelta(seconds=seconds)).isoformat()
            if not missing_text or seconds <= 0:
                continue
            try:
                missing = _utc(datetime.fromisoformat(str(missing_text)))
            except (TypeError, ValueError):
                continue
            recent.add((missing.isoformat(), (missing + timedelta(seconds=seconds)).isoformat()))
    return recent


def _tail_recovery_windows(*, coverage, start: datetime, end: datetime, policy,
                           session_profile=None) -> list[dict]:
    """Plan the observed suffix after an interior provider gap.

    ``plan_maintenance`` correctly prioritizes gap repair when a coverage
    object contains missing timestamps.  For a provider that permanently omits
    one minute, that alone would also stop the watermark from advancing.  A
    suffix is safe to fetch when rows exist after the last missing cadence; it
    starts strictly after the observed maximum and can therefore never include
    the unresolved gap.
    """
    if not coverage.missing_timestamps or coverage.max_ts is None:
        return []
    cadence = coverage.timeframe
    interior_missing = [stamp for stamp in coverage.missing_timestamps if stamp <= coverage.max_ts]
    if not interior_missing:
        return []
    suffix_start = max(_utc(start), coverage.max_ts + cadence)
    if suffix_start >= _utc(end):
        return []
    return [window.as_dict() for window in plan_windows(
        start=suffix_start, end=_utc(end), coverage=None, policy=policy,
        reason="tail", timeframe=cadence, session_profile=session_profile,
    )]


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
    gap_cooldown_runs: list[dict] = []
    if policy.gap_retry_cooldown_minutes > 0:
        try:
            gap_cooldown_runs = _request(session, base_url, "GET", "/runs", params={"status": "dead_letter"})
        except (requests.RequestException, RuntimeError, KeyError, TypeError, ValueError):
            # A missing cooldown lookup must not turn a read-only diagnostic
            # into a false suppression; normal governed retries remain safe.
            gap_cooldown_runs = []
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
                coverage = coverage_from_catalog(root=root, job=job)
                planned = build_ingest_plan(job=job, coverage=coverage, policy=policy)
                result["coverage_before"] = coverage.as_dict()
                windows = list(planned["windows"])
                session_profile = REGISTRY.session(REGISTRY.instrument(
                    target.provider, target.symbol,
                ).session_profile)
                recovery_windows = _tail_recovery_windows(
                    coverage=coverage, start=start, end=end, policy=policy,
                    session_profile=session_profile,
                )
                if recovery_windows:
                    windows.extend(recovery_windows)
                    result["recovery_window_count"] = len(recovery_windows)
                else:
                    result["recovery_window_count"] = 0
                result["window_count"] = len(windows)
                result["unresolved_gaps"] = []
                recent_gaps = _recent_gap_windows(
                    runs=gap_cooldown_runs, provider=provider, symbol=target.symbol,
                    run_scope=run_scope, now=datetime.now(timezone.utc),
                    cooldown_minutes=policy.gap_retry_cooldown_minutes,
                )
                result["suppressed_gap_count"] = 0
                pending = [(window, None, 0) for window in windows]
                window_failures = 0
                while pending:
                    window, recovery_of, isolation_depth = pending.pop(0)
                    window_start = datetime.fromisoformat(window["start"])
                    window_end = datetime.fromisoformat(window["end"])
                    reason = window.get("reason", "ingest")
                    run_kind = "gap_repair" if reason == "gap_repair" else "ingest"
                    ordinal = len(result["runs"])
                    if run_kind == "gap_repair" and _window_key(window) in recent_gaps:
                        result["suppressed_gap_count"] += 1
                        result["unresolved_gaps"].append({
                            "start": window_start.isoformat(), "end": window_end.isoformat(),
                            "run_id": None, "reason": "recent_dead_letter_cooldown",
                            "error_type": "GapRetrySuppressed",
                        })
                        window_failures += 1
                        continue
                    payload = {
                        "job_id": f"{job.job_id}-w{ordinal:04d}", "dataset_id": "provider_bars",
                        "provider": provider, "symbol": target.symbol, "asset_class": target.asset_class,
                        "timeframe": "1m", "start": window_start.isoformat(), "end": window_end.isoformat(),
                        "run_scope": run_scope, "run_kind": run_kind,
                    }
                    try:
                        submitted = _request(session, base_url, "POST", "/ingest/runs", json=payload)
                        receipt = _wait_run(session, base_url, submitted["run_id"],
                                            time.monotonic() + poll_deadline_seconds)
                        run_result = {"reason": reason, "run_id": submitted["run_id"],
                                      "status": receipt.get("status"),
                                      "row_count": receipt.get("row_count"),
                                      "coverage": receipt.get("coverage"),
                                      "error_type": receipt.get("error_type"),
                                      "recovery_of": recovery_of,
                                      "window": {"start": window_start.isoformat(),
                                                 "end": window_end.isoformat(),
                                                 "semantics": "half-open"}}
                        result["runs"].append(run_result)
                        if receipt.get("status") == "pass":
                            continue
                        window_failures += 1
                        isolated = _isolate_incomplete_window(
                            start=window_start, end=window_end, receipt=receipt,
                        ) if isolation_depth < 16 else None
                        if isolated is None:
                            result["unresolved_gaps"].append({
                                "start": window_start.isoformat(), "end": window_end.isoformat(),
                                "run_id": submitted["run_id"], "reason": reason,
                                "error_type": receipt.get("error_type"),
                                "quality_summary": receipt.get("quality_summary"),
                            })
                            continue
                        missing_start, missing_end, segments = isolated
                        result["unresolved_gaps"].append({
                            "start": missing_start.isoformat(), "end": missing_end.isoformat(),
                            "run_id": submitted["run_id"], "reason": "provider_gap",
                            "error_type": receipt.get("error_type"),
                            "quality_summary": receipt.get("quality_summary"),
                        })
                        result["recovery_window_count"] += len(segments)
                        pending.extend(({
                            "start": segment_start.isoformat(), "end": segment_end.isoformat(),
                            "reason": "gap_isolation", "ordinal": ordinal,
                            "semantics": "half-open",
                        }, submitted["run_id"], isolation_depth + 1)
                                       for segment_start, segment_end in segments)
                    except Exception as exc:  # noqa: BLE001 - continue independent windows
                        window_failures += 1
                        result["runs"].append({
                            "reason": reason, "run_id": None, "status": "failed",
                            "row_count": None, "coverage": None,
                            "error_type": type(exc).__name__, "recovery_of": recovery_of,
                            "window": {"start": window_start.isoformat(),
                                       "end": window_end.isoformat(), "semantics": "half-open"},
                        })
                result["coverage_after"] = coverage_from_catalog(root=root, job=job).as_dict()
                result["failed_window_count"] = window_failures
                if window_failures:
                    failures += 1
                    result.update(status="failed", error_type="MaintenanceWindowError")
                else:
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
        "gap_retry_cooldown_minutes": policy.gap_retry_cooldown_minutes,
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
    policy = maintenance_policy_for(args.provider, "1m")
    end = (_closed_minute_boundary(args.end) if args.end is not None
           else _scheduled_end(datetime.now(timezone.utc), lag_minutes=policy.closed_bar_lag_minutes))
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
