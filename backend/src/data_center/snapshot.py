"""Bounded operational snapshot shared by API, monitor and acceptance."""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import time
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCOPES = ("production", "acceptance", "migration", "maintenance", "legacy_unclassified")
TERMINAL = {"pass", "failed", "dead_letter"}


@dataclass(frozen=True)
class OperationalSnapshot:
    generated_at: str
    generation_seconds: float
    status: str
    snapshot_age_seconds: float
    metrics: dict
    capacity: dict | None
    temporary_artifacts: dict
    last_successful_backup_at: str | None
    last_successful_recovery_drill_at: str | None

    def as_dict(self) -> dict:
        return asdict(self)


class ReceiptIndex:
    """Atomic, idempotent and rebuildable metadata index for immutable receipts."""

    def __init__(self, evidence_root: Path):
        self.root = Path(evidence_root)
        self.path = self.root / "receipt-index.sqlite"
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.available = True
        try:
            self._initialize(self.path)
        except sqlite3.Error:
            self.available = False

    @staticmethod
    def _initialize(path: Path) -> None:
        with sqlite3.connect(path) as database:
            database.execute(
                "create table if not exists receipts ("
                "reference text primary key, action text not null, result text not null, "
                "completed_at text not null, deployment_id text, fields text not null)"
            )
            database.execute("create index if not exists receipt_latest on receipts(action,result,completed_at desc)")

    def add(self, receipt: dict, reference: str = "") -> None:
        if not self.available:
            raise sqlite3.DatabaseError("receipt index is unavailable")
        key = reference or receipt["receipt_id"]
        deployment_id = receipt.get("deployment_id") or receipt.get("details", {}).get("deployment_id")
        fields = {
            key: receipt.get("details", {}).get(key)
            for key in ("file_count", "bytes", "distinct_device", "byte_identical")
            if key in receipt.get("details", {})
        }
        with sqlite3.connect(self.path) as database:
            database.execute(
                "insert into receipts values (?,?,?,?,?,?) on conflict(reference) do update set "
                "action=excluded.action,result=excluded.result,completed_at=excluded.completed_at,"
                "deployment_id=excluded.deployment_id,fields=excluded.fields",
                (key, receipt["action"], receipt["result"], receipt["completed_at"],
                 deployment_id, json.dumps(fields, sort_keys=True)),
            )

    def latest(self, action: str) -> dict | None:
        if not self.available:
            raise sqlite3.DatabaseError("receipt index is unavailable")
        with sqlite3.connect(self.path, timeout=0.2) as database:
            row = database.execute(
                "select completed_at,deployment_id,reference,fields from receipts "
                "where action=? and result='pass' order by completed_at desc limit 1", (action,),
            ).fetchone()
        if not row:
            return None
        return {"completed_at": row[0], "deployment_id": row[1], "reference": row[2],
                "fields": json.loads(row[3])}

    def rebuild(self) -> dict:
        """Explicit maintenance operation; never called from a request hot path."""
        started = time.monotonic()
        with tempfile.NamedTemporaryFile(dir=self.root, prefix=".receipt-index-", delete=False) as stream:
            temporary = Path(stream.name)
        try:
            temporary.unlink()
            self._initialize(temporary)
            rows = []
            for action_dir in sorted((self.root / "operations").glob("*")):
                if not action_dir.is_dir():
                    continue
                for receipt_path in sorted(action_dir.glob("*.json")):
                    try:
                        receipt = json.loads(receipt_path.read_text())
                        rows.append(_index_row(receipt, str(receipt_path.relative_to(self.root))))
                    except (OSError, ValueError, KeyError, json.JSONDecodeError, sqlite3.Error):
                        continue
            with sqlite3.connect(temporary) as database:
                database.executemany("insert or replace into receipts values (?,?,?,?,?,?)", rows)
            os.replace(temporary, self.path)
            self.available = True
        finally:
            temporary.unlink(missing_ok=True)
        return {"status": "pass", "indexed": len(rows), "duration_seconds": time.monotonic() - started}


def _index_row(receipt: dict, reference: str) -> tuple:
    deployment_id = receipt.get("deployment_id") or receipt.get("details", {}).get("deployment_id")
    return (
        reference, receipt["action"], receipt["result"], receipt["completed_at"],
        deployment_id, json.dumps(receipt.get("details", {}), sort_keys=True),
    )


def count_temporary_artifacts(*roots: Path | None, limit: int = 100_000) -> dict:
    """Scan only explicitly configured Data Center-owned directories."""
    count = 0
    scanned = []
    for configured in roots:
        if configured is None:
            continue
        root = Path(configured)
        scanned.append(str(root))
        if not root.exists():
            continue
        stack = [root]
        while stack:
            directory = stack.pop()
            with os.scandir(directory) as entries:
                for entry in entries:
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(Path(entry.path))
                    elif entry.is_file(follow_symlinks=False) and (
                        entry.name.endswith(".partial") or ".restore.partial" in entry.name
                    ):
                        count += 1
                        if count >= limit:
                            return {"status": "truncated", "count": count, "roots": scanned}
    return {"status": "fresh", "count": count, "roots": scanned}


def _window(rows: list[dict], now: datetime, duration: timedelta) -> dict:
    cutoff = now - duration
    selected = [row for row in rows if row.get("finished_at") and datetime.fromisoformat(row["finished_at"]) >= cutoff]
    passed = sum(row.get("status") == "pass" for row in selected)
    return {"runs": len(selected), "passed": passed,
            "success_rate": passed / len(selected) if selected else None}


