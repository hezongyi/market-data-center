"""Pure window rules shared by the maintenance runner and the production scheduler.

These functions were extracted from ``maintenance_runner`` unchanged: the legacy
runner already had the governed behaviour for retry cooldowns, de-duplicating a
recovery window against the primary plan, and fetching the observed suffix after
an interior provider gap.  The production scheduler must keep the same
behaviour, so the rules live here instead of being re-implemented (plan S4.4).

Nothing in this module touches the provider, the ledger or the catalog: callers
own I/O and pass plain run/window/coverage data in.
"""
from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta

from data_center.control_plane import CoverageResult, MaintenancePolicy
from data_center.control_plane import plan_maintenance as plan_windows
from data_center.instants import aware_utc

from .instants import parse_instant

# Window coverage the provider itself cannot satisfy; the platform never synthesizes it.
PROVIDER_COVERAGE_FINDING = "coverage_not_ready"


def is_provider_gap_receipt(receipt: dict) -> bool:
    """Whether one terminal run proves a pure, exactly recoverable provider gap."""
    return bool(_provider_gap_windows_from_receipt(receipt))


def provider_gap_windows(receipts: Iterable[dict]) -> list[dict]:
    """Extract exact half-open gap debt from terminal provider responses."""
    gaps: set[tuple[datetime, datetime]] = set()
    for receipt in receipts:
        gaps.update(_provider_gap_windows_from_receipt(receipt))
    return [{"window_start": start.isoformat(), "window_end": end.isoformat()}
            for start, end in sorted(gaps)]


def _provider_gap_windows_from_receipt(receipt: dict) -> set[tuple[datetime, datetime]]:
    """Classify and extract together so a degraded outcome can never lose its debt."""
    gaps: set[tuple[datetime, datetime]] = set()
    if receipt.get("status") not in {"failed", "dead_letter"}:
        return gaps
    error_type = receipt.get("error_type")
    plan_windows = (receipt.get("execution_plan") or {}).get("windows") or ()
    if error_type == "ProviderGapError":
        for window in plan_windows:
            try:
                start = utc(parse_instant(str(window["start"])))
                end = utc(parse_instant(str(window["end"])))
            except (KeyError, TypeError, ValueError):
                continue
            if start < end:
                gaps.add((start, end))
        return gaps
    # Older receipts may lack error_type, but only structured coverage evidence
    # is authoritative. A historical gap_repair label alone can also accompany
    # a timeout or provider outage and must fail closed.
    if error_type not in {None, "QualityError"}:
        return gaps
    findings = (receipt.get("quality_summary") or {}).get("findings") or ()
    if not findings or any(item.get("code") != PROVIDER_COVERAGE_FINDING for item in findings):
        return gaps
    for finding in findings:
        coverage = finding.get("coverage") or {}
        seconds = int(coverage.get("timeframe_seconds") or 0)
        if seconds <= 0:
            return set()
        cadence = timedelta(seconds=seconds)
        ready = []
        for interval in coverage.get("ready_intervals") or ():
            try:
                ready_start = utc(parse_instant(str(interval["start"])))
                ready_end = utc(parse_instant(str(interval["end"])))
            except (KeyError, TypeError, ValueError):
                return set()
            if ready_start >= ready_end:
                return set()
            ready.append((ready_start, ready_end))
        if ready and plan_windows:
            finding_gaps: set[tuple[datetime, datetime]] = set()
            for window in plan_windows:
                try:
                    cursor = utc(parse_instant(str(window["start"])))
                    end = utc(parse_instant(str(window["end"])))
                except (KeyError, TypeError, ValueError):
                    return set()
                if cursor >= end:
                    return set()
                for ready_start, ready_end in sorted(ready):
                    if ready_end <= cursor or ready_start >= end:
                        continue
                    if cursor < ready_start:
                        finding_gaps.add((cursor, min(ready_start, end)))
                    cursor = max(cursor, min(ready_end, end))
                if cursor < end:
                    finding_gaps.add((cursor, end))
            if not finding_gaps:
                return set()
            gaps.update(finding_gaps)
            continue
        missing_text = coverage.get("first_missing_ts")
        if not missing_text and coverage.get("latest_complete_boundary"):
            try:
                missing_text = (utc(parse_instant(str(coverage["latest_complete_boundary"])))
                                + cadence).isoformat()
            except (TypeError, ValueError):
                return set()
        if not missing_text:
            return set()
        plan_windows = (receipt.get("execution_plan") or {}).get("windows") or ()
        try:
            missing = utc(parse_instant(str(missing_text)))
        except (TypeError, ValueError):
            return set()
        if plan_windows:
            try:
                bounds = [(utc(parse_instant(str(item["start"]))),
                           utc(parse_instant(str(item["end"])))) for item in plan_windows]
            except (KeyError, TypeError, ValueError):
                return set()
            if not any(start <= missing and missing + cadence <= end for start, end in bounds):
                return set()
        gaps.add((missing, missing + cadence))
    return gaps


