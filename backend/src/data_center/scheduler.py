"""Pure scheduling decisions plus one bounded tick for the scheduler process.

Schedule arithmetic stays free of I/O so it can be tested deterministically;
persistence and dispatch are injected through the ledger, which keeps shadow
ticks and real ticks on the same decision path.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from datetime import time as dt_time
from zoneinfo import ZoneInfo

from .instants import parse_instant

UTC = timezone.utc

#: spec 5.1: the first release accepts nothing shorter than five minutes.
MIN_INTERVAL_SECONDS = 300

#: Definition aliases kept readable while the persisted contract stays canonical.
SCHEDULE_ALIASES = {
    "interval": "fixed_rate",
    "interval_fixed_rate": "fixed_rate",
    "interval_fixed_delay": "fixed_delay",
    "calendar": "daily",
}

PERIODIC_SCHEDULES = {"fixed_rate", "fixed_delay"}
SUPPORTED_SCHEDULES = {"manual", "once", "fixed_rate", "fixed_delay", "daily"}


def _require_aware(value: datetime | None, field: str) -> datetime | None:
    """All persisted schedule times are UTC-aware; naive input is a field error."""
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def _canonical_schedule(schedule: str | None) -> str:
    canonical = SCHEDULE_ALIASES.get(schedule or "", schedule or "")
    if canonical not in SUPPORTED_SCHEDULES:
        raise ValueError(f"unsupported schedule: {schedule}")
    return canonical


def _parse_local_time(local_time: str) -> dt_time:
    try:
        hour, minute = (int(part) for part in local_time.split(":", 1))
        return dt_time(hour, minute)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("local_time must be HH:MM") from exc


def _load_zone(timezone_name: str | None) -> ZoneInfo:
    if not timezone_name:
        raise ValueError("daily schedules require an IANA timezone")
    try:
        return ZoneInfo(timezone_name)
    except (KeyError, ValueError) as exc:
        raise ValueError(f"unknown IANA timezone: {timezone_name}") from exc


def _is_missing_local_time(candidate: datetime) -> bool:
    """A wall time skipped by a DST transition does not round-trip through UTC."""
    round_trip = candidate.astimezone(UTC).astimezone(candidate.tzinfo)
    return round_trip.replace(tzinfo=None) != candidate.replace(tzinfo=None)


def _first_instant_after_gap(candidate: datetime) -> datetime:
    """Return the first legal instant at or after a wall time DST skipped.

    ``fold=0`` interprets a missing wall time with the offset in effect before
    the transition (an instant after the gap), ``fold=1`` with the offset after
    it (an instant before the gap).  The transition instant in between is the
    first moment that exists, so a plan at 02:30 in a 02:00→03:00 gap runs at
    03:00 rather than silently moving an hour later.
    """
    zone = candidate.tzinfo
    before_offset = candidate.replace(fold=0).utcoffset()
    low = min(candidate.replace(fold=0).astimezone(UTC), candidate.replace(fold=1).astimezone(UTC))
    high = max(candidate.replace(fold=0).astimezone(UTC), candidate.replace(fold=1).astimezone(UTC))
    while (high - low) > timedelta(seconds=1):
        middle = low + (high - low) / 2
        if middle.astimezone(zone).utcoffset() == before_offset:
            low = middle
        else:
            high = middle
    return high.replace(microsecond=0)


def _daily_candidate(*, now: datetime, zone: ZoneInfo, local_time: str, day_offset: int = 0) -> datetime:
    target = _parse_local_time(local_time)
    local_now = now.astimezone(zone)
    day = (local_now + timedelta(days=day_offset)).date()
    candidate = datetime.combine(day, target, tzinfo=zone)
    if _is_missing_local_time(candidate):
        return _first_instant_after_gap(candidate)
    return candidate.astimezone(UTC)


def _daily_next(*, now: datetime, zone: ZoneInfo, local_time: str, strict: bool = False) -> datetime:
    candidate = _daily_candidate(now=now, zone=zone, local_time=local_time)
    # A repeated (ambiguous) local time resolves to its first occurrence; once
    # that instant has passed the second one is skipped in favour of tomorrow.
    passed = candidate <= now if strict else candidate < now
    if passed:
        candidate = _daily_candidate(now=now, zone=zone, local_time=local_time, day_offset=1)
    return candidate


def next_run_at(*, schedule: str, now: datetime, anchor: datetime | None = None,
                interval_seconds: int | None = None, run_at: datetime | None = None,
                completed_at: datetime | None = None, timezone_name: str | None = None,
                local_time: str | None = None, strict: bool = False) -> datetime | None:
    """Return the next scheduled slot, or ``None`` when the plan has no fixed time.

    The function is pure schedule arithmetic: it never decides whether a slot
    was already consumed.  A ``once`` slot stays visible after it expires so the
    caller can record the misfire (``late_by``) instead of losing the run.

    ``strict`` selects which question is being asked: the default returns the
    earliest slot at or after ``now`` (planning a new plan, showing a preview),
    while ``strict=True`` returns the earliest slot after ``now``, which is what
    advancing a plan that just accepted a slot needs.
    """
    now = _require_aware(now, "now")
    schedule = _canonical_schedule(schedule)
    if schedule == "manual":
        return None
    if schedule == "once":
        return _require_aware(run_at, "run_at")
    if schedule == "fixed_delay":
        if not interval_seconds or interval_seconds <= 0:
            raise ValueError("fixed-delay schedules require positive interval")
        completed = _require_aware(completed_at, "completed_at")
        # Without a completion there is no honest absolute time to show.
        return None if completed is None else completed + timedelta(seconds=interval_seconds)
    if schedule == "daily":
        return _daily_next(now=now, zone=_load_zone(timezone_name), local_time=local_time or "",
                           strict=strict)
    if not interval_seconds or interval_seconds <= 0:
        raise ValueError("interval schedules require positive interval and anchor")
    anchor = _require_aware(anchor, "anchor")
    if anchor is None:
        raise ValueError("interval schedules require positive interval and anchor")
    # A tick that lands exactly on the anchor sees that slot as due rather than
    # already past, so the first planned time is the first run.
    if anchor > now or (anchor == now and not strict):
        return anchor
    steps = int((now - anchor).total_seconds() // interval_seconds) + 1
    return anchor + timedelta(seconds=steps * interval_seconds)


def validate_schedule(definition: dict, *, now: datetime) -> dict:
    """Validate a plan definition at creation/edit time and normalise its fields.

    spec 5.1 rejects naive datetimes, past first starts and non-positive or
    too-short periods here; an already expired *existing* plan is a misfire and
    is handled by :func:`next_run_at` instead of being rejected.
    """
    now = _require_aware(now, "now")
    schedule = _canonical_schedule(definition.get("schedule"))
    normalized = {**definition, "schedule": schedule}
    first_start = _require_aware(
        _parse_iso(definition.get("first_start_at"), "first_start_at"), "first_start_at")
    if first_start is not None and first_start <= now:
        raise ValueError("first_start_at must be in the future")
    if first_start is not None:
        normalized["first_start_at"] = first_start.isoformat()
    if schedule == "manual":
        return normalized
    if schedule == "once":
        run_at = _require_aware(_parse_iso(definition.get("run_at"), "run_at"), "run_at")
        if run_at is None:
            raise ValueError("once schedules require run_at")
        if run_at <= now:
            raise ValueError("run_at must be in the future")
        normalized["run_at"] = run_at.isoformat()
        return normalized
    if schedule in PERIODIC_SCHEDULES:
        interval = definition.get("interval_seconds")
        if not isinstance(interval, int) or isinstance(interval, bool) or interval < MIN_INTERVAL_SECONDS:
            raise ValueError(f"interval_seconds must be an integer >= {MIN_INTERVAL_SECONDS}")
        normalized["interval_seconds"] = interval
        if schedule == "fixed_rate":
            anchor = _require_aware(
                _parse_iso(definition.get("anchor") or definition.get("first_start_at"), "anchor"), "anchor")
            if anchor is None:
                raise ValueError("fixed_rate schedules require an anchor")
            normalized["anchor"] = anchor.isoformat()
        return normalized
    zone = _load_zone(definition.get("timezone"))
    normalized["timezone"] = str(zone)
    normalized["local_time"] = _parse_local_time(definition.get("local_time")).strftime("%H:%M")
    return normalized


def _parse_iso(value, field: str) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    try:
        return parse_instant(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO-8601 datetime") from exc


def reconcile_due(*, now: datetime, desired_state: str, next_at: datetime | None,
                  has_active_execution: bool = False) -> dict:
    """Return an auditable, bounded decision for one plan at a scheduler tick."""
    now = now.astimezone(timezone.utc)
    if desired_state != "enabled":
        return {"action": "hold", "reason": "paused" if desired_state == "paused" else "archived"}
    if has_active_execution:
        return {"action": "hold", "reason": "execution_in_progress"}
    if next_at is None:
        return {"action": "hold", "reason": "no_schedule"}
    if next_at > now:
        return {"action": "hold", "reason": "not_due"}
    return {"action": "start_execution", "scheduled_for": next_at.isoformat(),
            "late_by_seconds": max(0.0, (now - next_at).total_seconds())}


def coalesced_triggers(*, schedule: str, scheduled_for: datetime, now: datetime,
                       interval_seconds: int | None = None,
                       timezone_name: str | None = None, local_time: str | None = None) -> int:
    """How many planned slots a single catch-up run absorbs (spec 5.2).

    A merged trigger never means the missed data range is dropped: the count and
    the first/last planned times are recorded so the backlog stays visible.
    """
    if now <= scheduled_for:
        return 1
    if schedule in {"fixed_rate", "interval"} and interval_seconds:
        return int((now - scheduled_for).total_seconds() // interval_seconds) + 1
    if schedule in {"daily", "calendar"} and timezone_name and local_time:
        zone = _load_zone(timezone_name)
        first = scheduled_for.astimezone(zone).date()
        last = now.astimezone(zone).date()
        missed = 0
        day = first
        while day <= last:
            candidate = datetime.combine(day, _parse_local_time(local_time), tzinfo=zone)
            if _is_missing_local_time(candidate):
                instant = _first_instant_after_gap(candidate)
            else:
                instant = candidate.astimezone(UTC)
            if scheduled_for <= instant <= now:
                missed += 1
            day += timedelta(days=1)
        return max(1, missed)
    return 1


class Scheduler:
    """One bounded scheduler tick; dispatch remains opt-in for shadow mode."""

    def __init__(self, ledger, *, instance_id: str, dispatch_enabled: bool = False,
                 budget: int = 50, lease_ttl_seconds: float = 30.0, clock=None,
                 planner=None, step_budget: int = 8):
        self.ledger = ledger
        self.instance_id = instance_id
        self.dispatch_enabled = dispatch_enabled
        self.budget = budget
        self.lease_ttl_seconds = lease_ttl_seconds
        self.clock = clock
        # The production task module plans what an accepted execution expands;
        # without it a tick only claims the slot (shadow and test runs).
        self.planner = planner
        self.step_budget = step_budget

    def _now(self) -> datetime:
        if self.clock is not None:
            return datetime.fromtimestamp(self.clock(), tz=UTC)
        return datetime.now(UTC)

    def _record_block(self, task_id: str, *, gate: dict, now: datetime) -> None:
        """Let the planner record the refusal; a test double simply has no store."""
        record = getattr(self.planner, "record_dispatch_block", None)
        if record is not None:
            record(task_id, reason=gate.get("block_reason") or "capacity",
                   capacity=gate.get("capacity"), now=now)

    def dispatch_gate(self, now: datetime) -> dict:
        """Ask the planner whether new publishing work may be dispatched.

        A planner without a capacity policy (a test double, or a deployment with
        no canonical root) is treated as unconstrained rather than as blocked.
        """
        gate = getattr(self.planner, "dispatch_gate", None)
        if gate is None:
            return {"allowed": True, "block_reason": None, "capacity": {"status": "unknown"}}
        return gate(now=now)

    def next_slot(self, definition: dict, *, now: datetime) -> datetime | None:
        """The slot after this run, or ``None`` when the plan has no fixed next time."""
        schedule = definition.get("schedule") or {}
        kind = schedule.get("schedule")
        if kind in (None, "manual", "once", "fixed_delay"):
            # ``manual`` never advances on its own, a one-shot slot is consumed,
            # and a fixed-delay plan can only advance on a terminal execution.
            return None
        # Advancing asks for the slot *after* now: accepting a slot and then
        # immediately re-accepting it would run the same moment twice.
        return next_run_at(
            schedule=kind, now=now, anchor=_optional_utc(schedule.get("anchor")),
            interval_seconds=schedule.get("interval_seconds"),
            run_at=_optional_utc(schedule.get("run_at")),
            timezone_name=schedule.get("timezone"), local_time=schedule.get("local_time"),
            strict=True)

    def tick(self, *, now: datetime | None = None, budget: int | None = None) -> dict:
        """Evaluate due plans inside explicit bounds and return an auditable decision list.

        The due scan is an indexed range read with a hard limit, and no catalog,
        network or worker work happens while a write lock is held (spec 7.2,
        AC14).  Shadow mode records what *would* be dispatched without creating
        an execution.
        """
        now = (now or self._now()).astimezone(UTC)
        started = time.perf_counter()
        limit = self.budget if budget is None else budget
        self.ledger.scheduler_heartbeat(instance_id=self.instance_id,
                                        dispatch_enabled=self.dispatch_enabled)
        token = self.ledger.acquire_scheduler_lease("global", self.instance_id,
                                                    ttl_seconds=self.lease_ttl_seconds)
        if token is None:
            return {"instance_id": self.instance_id, "dispatch_enabled": self.dispatch_enabled,
                    "evaluated": 0, "decisions": [], "reason": "lease_unavailable",
                    "tick_at": now.isoformat(), "tick_seconds": 0.0}
        globally_paused = not self.ledger.dispatch_enabled()
        decisions = []
        for task in self.ledger.list_due_production_tasks(now=now.isoformat(), limit=max(0, limit)):
            definition = task.get("payload") or {}
            schedule = definition.get("schedule") or {}
            scheduled_for = _parse_stored(task.get("next_run_at"))
            active = self.ledger.active_execution_for_task(task["task_id"])
            decision = reconcile_due(now=now, desired_state=task["desired_state"], next_at=scheduled_for,
                                     has_active_execution=active is not None)
            decision["task_id"] = task["task_id"]
            decision["definition_version"] = task["definition_version"]
            if decision["action"] == "start_execution" and schedule.get("schedule") in (None, "manual"):
                # A manual plan never runs on a schedule, even if a stale slot
                # is left in the column by an edit.
                decision = {"action": "hold", "reason": "manual", "task_id": task["task_id"],
                            "definition_version": task["definition_version"]}
            elif decision["action"] == "start_execution" and globally_paused:
                decision = {**decision, "action": "hold", "reason": "global_pause"}
            if decision["action"] != "start_execution":
                decisions.append(decision)
                continue
            missed = coalesced_triggers(
                schedule=schedule.get("schedule"), scheduled_for=scheduled_for, now=now,
                interval_seconds=schedule.get("interval_seconds"),
                timezone_name=schedule.get("timezone"), local_time=schedule.get("local_time"))
            decision["coalesced_count"] = missed
            decision["coalesced_from"] = scheduled_for.isoformat()
            decision["coalesced_to"] = now.isoformat()
            following = self.next_slot(definition, now=now)
            decision["next_run_at"] = following.isoformat() if following else None
            if not self.dispatch_enabled:
                decision["action"] = "shadow_start_execution"
                decisions.append(decision)
                continue
            gate = self.dispatch_gate(now)
            if not gate["allowed"]:
                # Capacity protection holds new publishing work back without
                # consuming the slot or piling up executions: the plan stays due
                # with its backlog intact (spec 5.6, AC08).
                decision.update({"action": "capacity_blocked", "reason": gate["block_reason"],
                                 "capacity_status": gate["capacity"]["status"]})
                self._record_block(task["task_id"], gate=gate, now=now)
                decisions.append(decision)
                continue
            execution = self.ledger.claim_due_execution(
                task_id=task["task_id"], owner_id=self.instance_id, scheduled_for=scheduled_for.isoformat(),
                definition_version=task["definition_version"], fencing_token=token,
                schedule_revision=int(schedule.get("schedule_revision") or 0),
                next_run_at=following.isoformat() if following else None, coalesced_count=missed)
            decision["execution_id"] = execution["execution_id"] if execution else None
            decision["execution_created"] = bool(execution and execution.get("created"))
            decision["action"] = "execution_claimed" if execution else "claim_rejected"
            if execution is not None and self.planner is not None:
                # Planning happens outside the claim transaction: it reads
                # policy and must never extend the write lock (spec 7.2.2).
                try:
                    planned = self.planner.dispatch(task=task, execution=execution, now=now,
                                                    step_budget=self.step_budget)
                    decision["steps"] = planned["planned_steps"]
                    decision["runs"] = len(planned["run_ids"])
                    decision["backlog"] = planned["plan"]["backlog"]
                except (KeyError, ValueError) as exc:
                    # A plan that cannot be expanded must not look dispatched.
                    decision["action"] = "dispatch_rejected"
                    decision["reason"] = str(exc)
            decisions.append(decision)
        if self.dispatch_enabled and self.planner is not None and not globally_paused:
            # Executions accepted by run_now/retry carry no schedule slot, so
            # they are planned here instead of waiting for a due scan.
            for execution in self.ledger.list_pending_executions(limit=max(0, limit)):
                task = self.ledger.get_production_task(execution["task_id"])
                if task is None or task["desired_state"] != "enabled" or task["deleted_at"]:
                    continue
                decision = {"task_id": task["task_id"], "action": "execution_claimed",
                            "trigger_source": execution["trigger_source"],
                            "execution_id": execution["execution_id"], "execution_created": False,
                            "definition_version": task["definition_version"]}
                gate = self.dispatch_gate(now)
                if not gate["allowed"]:
                    decision.update({"action": "capacity_blocked", "reason": gate["block_reason"],
                                     "capacity_status": gate["capacity"]["status"]})
                    self._record_block(task["task_id"], gate=gate, now=now)
                    decisions.append(decision)
                    continue
                try:
                    planned = self.planner.dispatch(task=task, execution=execution, now=now,
                                                    step_budget=self.step_budget)
                    decision["steps"] = planned["planned_steps"]
                    decision["runs"] = len(planned["run_ids"])
                    decision["backlog"] = planned["plan"]["backlog"]
                    if planned.get("blocked"):
                        # The planner is the authority on the gate: if capacity
                        # turned critical between the check and the claim, the
                        # round is reported blocked instead of dispatched.
                        decision.update({"action": "capacity_blocked",
                                         "reason": planned["blocked"],
                                         "capacity_status": planned["plan"].get(
                                             "capacity", {}).get("status")})
                except (KeyError, ValueError) as exc:
                    decision["action"] = "dispatch_rejected"
                    decision["reason"] = str(exc)
                decisions.append(decision)
        reconcile = None
        if self.planner is not None:
            # Closure belongs to the same bounded tick: reading finished runs
            # back and advancing a fixed-delay plan must not need a second loop.
            reconcile = self.planner.reconcile(now=now, limit=max(0, limit))
        dispatched = sum(1 for item in decisions if item.get("action") == "execution_claimed")
        return {"instance_id": self.instance_id, "dispatch_enabled": self.dispatch_enabled,
                "evaluated": len(decisions), "dispatched": dispatched, "decisions": decisions,
                "reconcile": reconcile,
                "tick_at": now.isoformat(), "tick_seconds": round(time.perf_counter() - started, 6)}


def _parse_stored(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = parse_instant(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("stored schedule time must be timezone-aware")
    return parsed.astimezone(UTC)


def _optional_utc(value) -> datetime | None:
    return None if value is None else _require_aware(_parse_iso(value, "schedule value"), "schedule value")