def build_snapshot(
    ledger,
    *,
    capacity_policy=None,
    canonical_root: Path | None = None,
    receipt_index: ReceiptIndex | None = None,
    backup_root: Path | None = None,
    restore_staging_root: Path | None = None,
    evidence_root: Path | None = None,
) -> OperationalSnapshot:
    started = time.monotonic()
    now = datetime.now(timezone.utc)
    runs = ledger.list()
    counts = Counter(row.get("status", "unknown") for row in runs)
    scoped = {scope: [row for row in runs if row.get("run_scope", "legacy_unclassified") == scope]
              for scope in SCOPES}
    production = [row for row in scoped["production"] if row.get("status") in TERMINAL]
    lifetime_durations = [
        (datetime.fromisoformat(row["finished_at"]) - datetime.fromisoformat(row["started_at"])).total_seconds()
        for row in runs if row.get("finished_at") and row.get("started_at")
    ]
    production_durations = [
        (datetime.fromisoformat(row["finished_at"]) - datetime.fromisoformat(row["started_at"])).total_seconds()
        for row in production if row.get("finished_at") and row.get("started_at")
    ]
    queue_ages = [(now - datetime.fromisoformat(row["created_at"])).total_seconds()
                  for row in runs if row.get("status") == "queued"]
    lifetime_failures = [row for row in runs if row.get("status") in {"failed", "dead_letter"}]
    production_failures = [row for row in production if row.get("status") in {"failed", "dead_letter"}]
    errors = Counter((row.get("error_type") or "unknown") for row in production_failures)
    providers = Counter((row.get("provider") or "unknown") for row in production_failures)
    lifetime_errors = Counter((row.get("error_type") or "unknown") for row in lifetime_failures)
    lifetime_providers = Counter((row.get("provider") or "unknown") for row in lifetime_failures)
    production_pass = sum(row.get("status") == "pass" for row in production)
    lifetime_terminal = sum(counts[s] for s in TERMINAL)
    production_dead_letters = [row for row in production if row.get("status") == "dead_letter"]
    lifetime_dead_letters = [row for row in runs if row.get("status") == "dead_letter"]
    metrics = {
        "runs_total": len(runs),
        "runs_by_status": {status: counts[status] for status in ("queued", "running", "pass", "failed", "dead_letter")},
        "retry_attempts_total": sum(row.get("retry_count", 0) for row in runs),
        "timeouts_total": sum(sum(error.get("error_type") == "TimeoutError" for error in row.get("attempt_errors", [])) for row in runs),
        "worker_heartbeat_age_seconds": ledger.heartbeat_age_seconds(),
        "queue_depth": counts["queued"],
        "queue_oldest_age_seconds": max(queue_ages, default=0.0),
        "dead_letter_total": len(production_dead_letters),
        "lifetime_dead_letter_total": len(lifetime_dead_letters),
        "dead_letter_by_state": dict(Counter(
            (row.get("dead_letter_state") or {}).get("state", "active")
            for row in production_dead_letters
        )),
        "lifetime_dead_letter_by_state": dict(Counter(
            (row.get("dead_letter_state") or {}).get("state", "active")
            for row in lifetime_dead_letters
        )),
        "success_rate": production_pass / len(production) if production else None,
        "legacy_lifetime_success_rate": counts["pass"] / lifetime_terminal if lifetime_terminal else None,
        "production": {"runs_total": len(production), "success_rate": production_pass / len(production) if production else None},
        "production_sli": {
            "1h": _window(production, now, timedelta(hours=1)),
            "24h": _window(production, now, timedelta(hours=24)),
            "7d": _window(production, now, timedelta(days=7)),
        },
        "runs_by_scope": {scope: len(rows) for scope, rows in scoped.items()},
        "failures_by_error_category": dict(errors.most_common(20)),
        "failures_by_provider": dict(providers.most_common(20)),
        "lifetime_failures_by_error_category": dict(lifetime_errors.most_common(20)),
        "lifetime_failures_by_provider": dict(lifetime_providers.most_common(20)),
        "quality_failed_runs": [row["run_id"] for row in runs
                                if row.get("run_scope", "legacy_unclassified") == "production"
                                and (row.get("quality_summary") or {}).get("status") == "fail"][:100],
        "duration_seconds": {"count": len(production_durations), "sum": sum(production_durations),
                             "max": max(production_durations, default=0.0),
                             "mean": sum(production_durations) / len(production_durations) if production_durations else None},
        "lifetime_duration_seconds": {"count": len(lifetime_durations), "sum": sum(lifetime_durations),
                                      "max": max(lifetime_durations, default=0.0),
                                      "mean": sum(lifetime_durations) / len(lifetime_durations) if lifetime_durations else None},
    }
    status = "fresh"
    backup = recovery = None
    try:
        if receipt_index is None:
            status = "unknown"
        else:
            backup = receipt_index.latest("backup")
            recovery = receipt_index.latest("recovery_drill")
    except sqlite3.Error:
        status = "stale"
    evidence_temporary_root = evidence_root / "temporary" if evidence_root else None
    artifacts = count_temporary_artifacts(backup_root, restore_staging_root, evidence_temporary_root)
    capacity = capacity_policy.inspect(canonical_root).as_dict() if capacity_policy and canonical_root else None
    elapsed = time.monotonic() - started
    return OperationalSnapshot(
        generated_at=now.isoformat(), generation_seconds=elapsed, status=status,
        snapshot_age_seconds=0.0, metrics=metrics, capacity=capacity,
        temporary_artifacts=artifacts,
        last_successful_backup_at=backup["completed_at"] if backup else None,
        last_successful_recovery_drill_at=recovery["completed_at"] if recovery else None,
    )
