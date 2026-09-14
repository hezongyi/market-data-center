"""Long-running scheduler process: bounded ticks, durable receipts, clean shutdown.

systemd owns supervision and restarts; this process owns nothing but the tick
loop.  Which plans exist and when they run comes from the ledger, never from a
unit file (spec 7.1).  Shadow mode is the default: the process records what it
*would* dispatch without creating executions, so an operator can compare it
with the running timers before enabling real dispatch.
"""
from __future__ import annotations

import argparse
import json
import signal
import time
from datetime import datetime, timezone

from data_center.evidence import operation_receipt, write_receipt
from data_center.production_tasks import ProductionTasks
from data_center.runs.ledger import RunLedger
from data_center.scheduler import Scheduler
from data_center.settings import Settings

_stop_requested = False


def _request_stop(signum, frame) -> None:
    """Signal handler: only flips a flag, so the tick loop can exit cleanly."""
    global _stop_requested
    _stop_requested = True


def build_scheduler(settings: Settings, *, dispatch: bool, instance_id: str | None = None) -> Scheduler:
    ledger = RunLedger(settings.ledger_path)
    return Scheduler(
        ledger,
        instance_id=instance_id or settings.scheduler_instance_id,
        dispatch_enabled=dispatch,
        budget=settings.scheduler_tick_budget,
        lease_ttl_seconds=settings.scheduler_lease_seconds,
        planner=ProductionTasks(ledger, canonical_root=settings.canonical_root),
        step_budget=settings.scheduler_step_budget,
    )


def run_tick(scheduler: Scheduler, settings: Settings, *, now: datetime | None = None) -> dict:
    """One tick, then closure, plus their receipt: a shadow run leaves evidence."""
    started_at = (now or datetime.now(timezone.utc)).isoformat()
    # The tick both dispatches due work and closes finished rounds; it never
    # waits for a worker to finish a run.
    result = scheduler.tick(now=now)
    details = {
        "instance_id": result["instance_id"],
        "dispatch_enabled": result["dispatch_enabled"],
        "evaluated": result["evaluated"],
        "dispatched": result.get("dispatched", 0),
        "tick_seconds": result.get("tick_seconds"),
        "due_lag_seconds": max([item.get("late_by_seconds", 0.0) for item in result["decisions"]],
                               default=0.0),
        "coalesced_triggers": sum(item.get("coalesced_count", 1) - 1 for item in result["decisions"]),
        # Receipts stay bounded: a long tick cannot write an unbounded file.
        "decisions": result["decisions"][:50],
        "closed_executions": len((result.get("reconcile") or {}).get("closed", [])),
        "reason": result.get("reason"),
    }
    receipt = operation_receipt(action="scheduler_tick", command="data_center.scheduler_main",
                                started_at=started_at, result="pass", details=details)
    path = write_receipt(settings.evidence_root, receipt)
    return {**result, "receipt": None if path is None else str(path)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Production task scheduler")
    parser.add_argument("--interval-seconds", type=float, default=None,
                        help="seconds between ticks (default: DATACENTER_SCHEDULER_INTERVAL_SECONDS)")
    parser.add_argument("--dispatch", action="store_true",
                        help="enable real dispatch; without it the tick runs in shadow mode")
    parser.add_argument("--once", action="store_true", help="run a single tick and exit")
    parser.add_argument("--instance-id", default=None)
    args = parser.parse_args(argv)

    settings = Settings()
    interval = args.interval_seconds or settings.scheduler_interval_seconds
    dispatch = args.dispatch or settings.scheduler_dispatch_enabled
    scheduler = build_scheduler(settings, dispatch=dispatch, instance_id=args.instance_id)
    identity = {"deployment_id": "development", "software_version": "unknown", "source_commit": "unknown"}
    if settings.deployment_manifest:
        from data_center.deployment import validated_runtime_identity

        identity = validated_runtime_identity(settings.deployment_manifest, settings.evidence_root,
                                              component="scheduler")
    print(json.dumps({"event": "scheduler_started", "dispatch_enabled": dispatch,
                      "interval_seconds": interval,
                      **{key: identity[key] for key in ("deployment_id", "software_version", "source_commit")}}),
          flush=True)
    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)
    while not _stop_requested:
        result = run_tick(scheduler, settings)
        print(json.dumps({"event": "scheduler_tick", "tick_at": result["tick_at"],
                          "evaluated": result["evaluated"], "dispatched": result.get("dispatched", 0),
                          "reason": result.get("reason"), "receipt": result.get("receipt")}), flush=True)
        if args.once or _stop_requested:
            break
        # Sleep in short slices so a stop signal is honoured promptly.
        deadline = time.monotonic() + interval
        while not _stop_requested and time.monotonic() < deadline:
            time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))
    print(json.dumps({"event": "scheduler_stopped"}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
