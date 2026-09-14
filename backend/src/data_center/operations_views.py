"""Operations read models for the v0.4 workbench.

Everything here is derived from records the platform already keeps: the run
ledger, the durable alert outbox and the receipt index.  Nothing is estimated,
and a state the platform never recorded is reported as unknown.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

# Capacity transitions are recorded by the monitor as alert events; these are
# the only historical capacity states this platform actually persists.
CAPACITY_EVENTS = {"capacity_warning", "capacity_critical", "capacity_recovered", "capacity_ok"}
# Platform action names as they are recorded under the evidence root.  A name
# the platform never writes would render as "no receipt recorded" for a record
# that does exist, so every entry here is one that is actually produced; the
# console renders whatever the API reports rather than keeping its own copy.
RECEIPT_ACTIONS = ("backup", "backup_verify", "restore", "recovery_drill", "capacity_check",
                   "deployment_stage", "deployment_activate", "deployment_rollback",
                   "deployment_runtime_failure", "monitor", "derived_market_bars_maintenance",
                   "real_release_webui_acceptance", "post_release_rehearsal", "scheduler_tick",
                   "retention_audit")


def _iso(value) -> str | None:
    return value if isinstance(value, str) else None


def worker_activity(ledger, *, heartbeat_limit_seconds: float = 60.0) -> dict:
    """Worker liveness, in-flight jobs and queue depth, straight from the ledger."""
    age = ledger.heartbeat_age_seconds()
    running = ledger.running_jobs()
    queue = ledger.job_queue_state()
    return {
        "heartbeat_age_seconds": age,
        "heartbeat_status": ("fresh" if age is not None and age < heartbeat_limit_seconds
                             else "stale" if age is not None else "unknown"),
        "heartbeat_limit_seconds": heartbeat_limit_seconds,
        "running_jobs": [{"job_id": job["job_id"], "run_id": job["run_id"], "attempts": job["attempts"]}
                         for job in running],
        "running_count": len(running),
        "queue": queue,
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }


def capacity_history(alert_sink, live: dict, *, limit: int = 50) -> dict:
    """Recorded capacity transitions plus the live measurement.

    Every sample says where it came from: the recorded transitions were written by the monitor at the
    time they happened, and the live entry carries the measurement source of the policy that produced
    it (`live` or `fixed_acceptance`), so a pinned acceptance value is never quoted as a disk reading.
    """
    events = []
    if alert_sink is not None:
        for event in alert_sink.events():
            if event.get("event") in CAPACITY_EVENTS:
                events.append({
                    "event": event["event"], "created_at": _iso(event.get("created_at")),
                    "status": event.get("status"), "free_ratio": event.get("free_ratio"),
                    "warning_free_ratio": event.get("warning_free_ratio"),
                    "critical_free_ratio": event.get("critical_free_ratio"),
                    "recorded_only": True,
                })
    events.sort(key=lambda item: item.get("created_at") or "", reverse=True)
    live = {**live, "recorded_only": False}
    return {"live": live, "events": events[:limit], "event_count": len(events),
            "measurement_source": live.get("measurement_source"),
            "recorded_only": True,
            "note": "Only capacity transitions the monitor recorded are shown; no history is interpolated."}


#: Units that govern the platform rather than produce data.  The scheduler
#: never starts, stops or reorders them; the list exists so the unified view
#: does not claim to manage everything the host runs (spec 3.4).
GOVERNANCE_UNITS = (
    ("market-data-center-monitor.timer", "monitor"),
    ("market-data-center-smoke.timer", None),
    ("market-data-center-provider-acceptance.timer", None),
    ("market-data-center-retention-audit.timer", "retention_audit"),
    ("market-data-center-backup.timer", "backup"),
    ("market-data-center-restore.timer", "restore"),
    ("market-data-center-release.timer", "deployment_activate"),
)


def _unit_cadence(text: str) -> dict:
    """The declared schedule of a timer, read from its unit file and nothing else."""
    cadence = {}
    for line in text.splitlines():
        for directive in ("OnCalendar", "OnUnitInactiveSec", "OnUnitActiveSec", "OnBootSec", "OnActiveSec"):
            if line.strip().startswith(f"{directive}="):
                cadence[directive] = line.split("=", 1)[1].strip()
    return cadence


def governance_units(*, declared_root, installed: list[str] | None, receipt_index,
                     limit_per_action: int = 1) -> dict:
    """Read-only projection of the timers that govern the platform.

    Two sources are compared and both are named: the repository declares units
    under ``deploy/systemd``, and the host reports what it actually installed.
    A difference is reported, never reconciled (spec 3.4, AC23).
    """
    root = Path(declared_root)
    declared = {path.name: path.read_text() for path in sorted(root.glob("*.timer"))} if root.is_dir() else {}
    installed_set = None if installed is None else set(installed)
    known = set(declared) | (installed_set or set())
    entries = []
    for name, receipt_action in GOVERNANCE_UNITS:
        if name not in known:
            continue
        declaration = ("installed" if installed_set is not None and name in installed_set else
                       "declared_not_installed" if installed_set is not None else "unknown")
        if installed_set is not None and name not in declared:
            declaration = "installed_not_declared"
        receipt = None
        if receipt_action and receipt_index is not None and getattr(receipt_index, "available", False):
            history = receipt_index.history(receipt_action, limit=limit_per_action)
            receipt = history[0] if history else None
        entries.append({
            "unit": name,
            "owner": "market-data-center",
            # Read-only by construction: this projection carries no lifecycle verb.
            "read_only": True,
            "declaration": declaration,
            "cadence": _unit_cadence(declared.get(name, "")),
            "receipt_action": receipt_action,
            "latest_receipt": receipt,
            "evidence": "deploy/systemd unit file and receipt index",
        })
    extra = sorted((installed_set or set()) - set(declared))
    return {"available": installed_set is not None, "units": entries,
            "declared_not_installed": sorted(set(declared) - (installed_set or set())) if installed_set is not None else [],
            "installed_not_declared": [name for name in extra if name.startswith("market-data-center-")],
            "note": "Observational projection: the scheduler cannot start, stop or reorder these units."}


def operations_receipts(receipt_index, *, actions: tuple[str, ...] = RECEIPT_ACTIONS,
                        limit_per_action: int = 5) -> dict:
    """Backup, restore, drill, release and deployment records from the receipt index."""
    if receipt_index is None or not getattr(receipt_index, "available", False):
        return {"available": False, "receipts": [], "latest": {},
                "note": "The receipt index is unavailable, so no operational record can be shown."}
    receipts = []
    latest: dict[str, dict | None] = {}
    for action in actions:
        history = receipt_index.history(action, limit=limit_per_action)
        latest[action] = history[0] if history else None
        receipts.extend(history)
    receipts.sort(key=lambda item: item.get("completed_at") or "", reverse=True)
    return {"available": True, "receipts": receipts, "latest": latest, "note": None}
