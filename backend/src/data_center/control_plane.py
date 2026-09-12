"""Provider-agnostic control-plane primitives.

These small interfaces are the seam between provider adapters and the local
worker.  They intentionally return immutable plans/results so a run can be
replayed and audited without reinterpreting mutable configuration.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, model_validator

TIMEFRAME_DELTAS: dict[str, timedelta] = {
    "1m": timedelta(minutes=1),
    "5m": timedelta(minutes=5),
    "15m": timedelta(minutes=15),
    "30m": timedelta(minutes=30),
    "1h": timedelta(hours=1),
    "4h": timedelta(hours=4),
    "1d": timedelta(days=1),
    "1w": timedelta(days=7),
    # Calendar-month semantics are handled by the transform executor and the
    # coverage evaluator.  The duration is retained as a conservative cadence
    # for generic planning and validation callers.
    "1mo": timedelta(days=30),
}


def timeframe_delta(timeframe: str) -> timedelta:
    """Resolve a canonical timeframe to its duration at the control-plane seam."""
    try:
        return TIMEFRAME_DELTAS[timeframe]
    except KeyError as exc:
        raise ValueError(f"unsupported timeframe: {timeframe}") from exc


class ProviderCapability(BaseModel):
    provider: str
    asset_classes: tuple[str, ...] = ()
    symbols: tuple[str, ...] = ()
    timeframes: tuple[str, ...] = ()
    # A provider may expose native higher periods while the platform's
    # canonical maintenance contract intentionally allows only its raw source
    # timeframe (for example Dukascopy 1m BID).
    maintenance_timeframes: tuple[str, ...] = ()
    price_bases: tuple[str, ...] = ("raw",)
    max_window_days: int = Field(default=31, ge=1)
    session_profile: str = "default"


class InstrumentMetadata(BaseModel):
    provider: str
    symbol: str
    canonical_symbol: str
    asset_class: str
    currency: str
    session_profile: str
    calendar_profile: str
    provider_symbol: str | None = None
    approved: bool = True


class QualityProfile(BaseModel):
    profile_id: str
    require_utc: bool = True
    require_unique_primary_key: bool = True
    require_ohlc: bool = True
    require_session_coverage: bool = True


class MaintenancePolicy(BaseModel):
    policy_id: str = "default"
    max_window_days: int = Field(default=7, ge=1)
    tail_days: int = Field(default=2, ge=1)
    shard_days: int = Field(default=7, ge=1)
    # Some providers return incomplete results for long intraday requests even
    # when the same interval is complete in a smaller request.  Keep this as
    # policy data so the planner remains provider-agnostic.
    shard_minutes: int | None = Field(default=None, ge=1)
    closed_bar_lag_minutes: int = Field(default=1, ge=0)
    # A terminal provider gap is retried less frequently than the short worker
    # retry loop.  Zero keeps the default stateless behavior for providers that
    # do not opt into a governed cooldown.
    gap_retry_cooldown_minutes: int = Field(default=0, ge=0)


class SessionProfile(BaseModel):
    profile_id: str
    mode: Literal["continuous", "weekdays", "weekly"] = "continuous"
    timezone: str = "UTC"
    weekly_open_minute: int | None = Field(default=None, ge=0, lt=7 * 24 * 60)
    weekly_close_minute: int | None = Field(default=None, ge=0, lt=7 * 24 * 60)
    daily_breaks: tuple[tuple[int, int], ...] = ()
    closed_local_dates: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_schedule(self) -> SessionProfile:
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown session timezone: {self.timezone}") from exc
        if self.mode == "weekly" and (
            self.weekly_open_minute is None
            or self.weekly_close_minute is None
            or self.weekly_open_minute == self.weekly_close_minute
        ):
            raise ValueError("weekly session requires distinct open and close boundaries")
        for break_start, break_end in self.daily_breaks:
            if not 0 <= break_start < break_end <= 24 * 60:
                raise ValueError(f"invalid daily session break: {self.profile_id}")
        for closed_date in self.closed_local_dates:
            date.fromisoformat(closed_date)
        return self

    def is_open(self, value: datetime) -> bool:
        stamp = _utc(value).astimezone(ZoneInfo(self.timezone))
        if stamp.date().isoformat() in self.closed_local_dates:
            return False
        if self.mode == "continuous":
            open_now = True
        elif self.mode == "weekdays":
            open_now = stamp.weekday() < 5
        else:
            if self.weekly_open_minute is None or self.weekly_close_minute is None:
                raise ValueError(f"weekly session boundaries are required: {self.profile_id}")
            minute = stamp.weekday() * 24 * 60 + stamp.hour * 60 + stamp.minute
            if self.weekly_open_minute < self.weekly_close_minute:
                open_now = self.weekly_open_minute <= minute < self.weekly_close_minute
            else:
                open_now = minute >= self.weekly_open_minute or minute < self.weekly_close_minute
        if not open_now:
            return False
        minute_of_day = stamp.hour * 60 + stamp.minute
        for break_start, break_end in self.daily_breaks:
            if break_start <= minute_of_day < break_end:
                return False
        return True


class TransformRecipe(BaseModel):
    recipe_id: str
    version: str
    input_dataset: str
    output_dataset: str
    source_timeframe: str
    target_timeframe: str
    allowed_schema_versions: tuple[str, ...]
    session_profile: str = "utc_24x7"
    calendar_profile: str = "continuous"
    aggregation: Literal["ohlcv", "identity"] = "ohlcv"
    partial_bucket_policy: Literal["drop", "allow"] = "drop"
    missing_input_policy: Literal["fail", "allow"] = "fail"
    materialization: Literal["persisted", "ephemeral"] = "persisted"
    publication_policy: Literal["canonical", "research_only"] = "canonical"
    # Optional selector constraints keep recipe semantics explicit without
    # coupling the executor to a provider name.  Empty tuples mean that the
    # recipe is reusable for every value accepted by the input dataset.
    allowed_providers: tuple[str, ...] = ()
    allowed_asset_classes: tuple[str, ...] = ()
    allowed_symbols: tuple[str, ...] = ()
    allowed_price_bases: tuple[str, ...] = ()
    # A repaired source bar can affect the containing output bucket and (for
    # calendar/session transforms) an explicitly configured lookback.  The
    # runner uses this value when constructing a recomputation plan; execution
    # remains a separate, auditable step.
    recompute_lookback_buckets: int = Field(default=1, ge=1)
    quality_profile: str | None = None
    downstream_targets: tuple[str, ...] = ()
    input_recipe_id: str | None = None
    input_recipe_version: str | None = None


class ControlPlaneRegistry:
    """Versioned provider capability and recipe registry."""

    def __init__(self) -> None:
        self._capabilities: dict[str, ProviderCapability] = {}
        self._recipes: dict[tuple[str, str], TransformRecipe] = {}
        self._sessions: dict[str, SessionProfile] = {}
        self._instruments: dict[tuple[str, str], InstrumentMetadata] = {}
        self._quality_profiles: dict[str, QualityProfile] = {}
        self._maintenance_policies: dict[str, MaintenancePolicy] = {}

    def register_capability(self, capability: ProviderCapability) -> ProviderCapability:
        current = self._capabilities.get(capability.provider)
        if current is not None and current != capability:
            raise ValueError(f"provider capability already registered: {capability.provider}")
        self._capabilities[capability.provider] = capability
        return capability

    def capability(self, provider: str) -> ProviderCapability:
        try:
            return self._capabilities[provider]
        except KeyError as exc:
            raise ValueError(f"provider capability is not registered: {provider}") from exc

    def register_recipe(self, recipe: TransformRecipe) -> TransformRecipe:
        key = (recipe.recipe_id, recipe.version)
        current = self._recipes.get(key)
        if current is not None and current != recipe:
            raise ValueError(f"recipe version already registered: {recipe.recipe_id}@{recipe.version}")
        self._recipes[key] = recipe
        return recipe

    def recipe(self, recipe_id: str, version: str) -> TransformRecipe:
        try:
            return self._recipes[(recipe_id, version)]
        except KeyError as exc:
            raise ValueError(f"recipe is not registered: {recipe_id}@{version}") from exc

    def recipes(self) -> tuple[TransformRecipe, ...]:
        return tuple(self._recipes.values())

    def dependency_graph(self) -> dict[str, tuple[dict[str, str], ...]]:
        """Return the recipe dependency graph as an immutable read model."""
        graph: dict[str, list[dict[str, str]]] = {}
        # Keep the graph deterministic and source-first.  The latter matters
        # to maintenance planners that walk the graph after a raw repair:
        # provider_bars must be considered before market_bars rollups even
        # though a lexical sort would place ``market_bars`` first.
        for recipe in sorted(self._recipes.values(), key=lambda item: (
            0 if item.input_dataset == "provider_bars" else 1,
            item.input_dataset, item.recipe_id, item.version,
        )):
            graph.setdefault(recipe.input_dataset, []).append({
                "output_dataset": recipe.output_dataset,
                "recipe_id": recipe.recipe_id,
                "recipe_version": recipe.version,
            })
        return {dataset: tuple(edges) for dataset, edges in graph.items()}

    def register_session(self, profile: SessionProfile) -> SessionProfile:
        current = self._sessions.get(profile.profile_id)
        if current is not None and current != profile:
            raise ValueError(f"session profile already registered: {profile.profile_id}")
        self._sessions[profile.profile_id] = profile
        return profile

    def session(self, profile_id: str) -> SessionProfile:
        try:
            return self._sessions[profile_id]
        except KeyError as exc:
            raise ValueError(f"session profile is not registered: {profile_id}") from exc

    def register_instrument(self, metadata: InstrumentMetadata) -> InstrumentMetadata:
        key = (metadata.provider, metadata.symbol)
        current = self._instruments.get(key)
        if current is not None and current != metadata:
            raise ValueError(f"instrument already registered: {metadata.provider}/{metadata.symbol}")
        self._instruments[key] = metadata
        return metadata

    def instrument(self, provider: str, symbol: str) -> InstrumentMetadata:
        try:
            return self._instruments[(provider, symbol)]
        except KeyError as exc:
            raise ValueError(f"instrument is not registered: {provider}/{symbol}") from exc

    def instruments(self, provider: str | None = None, *, approved_only: bool = True) -> tuple[InstrumentMetadata, ...]:
        """Return a deterministic read model of the approved instrument manifest."""
        values = self._instruments.values()
        if provider is not None:
            values = (item for item in values if item.provider == provider)
        if approved_only:
            values = (item for item in values if item.approved)
        return tuple(sorted(values, key=lambda item: (item.provider, item.symbol)))

    def register_quality_profile(self, profile: QualityProfile) -> QualityProfile:
        current = self._quality_profiles.get(profile.profile_id)
        if current is not None and current != profile:
            raise ValueError(f"quality profile already registered: {profile.profile_id}")
        self._quality_profiles[profile.profile_id] = profile
        return profile

    def quality_profile(self, profile_id: str) -> QualityProfile:
        try:
            return self._quality_profiles[profile_id]
        except KeyError as exc:
            raise ValueError(f"quality profile is not registered: {profile_id}") from exc

    def register_maintenance_policy(self, policy: MaintenancePolicy) -> MaintenancePolicy:
        current = self._maintenance_policies.get(policy.policy_id)
        if current is not None and current != policy:
            raise ValueError(f"maintenance policy already registered: {policy.policy_id}")
        self._maintenance_policies[policy.policy_id] = policy
        return policy

    def maintenance_policy(self, policy_id: str = "default") -> MaintenancePolicy:
        try:
            return self._maintenance_policies[policy_id]
        except KeyError as exc:
            raise ValueError(f"maintenance policy is not registered: {policy_id}") from exc

    def maintenance_policies(self) -> tuple[MaintenancePolicy, ...]:
        return tuple(self._maintenance_policies.values())


@dataclass(frozen=True)
class CoverageResult:
    dataset_id: str
    selector: tuple[tuple[str, str], ...]
    row_count: int
    min_ts: datetime | None
    max_ts: datetime | None
    duplicate_count: int = 0
    gap_count: int = 0
    expected_timestamp_count: int = 0
    closed_timestamp_count: int = 0
    physical_coverage: str = "empty"
    session_coverage: str = "unknown"
    quality_status: str = "not_run"
    readiness_status: str = "not_ready"
    latest_complete_boundary: datetime | None = None
    missing_timestamps: tuple[datetime, ...] = ()
    # Half-open intervals containing only expected, observed bars.  A dataset
    # can therefore be ``degraded`` globally while still exposing safe
    # contiguous ranges for query and derivation consumers.
    ready_intervals: tuple[tuple[datetime, datetime], ...] = ()
    # The cadence is part of the coverage result so a gap-repair plan does not
    # silently assume one-minute bars when the same evaluator is used for a
    # different source timeframe.
    timeframe: timedelta = timedelta(minutes=1)
    calendar_unit: str = "fixed"

    def is_ready_for(self, start: datetime, end: datetime) -> bool:
        """Whether the half-open range is wholly inside a ready interval."""
        if start.tzinfo is None or end.tzinfo is None:
            raise ValueError("coverage range must use timezone-aware timestamps")
        start, end = start.astimezone(timezone.utc), end.astimezone(timezone.utc)
        if end <= start:
            return False
        return any(interval_start <= start and end <= interval_end
                   for interval_start, interval_end in self.ready_intervals)

    def as_dict(self) -> dict:
        return {
            "dataset_id": self.dataset_id,
            "selector": dict(self.selector),
            "row_count": self.row_count,
            "min_ts": self.min_ts.isoformat() if self.min_ts else None,
            "max_ts": self.max_ts.isoformat() if self.max_ts else None,
            "duplicate_count": self.duplicate_count,
            "gap_count": self.gap_count,
            "expected_timestamp_count": self.expected_timestamp_count,
            "closed_timestamp_count": self.closed_timestamp_count,
            "physical_coverage": self.physical_coverage,
            "session_coverage": self.session_coverage,
            "quality_status": self.quality_status,
            "readiness_status": self.readiness_status,
            "latest_complete_boundary": self.latest_complete_boundary.isoformat() if self.latest_complete_boundary else None,
            "missing_timestamp_count": len(self.missing_timestamps),
            "first_missing_ts": self.missing_timestamps[0].isoformat() if self.missing_timestamps else None,
            "ready_interval_count": len(self.ready_intervals),
            "ready_intervals": [
                {"start": start.isoformat(), "end": end.isoformat(),
                 "semantics": "half-open"}
                for start, end in self.ready_intervals
            ],
            "timeframe_seconds": int(self.timeframe.total_seconds()),
            "calendar_unit": self.calendar_unit,
        }


@dataclass(frozen=True)
class IngestWindow:
    start: datetime
    end: datetime
    reason: str
    ordinal: int

    def __post_init__(self) -> None:
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise ValueError("maintenance windows must use timezone-aware timestamps")
        if self.end <= self.start:
            raise ValueError("maintenance window must be non-empty")

    def as_dict(self) -> dict:
        return {"start": self.start.isoformat(), "end": self.end.isoformat(),
                "reason": self.reason, "ordinal": self.ordinal, "semantics": "half-open"}


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(timezone.utc)


def _advance_boundary(value: datetime, *, timeframe: timedelta, calendar_unit: str) -> datetime:
    if calendar_unit == "fixed":
        return value + timeframe
    if calendar_unit == "week":
        return value + timedelta(days=7)
    if calendar_unit == "month":
        year, month = value.year, value.month
        if month == 12:
            return value.replace(year=year + 1, month=1, day=1)
        return value.replace(month=month + 1, day=1)
    raise ValueError(f"unsupported coverage calendar unit: {calendar_unit}")


def evaluate_coverage(*, dataset_id: str, selector: Mapping[str, str], rows: Iterable[Mapping],
                      timeframe: timedelta = timedelta(minutes=1), quality_status: str = "pass",
                      session_profile: SessionProfile | None = None,
                      requested_start: datetime | None = None, requested_end: datetime | None = None,
                      calendar_unit: str = "fixed") -> CoverageResult:
    """Evaluate physical and key coverage without assuming a provider."""
    if timeframe <= timedelta(0):
        raise ValueError("coverage timeframe must be positive")
    timestamps = [_utc(row["bar_ts"]) for row in rows if row.get("bar_ts") is not None]
    unique = sorted(set(timestamps))
    duplicate_count = len(timestamps) - len(unique)
    session_profile = session_profile or SessionProfile(profile_id="continuous")
    expected: list[datetime] = []
    closed_timestamp_count = 0
    if unique or (requested_start is not None and requested_end is not None):
        cursor = _utc(requested_start) if requested_start is not None else unique[0]
        boundary = _utc(requested_end) if requested_end is not None else unique[-1] + timeframe
        while cursor < boundary:
            if session_profile.is_open(cursor):
                expected.append(cursor)
            else:
                closed_timestamp_count += 1
            cursor = _advance_boundary(cursor, timeframe=timeframe, calendar_unit=calendar_unit)
    missing = sorted(set(expected) - set(unique))
    gaps = len(missing)
    minimum, maximum = (unique[0], unique[-1]) if unique else (None, None)
    physical = "empty" if not unique else "present"
    session_coverage = "complete" if expected and not missing else "incomplete" if expected else "unknown"
    # A missing provider bar must not make every other interval unusable.  Keep
    # the global state explicit (``degraded``), while returning the exact
    # half-open contiguous ranges that are safe for downstream consumers.
    observed = set(unique)
    ready_intervals: list[tuple[datetime, datetime]] = []
    interval_start: datetime | None = None
    previous_expected: datetime | None = None
    for stamp in expected:
        is_contiguous = (previous_expected is not None
                         and stamp == previous_expected + timeframe)
        if stamp not in observed or (interval_start is not None and not is_contiguous):
            if interval_start is not None and previous_expected is not None:
                ready_intervals.append((interval_start, previous_expected + timeframe))
            interval_start = None
        if stamp in observed and interval_start is None:
            interval_start = stamp
        previous_expected = stamp
    if interval_start is not None and previous_expected is not None:
        ready_intervals.append((interval_start, previous_expected + timeframe))
    if not unique or duplicate_count or quality_status != "pass":
        readiness = "not_ready"
    elif gaps == 0:
        readiness = "ready"
    elif ready_intervals:
        readiness = "degraded"
    else:
        readiness = "not_ready"
    latest_complete = maximum
    if expected:
        latest_complete = None
        for stamp in expected:
            if stamp not in observed:
                break
            latest_complete = stamp
    return CoverageResult(dataset_id=dataset_id, selector=tuple(sorted(selector.items())), row_count=len(timestamps),
                           min_ts=minimum, max_ts=maximum, duplicate_count=duplicate_count, gap_count=gaps,
                           expected_timestamp_count=len(expected),
                           closed_timestamp_count=closed_timestamp_count,
                           physical_coverage=physical, session_coverage=session_coverage,
                           quality_status=quality_status, readiness_status=readiness,
                           latest_complete_boundary=latest_complete, missing_timestamps=tuple(missing),
                           ready_intervals=tuple(ready_intervals),
                           timeframe=timeframe, calendar_unit=calendar_unit)


def plan_maintenance(*, start: datetime, end: datetime, coverage: CoverageResult | None = None,
                     policy: MaintenancePolicy | None = None, reason: str = "backfill",
                     timeframe: timedelta | None = None,
                     session_profile: SessionProfile | None = None) -> list[IngestWindow]:
    """Create bounded, deterministic half-open windows for backfill/tail/gap repair."""
    start, end = _utc(start), _utc(end)
    if end <= start:
        return []
    policy = policy or MaintenancePolicy()
    span = min(policy.shard_days, policy.max_window_days)
    shard_delta = (timedelta(minutes=policy.shard_minutes)
                   if policy.shard_minutes is not None
                   else timedelta(days=span))
    cadence = timeframe or (coverage.timeframe if coverage is not None else timedelta(minutes=1))
    if cadence <= timedelta(0):
        raise ValueError("maintenance timeframe must be positive")
    # A tail scheduler is expected to be idempotent.  Do not enqueue a second
    # immutable part when the requested interval is already fully covered
    # (including a legal closed session with no expected bars).
    if (
        coverage is not None
        and coverage.readiness_status == "ready"
        and not coverage.missing_timestamps
        and (
            coverage.min_ts is None
            or coverage.max_ts is None
            or (coverage.min_ts <= start and coverage.max_ts + cadence >= end)
            or coverage.session_coverage == "complete"
            or coverage.session_coverage == "unknown"
        )
    ):
        return []
    targets = [(start, end, reason)]
    if coverage is not None and coverage.missing_timestamps:
        missing = list(coverage.missing_timestamps)
        targets = []
        gap_start = gap_end = missing[0]
        for stamp in missing[1:]:
            if stamp - gap_end == cadence:
                gap_end = stamp
            else:
                targets.append((gap_start, gap_end + cadence, "gap_repair"))
                gap_start = gap_end = stamp
        targets.append((gap_start, gap_end + cadence, "gap_repair"))
    session_profile = session_profile or SessionProfile(profile_id="continuous")
    open_targets: list[tuple[datetime, datetime, str]] = []
    for target_start, target_end, target_reason in targets:
        cursor = target_start
        open_start: datetime | None = None
        while cursor < target_end:
            open_now = session_profile.is_open(cursor)
            if open_now and open_start is None:
                open_start = cursor
            if not open_now and open_start is not None:
                open_targets.append((open_start, cursor, target_reason))
                open_start = None
            cursor = min(cursor + cadence, target_end)
        if open_start is not None:
            open_targets.append((open_start, target_end, target_reason))

    windows: list[IngestWindow] = []
    ordinal = 0
    for target_start, target_end, target_reason in open_targets:
        cursor = target_start
        while cursor < target_end:
            window_end = min(cursor + shard_delta, target_end)
            windows.append(IngestWindow(cursor, window_end, target_reason, ordinal))
            cursor = window_end
            ordinal += 1
    return windows


def plan_tail(*, watermark: datetime | None, now: datetime, policy: MaintenancePolicy | None = None) -> list[IngestWindow]:
    policy = policy or MaintenancePolicy()
    now = _utc(now)
    start = _utc(watermark) if watermark is not None else now - timedelta(days=policy.tail_days)
    return plan_maintenance(start=start, end=now, policy=policy, reason="tail")


def immutable_execution_plan(*, run_kind: str, run_scope: str, dataset_id: str,
                             selector: Mapping[str, str], windows: Iterable[IngestWindow],
                             config_digest: str | None = None) -> dict:
    """Serialize the exact control-plane interpretation handed to a worker."""
    return {"run_kind": run_kind, "run_scope": run_scope, "dataset_id": dataset_id,
            "selector": dict(sorted(selector.items())), "windows": [window.as_dict() for window in windows],
            "config_digest": config_digest}
