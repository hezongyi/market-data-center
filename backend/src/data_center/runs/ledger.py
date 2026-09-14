from __future__ import annotations

import builtins
import hashlib
import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


class RunLedger:
    """SQLite-backed run ledger.

    Schema changes are applied in order and are safe to repeat.  A clock can
    be injected by tests and schedulers so time-based behaviour is deterministic.
    """

    def __init__(self, path: Path, *, clock=None):
        self.path = path
        self.clock = clock or time.time
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path, timeout=5.0) as conn:
            conn.execute("pragma busy_timeout=5000")
            conn.execute("pragma journal_mode=WAL")
            # Serialize bootstrap/migration work across API, worker and
            # scheduler processes.  Without an immediate transaction two
            # first-openers can both observe a missing column and race ALTER.
            conn.execute("begin immediate")
            conn.execute("create table if not exists schema_migrations (version integer primary key, applied_at text not null)")
            conn.execute("create table if not exists runs (run_id text primary key, payload text not null)")
            conn.execute("create table if not exists jobs (job_id text primary key, run_id text not null, status text not null, payload text not null, attempts integer not null default 0, available_at real)")
            conn.execute("create table if not exists worker_heartbeat (id integer primary key check (id=1), heartbeat text not null)")
            conn.execute("create table if not exists maintenance_tasks (task_id text primary key, payload text not null, status text not null, updated_at text not null)")
            conn.execute("create table if not exists production_tasks (task_id text primary key, alias text unique, name text not null, desired_state text not null, definition_version integer not null, payload text not null, created_at text not null, updated_at text not null, deleted_at text)")
            conn.execute("create table if not exists production_task_versions (task_id text not null, definition_version integer not null, payload text not null, config_digest text, created_at text not null, primary key(task_id, definition_version))")
            conn.execute("create table if not exists plan_ownership (ownership_key text primary key, task_id text not null, state text not null, updated_at text not null)")
            conn.execute("create unique index if not exists plan_ownership_active on plan_ownership(ownership_key) where state in ('enabled','paused')")
            conn.execute("create table if not exists production_idempotency (idempotency_key text primary key, task_id text, command text not null, response text not null, created_at text not null)")
            conn.execute("create table if not exists scheduler_state (id integer primary key check(id=1), dispatch_enabled integer not null, heartbeat_at text not null, instance_id text, last_tick_at text)")
            conn.execute("create table if not exists scheduler_leases (lease_key text primary key, owner_id text not null, fencing_token integer not null, expires_at real not null)")
            conn.execute("create table if not exists dead_letter_state (run_id text primary key, state text not null, acknowledged_at text, resolved_by_run_id text, resolved_at text)")
            conn.execute("create table if not exists dead_letter_audit (id integer primary key, run_id text not null, action text not null, at text not null, related_run_id text, unique(run_id,action,related_run_id))")
            columns = {row[1] for row in conn.execute("pragma table_info(jobs)")}
            if "attempts" not in columns:
                conn.execute("alter table jobs add column attempts integer not null default 0")
            if "available_at" not in columns:
                conn.execute("alter table jobs add column available_at real")
            dead_letter_columns = {row[1] for row in conn.execute("pragma table_info(dead_letter_state)")}
            if "resolved_at" not in dead_letter_columns:
                conn.execute("alter table dead_letter_state add column resolved_at text")
            job_columns = {row[1] for row in conn.execute("pragma table_info(jobs)")}
            for column in ("owner_plan_id", "owner_step_id", "owner_execution_id"):
                if column not in job_columns:
                    conn.execute(f"alter table jobs add column {column} text")
            run_columns = {row[1] for row in conn.execute("pragma table_info(runs)")}
            for column in ("plan_id", "execution_id", "step_id", "status", "created_at"):
                if column not in run_columns:
                    conn.execute(f"alter table runs add column {column} text")
            conn.execute("create index if not exists jobs_status_available on jobs(status, available_at)")
            conn.execute("create index if not exists jobs_owner_plan on jobs(owner_plan_id) where owner_plan_id is not null")
            conn.execute("create index if not exists runs_plan_created on runs(plan_id, created_at)")
            conn.execute("create index if not exists runs_execution_step on runs(execution_id, step_id)")
            conn.execute("insert or ignore into schema_migrations(version, applied_at) values (1, ?)",
                         (self._now(),))
            conn.execute("pragma user_version=1")

    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=5.0)
        conn.execute("pragma busy_timeout=5000")
        return conn

    def _now(self) -> str:
        return datetime.fromtimestamp(self.clock(), tz=timezone.utc).isoformat()

    def create_production_task(self, *, task_id: str, name: str, payload: dict,
                               ownership_keys: list[str], desired_state: str = "paused",
                               alias: str | None = None, config_digest: str | None = None) -> dict:
        """Create a versioned production task and claim its ownership atomically."""
        if desired_state not in {"enabled", "paused"}:
            raise ValueError("production task must start enabled or paused")
        stamp = self._now()
        with self._connect() as conn:
            conn.execute("begin immediate")
            try:
                conn.execute("insert into production_tasks(task_id,alias,name,desired_state,definition_version,payload,created_at,updated_at) values (?,?,?,?,?,?,?,?)",
                             (task_id, alias, name, desired_state, 1, json.dumps(payload), stamp, stamp))
                conn.execute("insert into production_task_versions(task_id,definition_version,payload,config_digest,created_at) values (?,?,?,?,?)",
                             (task_id, 1, json.dumps(payload), config_digest, stamp))
                for key in ownership_keys:
                    conn.execute("insert into plan_ownership(ownership_key,task_id,state,updated_at) values (?,?,?,?)",
                                 (key, task_id, desired_state, stamp))
            except sqlite3.IntegrityError as exc:
                raise ValueError("production task or ownership already exists") from exc
        return {"task_id": task_id, "alias": alias, "name": name, "desired_state": desired_state,
                "definition_version": 1, "payload": payload, "created_at": stamp, "updated_at": stamp}

    def list_production_tasks(self, *, include_deleted: bool = False) -> list[dict]:
        with self._connect() as conn:
            sql = "select task_id,alias,name,desired_state,definition_version,payload,created_at,updated_at,deleted_at from production_tasks"
            if not include_deleted:
                sql += " where deleted_at is null"
            sql += " order by updated_at desc"
            rows = conn.execute(sql).fetchall()
        return [{"task_id": r[0], "alias": r[1], "name": r[2], "desired_state": r[3],
                 "definition_version": r[4], "payload": json.loads(r[5]), "created_at": r[6],
                 "updated_at": r[7], "deleted_at": r[8]} for r in rows]

    def set_production_task_state(self, task_id: str, state: str) -> dict:
        if state not in {"enabled", "paused", "archived"}:
            raise ValueError("unsupported production task state")
        stamp = self._now()
        with self._connect() as conn:
            conn.execute("begin immediate")
            row = conn.execute("select name,alias,definition_version,payload,created_at,deleted_at from production_tasks where task_id=?", (task_id,)).fetchone()
            if row is None or row[5] is not None:
                raise KeyError(task_id)
            conn.execute("update production_tasks set desired_state=?,updated_at=? where task_id=?", (state, stamp, task_id))
            conn.execute("update plan_ownership set state=?,updated_at=? where task_id=?", (state, stamp, task_id))
        return {"task_id": task_id, "alias": row[1], "name": row[0], "desired_state": state,
                "definition_version": row[2], "payload": json.loads(row[3]), "created_at": row[4], "updated_at": stamp}

    def update_production_task(self, task_id: str, payload: dict, *, expected_version: int) -> dict:
        stamp = self._now()
        with self._connect() as conn:
            conn.execute("begin immediate")
            row = conn.execute("select name,alias,desired_state,definition_version,created_at,deleted_at from production_tasks where task_id=?", (task_id,)).fetchone()
            if row is None or row[5] is not None:
                raise KeyError(task_id)
            if row[3] != expected_version:
                raise ValueError("definition version conflict")
            version = row[3] + 1
            conn.execute("update production_tasks set payload=?,definition_version=?,updated_at=? where task_id=?",
                         (json.dumps(payload), version, stamp, task_id))
            conn.execute("insert into production_task_versions(task_id,definition_version,payload,created_at) values (?,?,?,?)",
                         (task_id, version, json.dumps(payload), stamp))
        return {"task_id": task_id, "alias": row[1], "name": row[0], "desired_state": row[2],
                "definition_version": version, "payload": payload, "created_at": row[4], "updated_at": stamp}

    def delete_production_task(self, task_id: str) -> dict:
        """Tombstone a task definition while retaining history and ownership auditability."""
        stamp = self._now()
        with self._connect() as conn:
            conn.execute("begin immediate")
            row = conn.execute("select desired_state,definition_version,payload from production_tasks where task_id=? and deleted_at is null", (task_id,)).fetchone()
            if row is None:
                raise KeyError(task_id)
            if row[0] not in {"paused", "archived"}:
                raise ValueError("task must be paused or archived before deletion")
            conn.execute("update production_tasks set deleted_at=?,updated_at=? where task_id=?", (stamp, stamp, task_id))
            conn.execute("delete from plan_ownership where task_id=?", (task_id,))
        return {"task_id": task_id, "deleted_at": stamp, "definition_version": row[1],
                "payload_digest": hashlib.sha256(row[2].encode()).hexdigest()}

    def production_idempotent(self, key: str | None, *, task_id: str | None, command: str, action):
        """Run a task action once; replaying a key returns its original response."""
        if not key:
            return action()
        with self._connect() as conn:
            row = conn.execute("select response from production_idempotency where idempotency_key=?", (key,)).fetchone()
        if row:
            return json.loads(row[0])
        result = action()
        with self._connect() as conn:
            conn.execute("insert or ignore into production_idempotency(idempotency_key,task_id,command,response,created_at) values (?,?,?,?,?)",
                         (key, task_id, command, json.dumps(result), self._now()))
        return result

    def acquire_scheduler_lease(self, lease_key: str, owner_id: str, *, ttl_seconds: float = 30.0) -> int | None:
        now = self.clock()
        with self._connect() as conn:
            conn.execute("begin immediate")
            row = conn.execute("select owner_id,fencing_token,expires_at from scheduler_leases where lease_key=?", (lease_key,)).fetchone()
            if row and row[0] != owner_id and row[2] > now:
                return None
            token = (row[1] + 1) if row else 1
            conn.execute("insert into scheduler_leases(lease_key,owner_id,fencing_token,expires_at) values (?,?,?,?) on conflict(lease_key) do update set owner_id=excluded.owner_id,fencing_token=excluded.fencing_token,expires_at=excluded.expires_at",
                         (lease_key, owner_id, token, now + ttl_seconds))
            return token

    def scheduler_heartbeat(self, *, instance_id: str, dispatch_enabled: bool) -> str:
        stamp = self._now()
        with self._connect() as conn:
            conn.execute("insert into scheduler_state(id,dispatch_enabled,heartbeat_at,instance_id,last_tick_at) values (1,?,?,?,?) on conflict(id) do update set dispatch_enabled=excluded.dispatch_enabled,heartbeat_at=excluded.heartbeat_at,instance_id=excluded.instance_id,last_tick_at=excluded.last_tick_at",
                         (int(dispatch_enabled), stamp, instance_id, stamp))
        return stamp

    def put(self, run_id: str, payload: dict) -> None:
        with self._connect() as conn:
            conn.execute("begin immediate")
            row = conn.execute("select payload from runs where run_id=?", (run_id,)).fetchone()
            original = json.loads(row[0]) if row else {}
            self._assert_immutable(original, payload)
            if original.get("status") in {"pass", "failed", "dead_letter"} and original != payload:
                raise ValueError("terminal receipt is immutable")
            merged = {**original, **payload}
            conn.execute("insert into runs(run_id,payload,status,created_at,plan_id,execution_id,step_id) values (?, ?, ?, ?, ?, ?, ?) "
                         "on conflict(run_id) do update set payload=excluded.payload,status=excluded.status",
                         (run_id, json.dumps(merged), merged.get("status"), merged.get("created_at"),
                          merged.get("plan_id"), merged.get("execution_id"), merged.get("step_id")))

    @staticmethod
    def _assert_immutable(original: dict, incoming: dict) -> None:
        if not original:
            return
        for field in ("run_id", "job_id", "dataset_id", "run_scope", "run_kind", "execution_plan"):
            if field in incoming and incoming[field] != original.get(field):
                raise ValueError(f"{field} is immutable")

    def update(self, run_id: str, **fields) -> None:
        payload = self.get(run_id)
        payload.update(fields)
        self.put(run_id, payload)

    def list(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute("select payload from runs order by rowid desc").fetchall()
            states = {row[0]: {"state": row[1], "acknowledged_at": row[2], "resolved_by_run_id": row[3],
                               "resolved_at": row[4]}
                      for row in conn.execute("select run_id,state,acknowledged_at,resolved_by_run_id,resolved_at from dead_letter_state")}
        payloads = [json.loads(row[0]) for row in rows]
        for payload in payloads:
            if payload["run_id"] in states:
                payload["dead_letter_state"] = states[payload["run_id"]]
        return payloads

    def get(self, run_id: str) -> dict:
        with self._connect() as conn:
            row = conn.execute("select payload from runs where run_id = ?", (run_id,)).fetchone()
            state = conn.execute("select state,acknowledged_at,resolved_by_run_id,resolved_at from dead_letter_state where run_id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(run_id)
        payload = json.loads(row[0])
        if state:
            payload["dead_letter_state"] = {"state": state[0], "acknowledged_at": state[1],
                                            "resolved_by_run_id": state[2], "resolved_at": state[3]}
        return payload

    def upsert_maintenance_task(self, task_id: str, payload: dict, status: str = "queued") -> None:
        with self._connect() as conn:
            conn.execute("insert into maintenance_tasks(task_id,payload,status,updated_at) values (?,?,?,?) on conflict(task_id) do update set payload=excluded.payload,status=excluded.status,updated_at=excluded.updated_at", (task_id, json.dumps(payload), status, self._now()))

    def list_maintenance_tasks(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute("select task_id,payload,status,updated_at from maintenance_tasks order by updated_at desc").fetchall()
        runs = {item["run_id"]: item for item in self.list()}
        result = []
        for row in rows:
            item = {**json.loads(row[1]), "task_id": row[0], "status": row[2], "updated_at": row[3]}
            item.setdefault("schedule", "manual")
            if row[2] != "paused":
                related = [runs[rid] for rid in item.get("run_ids", []) if rid in runs]
                states = {str(run.get("status", "queued")) for run in related}
                item["status"] = "failed" if "failed" in states else "running" if "running" in states else "succeeded" if related and states <= {"pass", "succeeded"} else "queued"
                item["recent_error"] = next((run.get("error") or run.get("message") for run in reversed(related) if run.get("status") == "failed"), None)
                item["recent_run_id"] = related[-1].get("run_id") if related else None
                item["recent_run_at"] = related[-1].get("created_at") if related else None
            result.append(item)
        return result

    def update_maintenance_task_status(self, task_id: str, status: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute("select payload from maintenance_tasks where task_id=?", (task_id,)).fetchone()
            if row is None: return None
            now = self._now()
            conn.execute("update maintenance_tasks set status=?, updated_at=? where task_id=?", (status, now, task_id))
            return {**json.loads(row[0]), "task_id": task_id, "status": status, "updated_at": now}

    def findings(self) -> builtins.list[dict]:
        with self._connect() as conn:
            conn.execute("create table if not exists quality_findings (id integer primary key, payload text not null)")
            rows = conn.execute("select payload from quality_findings order by id desc").fetchall()
        return self._with_finding_state([json.loads(row[0]) for row in rows])

    def _with_finding_state(self, payloads: builtins.list[dict]) -> builtins.list[dict]:
        states = self.finding_states()
        records = []
        for payload in payloads:
            state = states.get(payload.get("finding_id"))
            records.append({**payload, "state": (state or {}).get("state", payload.get("state", "open")),
                            "state_updated_at": (state or {}).get("updated_at"),
                            "resolved_by_run_id": (state or {}).get("resolved_by_run_id")})
        return records

    def add_findings(self, payloads: builtins.list[dict]) -> None:
        with self._connect() as conn:
            conn.execute("create table if not exists quality_findings (id integer primary key, payload text not null)")
            conn.executemany("insert into quality_findings(payload) values (?)", [(json.dumps(item),) for item in payloads])

    @staticmethod
    def _finding_dedupe_key(payload: dict) -> str:
        """Identity of an observed defect, independent of when it was re-checked.

        The same defect re-detected by a later run must keep one identity so the
        console can distinguish "still present" from "newly introduced" without
        rewriting the run that reported it.
        """
        selector = payload.get("selector") if isinstance(payload.get("selector"), dict) else {}
        identity = {
            "dataset_id": payload.get("dataset_id"),
            "code": payload.get("code"),
            "provider": payload.get("provider") or selector.get("provider"),
            "symbol": payload.get("symbol") or selector.get("symbol"),
            "timeframe": payload.get("timeframe") or selector.get("timeframe"),
            "series_id": payload.get("series_id") or selector.get("series_id"),
            "bar_ts": payload.get("bar_ts"),
            "observation_date": payload.get("observation_date"),
            "message": payload.get("message"),
        }
        return hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def record_findings(self, payloads: builtins.list[dict]) -> builtins.list[dict]:
        """Persist findings idempotently and return the stored records.

        Findings never mutate terminal run receipts; they are additive records
        that carry their own stable ``finding_id`` and handling state.
        """
        stamp = self._now()
        stored_ids: builtins.list[str] = []
        with self._connect() as conn:
            conn.execute("create table if not exists quality_findings (id integer primary key, payload text not null)")
            columns = {row[1] for row in conn.execute("pragma table_info(quality_findings)")}
            for column in ("finding_id", "dedupe_key", "created_at"):
                if column not in columns:
                    conn.execute(f"alter table quality_findings add column {column} text")
            conn.execute("create unique index if not exists quality_findings_dedupe "
                         "on quality_findings(dedupe_key)")
            for payload in payloads:
                record = {key: value for key, value in payload.items() if value is not None}
                dedupe_key = self._finding_dedupe_key(record)
                finding_id = record.get("finding_id") or f"finding-{dedupe_key[:16]}"
                record["finding_id"] = finding_id
                record.setdefault("state", "open")
                existing = conn.execute("select payload from quality_findings where dedupe_key=?",
                                        (dedupe_key,)).fetchone()
                if existing is not None:
                    # Re-observing a defect keeps its identity and first
                    # observer, and records that it is still present instead of
                    # rewriting the run that reported it.
                    previous = json.loads(existing[0])
                    record = {**previous, **record, "run_id": previous.get("run_id"),
                              "occurrence_count": int(previous.get("occurrence_count") or 1) + 1,
                              "first_observed_at": previous.get("first_observed_at") or previous.get("observed_at"),
                              "last_observed_at": stamp, "last_run_id": record.get("run_id")}
                    conn.execute("update quality_findings set payload=? where dedupe_key=?",
                                 (json.dumps(record), dedupe_key))
                else:
                    record = {**record, "occurrence_count": 1, "first_observed_at": stamp,
                              "last_observed_at": stamp, "last_run_id": record.get("run_id")}
                    conn.execute(
                        "insert or ignore into quality_findings(finding_id, dedupe_key, created_at, payload) "
                        "values (?, ?, ?, ?)",
                        (finding_id, dedupe_key, stamp, json.dumps(record)),
                    )
                stored_ids.append(finding_id)
        return [item for item in self.findings() if item.get("finding_id") in set(stored_ids)]

    def finding_states(self) -> dict[str, dict]:
        with self._connect() as conn:
            conn.execute("create table if not exists quality_finding_state "
                         "(finding_id text primary key, state text not null, updated_at text not null, "
                         "note text, resolved_by_run_id text)")
            rows = conn.execute("select finding_id, state, updated_at, note, resolved_by_run_id "
                                "from quality_finding_state").fetchall()
        return {row[0]: {"finding_id": row[0], "state": row[1], "updated_at": row[2],
                         "note": row[3], "resolved_by_run_id": row[4]} for row in rows}

    def set_finding_state(self, finding_id: str, state: str, *, note: str | None = None,
                          resolved_by_run_id: str | None = None) -> dict:
        if state not in {"open", "acknowledged", "resolved"}:
            raise ValueError("unsupported finding state")
        known = {item.get("finding_id") for item in self.findings()}
        if finding_id not in known:
            raise KeyError(finding_id)
        stamp = self._now()
        with self._connect() as conn:
            conn.execute("create table if not exists quality_finding_state "
                         "(finding_id text primary key, state text not null, updated_at text not null, "
                         "note text, resolved_by_run_id text)")
            conn.execute(
                "insert into quality_finding_state(finding_id, state, updated_at, note, resolved_by_run_id) "
                "values (?, ?, ?, ?, ?) on conflict(finding_id) do update set "
                "state=excluded.state, updated_at=excluded.updated_at, note=excluded.note, "
                "resolved_by_run_id=excluded.resolved_by_run_id",
                (finding_id, state, stamp, note, resolved_by_run_id),
            )
        return {"finding_id": finding_id, "state": state, "updated_at": stamp, "note": note,
                "resolved_by_run_id": resolved_by_run_id}

    def record_write_audit(self, entry: dict) -> dict:
        """Append one write-operation audit record and return it.

        The audit trail is additive; it never rewrites a run receipt and never
        stores credentials, only a non-reversible actor fingerprint.
        """
        stamp = entry.get("at") or self._now()
        with self._connect() as conn:
            conn.execute(
                "create table if not exists write_audit (id integer primary key, at text not null, "
                "action text not null, actor text, request_id text, task_id text, run_ids text, "
                "run_kind text, run_scope text, dataset_id text, selector text, time_range text, "
                "outcome text not null, code text, message text)"
            )
            cursor = conn.execute(
                "insert into write_audit(at, action, actor, request_id, task_id, run_ids, run_kind, run_scope, "
                "dataset_id, selector, time_range, outcome, code, message) values (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (stamp, entry.get("action"), entry.get("actor"), entry.get("request_id"), entry.get("task_id"),
                 json.dumps(entry.get("run_ids") or []), entry.get("run_kind"), entry.get("run_scope"),
                 entry.get("dataset_id"), json.dumps(entry.get("selector") or {}, sort_keys=True),
                 json.dumps(entry.get("time_range") or {}, sort_keys=True), entry.get("outcome", "unknown"),
                 entry.get("code"), entry.get("message")),
            )
            audit_id = cursor.lastrowid
        return {**entry, "audit_id": audit_id, "at": stamp}

    def write_audit_entries(self, limit: int = 100) -> builtins.list[dict]:
        with self._connect() as conn:
            conn.execute(
                "create table if not exists write_audit (id integer primary key, at text not null, "
                "action text not null, actor text, request_id text, task_id text, run_ids text, "
                "run_kind text, run_scope text, dataset_id text, selector text, time_range text, "
                "outcome text not null, code text, message text)"
            )
            rows = conn.execute(
                "select id, at, action, actor, request_id, task_id, run_ids, run_kind, run_scope, dataset_id, "
                "selector, time_range, outcome, code, message from write_audit order by id desc limit ?",
                (limit,),
            ).fetchall()
        return [{
            "audit_id": row[0], "at": row[1], "action": row[2], "actor": row[3], "request_id": row[4],
            "task_id": row[5], "run_ids": json.loads(row[6] or "[]"), "run_kind": row[7], "run_scope": row[8],
            "dataset_id": row[9], "selector": json.loads(row[10] or "{}"), "time_range": json.loads(row[11] or "{}"),
            "outcome": row[12], "code": row[13], "message": row[14],
        } for row in rows]

    def job_queue_state(self) -> dict:
        """Read-only queue view used by the operations console."""
        with self._connect() as conn:
            rows = conn.execute("select status, count(*), min(available_at) from jobs group by status").fetchall()
        counts = {row[0]: row[1] for row in rows}
        oldest = min([row[2] for row in rows if row[0] == "queued" and row[2] is not None], default=None)
        return {"queued": counts.get("queued", 0), "running": counts.get("running", 0),
                "completed": counts.get("completed", 0), "by_status": counts,
                "oldest_queued_available_at": None if oldest is None else datetime.fromtimestamp(
                    oldest, tz=timezone.utc).isoformat()}

    def enqueue_provider_bars(self, job_payload: dict) -> str:
        return self.enqueue_job(job_payload)

    def enqueue_job(self, job_payload: dict) -> str:
        return self.enqueue_batch([job_payload])[0]

    def enqueue_batch(self, job_payloads: list[dict]) -> list[str]:
        """Atomically enqueue a batch of jobs and their run receipts."""
        if not job_payloads:
            return []
        run_ids: list[str] = []
        stamp = self._now()
        with self._connect() as conn:
            conn.execute("begin immediate")
            for job_payload in job_payloads:
                run_id = str(uuid4())
                payload = {"run_id": run_id, "job_id": job_payload["job_id"], "dataset_id": job_payload["dataset_id"],
                           "provider": job_payload.get("provider"), "request_id": job_payload.get("request_id"),
                           "symbol": job_payload.get("symbol"), "recipe_id": job_payload.get("recipe_id"),
                           "recipe_version": job_payload.get("recipe_version"), "input_snapshot_id": job_payload.get("input_snapshot_id"),
                           "run_scope": job_payload.get("run_scope", "production"), "run_kind": job_payload.get("run_kind", "ingest"),
                           "execution_plan": job_payload.get("execution_plan"), "timeframe": job_payload.get("timeframe"),
                           "asset_class": job_payload.get("asset_class"), "series_id": job_payload.get("series_id"),
                           "price_basis": job_payload.get("price_basis"), "start": job_payload.get("start"), "end": job_payload.get("end"),
                           "status": "queued", "created_at": stamp}
                conn.execute("insert into runs(run_id,payload,status,created_at) values (?, ?, ?, ?)",
                             (run_id, json.dumps(payload), "queued", stamp))
                conn.execute("insert into jobs(job_id, run_id, status, payload, owner_plan_id, owner_step_id, owner_execution_id) values (?, ?, ?, ?, ?, ?, ?)",
                             (str(uuid4()), run_id, "queued", json.dumps(job_payload), job_payload.get("owner_plan_id"), job_payload.get("owner_step_id"), job_payload.get("owner_execution_id")))
                run_ids.append(run_id)
        return run_ids

    def claim_next_job(self) -> dict | None:
        import json

        with self._connect() as conn:
            conn.execute("begin immediate")
            candidates = conn.execute("select job_id, run_id, payload, attempts, owner_plan_id from jobs where status = 'queued' and (available_at is null or available_at <= ?) order by rowid", (self.clock(),)).fetchall()
            paused = {item[0] for item in conn.execute("select task_id from maintenance_tasks where status='paused'").fetchall()}
            row = next((candidate for candidate in candidates
                        if not any(candidate[4] == task_id or (candidate[4] is None and json.loads(candidate[2]).get("job_id", "").startswith(task_id)) for task_id in paused)), None)
            if row is None:
                return None
            conn.execute("update jobs set status = 'running' where job_id = ?", (row[0],))
            run_row = conn.execute("select payload from runs where run_id = ?", (row[1],)).fetchone()
            run = json.loads(run_row[0])
            run["status"] = "running"
            run["started_at"] = self._now()
            conn.execute("update runs set payload = ?, status = ?, created_at = coalesce(created_at, ?) where run_id = ?", (json.dumps(run), "running", run.get("created_at"), row[1]))
            conn.execute("update jobs set attempts = attempts + 1 where job_id = ?", (row[0],))
            return {"job_id": row[0], "run_id": row[1], "payload": json.loads(row[2]), "attempts": row[3] + 1}

    def running_jobs(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute("select job_id, run_id, payload, attempts from jobs where status='running'").fetchall()
        return [{"job_id": r[0], "run_id": r[1], "payload": json.loads(r[2]), "attempts": r[3]} for r in rows]

    def complete_job(self, job_id: str) -> None:
        with self._connect() as conn:
            conn.execute("update jobs set status = 'completed' where job_id = ?", (job_id,))

    def finish_job(self, job_id: str, run_id: str, receipt: dict) -> None:
        with self._connect() as conn:
            conn.execute("begin immediate")
            original = json.loads(conn.execute("select payload from runs where run_id=?", (run_id,)).fetchone()[0])
            attempt_row = conn.execute("select attempts from jobs where job_id=?", (job_id,)).fetchone()
            attempts = attempt_row[0] if attempt_row else 1
            if original.get("status") != "running":
                raise ValueError("only a running job can finish")
            self._assert_immutable(original, receipt)
            payload = {**original, **receipt, "created_at": original.get("created_at"),
                       "attempt_count": attempts, "retry_count": max(0, attempts - 1),
                       "attempt_errors": original.get("attempt_errors", []),
                       "finished_at": self._now(), "error": None, "error_type": None,
                       "failure_stage": None, "retryable": False}
            conn.execute("update runs set payload=? where run_id=?", (json.dumps(payload), run_id))
            conn.execute("update jobs set status='completed' where job_id=?", (job_id,))
            if original.get("retry_of"):
                resolved_at = payload["finished_at"]
                updated = conn.execute(
                    "update dead_letter_state set state='resolved', resolved_by_run_id=?, resolved_at=? "
                    "where run_id=? and state in ('active','acknowledged')",
                    (run_id, resolved_at, original["retry_of"]),
                ).rowcount
                if updated:
                    conn.execute(
                        "insert or ignore into dead_letter_audit(run_id,action,at,related_run_id) values (?,?,?,?)",
                        (original["retry_of"], "resolved", resolved_at, run_id),
                    )

    def fail_job(self, job_id: str, run_id: str, error: str, *, error_type: str | None = None,
                 failure_stage: str = "execute", retryable: bool = True, quality_summary: dict | None = None,
                 delay_seconds: float = 0.0) -> None:
        with self._connect() as conn:
            conn.execute("begin immediate")
            row = conn.execute("select attempts from jobs where job_id = ?", (job_id,)).fetchone()
            attempts = row[0] if row else 1
            original = json.loads(conn.execute("select payload from runs where run_id=?", (run_id,)).fetchone()[0])
            if original.get("status") != "running":
                raise ValueError("only a running job can fail")
            status = "failed" if not retryable else ("dead_letter" if attempts >= 3 else "queued")
            available = self.clock() + delay_seconds * (2 ** max(0, attempts - 1))
            failure = {"attempt": attempts, "error_type": error_type or "IngestError", "failure_stage": failure_stage, "error": error,
                       "retryable": retryable, "at": self._now()}
            payload = {**original, **failure, "status": status, "retry_count": max(0, attempts - 1),
                       "attempt_count": attempts, "attempt_errors": original.get("attempt_errors", []) + [failure],
                       "quality_summary": quality_summary or {"status": "not_run", "finding_count": 0, "findings": []},
                       "next_attempt_at": available if status == "queued" else None}
            if status != "queued":
                payload["finished_at"] = failure["at"]
            conn.execute("update jobs set status = ?, available_at = ? where job_id = ?", (status, available, job_id))
            conn.execute("update runs set payload=? where run_id=?", (json.dumps(payload), run_id))
            if status == "dead_letter":
                conn.execute("insert or ignore into dead_letter_state(run_id,state) values (?,'active')", (run_id,))

    def retry_run(self, run_id: str) -> str:
        with self._connect() as conn:
            conn.execute("begin immediate")
            row = conn.execute("select payload from runs where run_id=?", (run_id,)).fetchone()
            if row is None:
                raise KeyError(run_id)
            original = json.loads(row[0])
            if original["status"] not in {"failed", "dead_letter"}:
                raise ValueError("only failed or dead-letter runs can be retried")
            job = conn.execute("select payload from jobs where run_id=?", (run_id,)).fetchone()
            if job is None:
                raise ValueError("original job request unavailable")
            new_id = str(uuid4())
            request = json.loads(job[0])
            payload = {"run_id": new_id, "job_id": request["job_id"], "dataset_id": request["dataset_id"],
                       "provider": request.get("provider"), "run_scope": original.get("run_scope", "legacy_unclassified"),
                       "run_kind": original.get("run_kind", request.get("run_kind", "ingest")),
                       "status": "queued", "retry_of": run_id,
                       "created_at": self._now()}
            conn.execute("insert into runs(run_id,payload,status,created_at) values (?, ?, ?, ?)",
                         (new_id, json.dumps(payload), "queued", payload["created_at"]))
            conn.execute("insert into jobs(job_id,run_id,status,payload) values (?,?,?,?)", (str(uuid4()), new_id, "queued", job[0]))
        return new_id

    def acknowledge_dead_letter(self, run_id: str) -> dict:
        stamp = self._now()
        with self._connect() as conn:
            conn.execute("begin immediate")
            row = conn.execute("select payload from runs where run_id=?", (run_id,)).fetchone()
            if row is None:
                raise KeyError(run_id)
            if json.loads(row[0]).get("status") != "dead_letter":
                raise ValueError("only dead-letter runs can be acknowledged")
            state = conn.execute("select state from dead_letter_state where run_id=?", (run_id,)).fetchone()
            if state and state[0] == "resolved":
                raise ValueError("resolved dead-letter runs cannot return to acknowledged")
            if state and state[0] == "acknowledged":
                return self.get(run_id)
            conn.execute("insert into dead_letter_state(run_id,state,acknowledged_at) values (?,'acknowledged',?) "
                         "on conflict(run_id) do update set state='acknowledged', acknowledged_at=excluded.acknowledged_at",
                         (run_id, stamp))
            conn.execute(
                "insert or ignore into dead_letter_audit(run_id,action,at,related_run_id) values (?,?,?,null)",
                (run_id, "acknowledged", stamp),
            )
        return self.get(run_id)

    def dead_letter_audit(self, run_id: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "select action,at,related_run_id from dead_letter_audit where run_id=? order by id", (run_id,),
            ).fetchall()
        return [{"action": row[0], "at": row[1], "related_run_id": row[2]} for row in rows]

    def heartbeat(self) -> str:
        stamp = self._now()
        with self._connect() as conn:
            conn.execute("insert into worker_heartbeat(id, heartbeat) values (1, ?) on conflict(id) do update set heartbeat=excluded.heartbeat", (stamp,))
        return stamp

    def heartbeat_age_seconds(self) -> float | None:
        with self._connect() as conn:
            row = conn.execute("select heartbeat from worker_heartbeat where id=1").fetchone()
        if not row:
            return None
        return max(0.0, (datetime.fromtimestamp(self.clock(), tz=timezone.utc) - datetime.fromisoformat(row[0])).total_seconds())
