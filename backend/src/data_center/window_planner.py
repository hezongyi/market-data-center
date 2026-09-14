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
                or run.get("run_kind") != "gap_repair"):
            continue
        finished_text = run.get("finished_at") or run.get("at")
        if not finished_text:
            continue
        try:
            if utc(parse_instant(str(finished_text))) < cutoff:
                continue
        except (TypeError, ValueError):
            continue
        execution_plan = run.get("execution_plan") or {}
        for window in execution_plan.get("windows") or ():
            if window.get("reason") == "gap_repair":
                try:
                    recent.add(window_key(window))
                except (KeyError, TypeError, ValueError):
                    continue
        for finding in (run.get("quality_summary") or {}).get("findings") or ():
            if finding.get("code") != PROVIDER_COVERAGE_FINDING:
                continue
            coverage = finding.get("coverage") or {}
            seconds = int(coverage.get("timeframe_seconds") or 0)
            missing_text = coverage.get("first_missing_ts")
            complete_text = coverage.get("latest_complete_boundary")
            if not missing_text and complete_text and seconds > 0:
                missing_text = (utc(parse_instant(str(complete_text)))
                                + timedelta(seconds=seconds)).isoformat()
            if not missing_text or seconds <= 0:
                continue
            try:
                missing = utc(parse_instant(str(missing_text)))
            except (TypeError, ValueError):
                continue
            recent.add((missing.isoformat(), (missing + timedelta(seconds=seconds)).isoformat()))
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