def utc(value: datetime) -> datetime:
    """Require an aware timestamp and return it in UTC.

    Delegates to the platform's single parser so the runner, the planner and the
    API cannot disagree about what an instant is.
    """
    return aware_utc(value, field="planning timestamp")


def window_key(window: dict) -> tuple[str, str]:
    """Normalize a half-open window for exact gap de-duplication."""
    return (
        utc(parse_instant(str(window["start"]))).isoformat(),
        utc(parse_instant(str(window["end"]))).isoformat(),
    )


def recent_gap_windows(*, runs: Iterable[dict], provider: str, symbol: str,
                       run_scope: str, now: datetime,
                       cooldown_minutes: int) -> set[tuple[str, str]]:
    """Find terminal gap windows still inside the configured retry cooldown."""
    if cooldown_minutes <= 0:
        return set()
    cutoff = utc(now) - timedelta(minutes=cooldown_minutes)
    recent: set[tuple[str, str]] = set()
    for run in runs:
        if (run.get("status") not in {"failed", "dead_letter"} or run.get("provider") != provider
                or run.get("symbol") != symbol or run.get("run_scope") != run_scope
                or not is_provider_gap_receipt(run)):
            continue
        finished_text = run.get("finished_at") or run.get("at")
        if not finished_text:
            continue
        try:
            if utc(parse_instant(str(finished_text))) < cutoff:
                continue
        except (TypeError, ValueError):
            continue
        for gap in provider_gap_windows([run]):
            recent.add((gap["window_start"], gap["window_end"]))
    return recent


def exclude_planned_windows(*, candidates: Iterable[dict], planned: Iterable[dict]) -> list[dict]:
    """Remove intervals already covered by the primary maintenance plan."""
    occupied = [
        (utc(parse_instant(str(window["start"]))),
         utc(parse_instant(str(window["end"]))))
        for window in planned
    ]
    uncovered: list[dict] = []
    for candidate in candidates:
        segments = [
            (utc(parse_instant(str(candidate["start"]))),
             utc(parse_instant(str(candidate["end"]))))
        ]
        for occupied_start, occupied_end in occupied:
            remaining: list[tuple[datetime, datetime]] = []
            for segment_start, segment_end in segments:
                if occupied_end <= segment_start or occupied_start >= segment_end:
                    remaining.append((segment_start, segment_end))
                    continue
                if segment_start < occupied_start:
                    remaining.append((segment_start, occupied_start))
                if occupied_end < segment_end:
                    remaining.append((occupied_end, segment_end))
            segments = remaining
        for segment_start, segment_end in segments:
            uncovered.append({
                **candidate,
                "start": segment_start.isoformat(),
                "end": segment_end.isoformat(),
                "ordinal": len(uncovered),
            })
    return uncovered


def tail_recovery_windows(*, coverage: CoverageResult, start: datetime, end: datetime,
                          policy: MaintenancePolicy,
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
    suffix_start = max(utc(start), coverage.max_ts + cadence)
    if suffix_start >= utc(end):
        return []
    return [window.as_dict() for window in plan_windows(
        start=suffix_start, end=utc(end), coverage=None, policy=policy,
        reason="tail", timeframe=cadence, session_profile=session_profile,
    )]


def missing_ranges(*, coverage: CoverageResult, start: datetime, end: datetime) -> list[dict]:
    """Contiguous half-open ranges of expected-but-absent timestamps.

    The ranges come from ``missing_timestamps``, never from ``max_ts``: using the
    observed maximum would silently retire every gap behind it (spec 5.5).
    """
    cadence = coverage.timeframe
    window_start, window_end = utc(start), utc(end)
    stamps = sorted(stamp for stamp in coverage.missing_timestamps
                    if window_start <= stamp < window_end)
    ranges: list[dict] = []
    for stamp in stamps:
        if ranges and stamp - ranges[-1]["_last"] == cadence:
            ranges[-1]["end"] = stamp + cadence
            ranges[-1]["_last"] = stamp
            continue
        ranges.append({"start": stamp, "end": stamp + cadence, "_last": stamp})
    # A range that reaches past the newest bar we have seen is a fetch of data
    # the provider may simply not publish yet; anything fully behind it is an
    # interior gap that blocks only the outputs covering it.
    return [{"start": item["start"], "end": item["end"],
             "trailing": bool(coverage.max_ts is None or item["end"] > coverage.max_ts)}
            for item in ranges]
