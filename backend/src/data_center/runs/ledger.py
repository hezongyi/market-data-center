from __future__ import annotations

import builtins
import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

#: Highest schema version this binary understands.  A ledger recorded by a
#: newer binary is refused instead of being silently downgraded.
SCHEMA_VERSION = 2

BASELINE_TABLES = (
    "create table if not exists runs (run_id text primary key, payload text not null)",
    "create table if not exists jobs (job_id text primary key, run_id text not null, status text not null, payload text not null, attempts integer not null default 0, available_at real)",
    "create table if not exists worker_heartbeat (id integer primary key check (id=1), heartbeat text not null)",
    "create table if not exists maintenance_tasks (task_id text primary key, payload text not null, status text not null, updated_at text not null)",
    "create table if not exists dead_letter_state (run_id text primary key, state text not null, acknowledged_at text, resolved_by_run_id text, resolved_at text)",
    "create table if not exists dead_letter_audit (id integer primary key, run_id text not null, action text not null, at text not null, related_run_id text, unique(run_id,action,related_run_id))",
    "create table if not exists quality_findings (id integer primary key, payload text not null)",
    "create table if not exists quality_finding_state (finding_id text primary key, state text not null, updated_at text not null, note text, resolved_by_run_id text)",
    "create table if not exists write_audit (id integer primary key, at text not null, action text not null, actor text, request_id text, task_id text, run_ids text, run_kind text, run_scope text, dataset_id text, selector text, time_range text, outcome text not null, code text, message text)",
)


def _add_columns(conn, table: str, columns: dict[str, str]) -> None:
    existing = {row[1] for row in conn.execute(f"pragma table_info({table})")}
    for name, ddl in columns.items():
        if name not in existing:
            conn.execute(f"alter table {table} add column {name} {ddl}")


def _baseline_migration(conn) -> None:
    """Version 1: the schema every existing production ledger already has.

    Kept idempotent so a database written before migrations were versioned (or
    by an older binary) upgrades in place instead of being recreated.
    """
    for statement in BASELINE_TABLES:
        conn.execute(statement)
    _add_columns(conn, "jobs", {"attempts": "integer not null default 0", "available_at": "real"})
    _add_columns(conn, "dead_letter_state", {"resolved_at": "text"})
    _add_columns(conn, "jobs", {"owner_plan_id": "text", "owner_step_id": "text", "owner_execution_id": "text"})
    _add_columns(conn, "runs", {"plan_id": "text", "execution_id": "text", "step_id": "text",
                                "status": "text", "created_at": "text"})
    _add_columns(conn, "quality_findings", {"finding_id": "text", "dedupe_key": "text", "created_at": "text"})
    for statement in (
        "create unique index if not exists quality_findings_dedupe on quality_findings(dedupe_key)",
        "create index if not exists jobs_status_available on jobs(status, available_at)",
        "create index if not exists jobs_owner_plan on jobs(owner_plan_id) where owner_plan_id is not null",
        "create index if not exists runs_plan_created on runs(plan_id, created_at)",
        "create index if not exists runs_execution_step on runs(execution_id, step_id)",
    ):
        conn.execute(statement)
    conn.execute("create table if not exists production_tasks (task_id text primary key, alias text unique, name text not null, desired_state text not null, definition_version integer not null, payload text not null, created_at text not null, updated_at text not null, deleted_at text)")
    conn.execute("create table if not exists production_task_versions (task_id text not null, definition_version integer not null, payload text not null, config_digest text, created_at text not null, primary key(task_id, definition_version))")
    conn.execute("create table if not exists plan_ownership (ownership_key text primary key, task_id text not null, state text not null, updated_at text not null)")
    conn.execute("create unique index if not exists plan_ownership_active on plan_ownership(ownership_key) where state in ('enabled','paused')")
    conn.execute("create table if not exists production_idempotency (idempotency_key text primary key, task_id text, command text not null, response text not null, created_at text not null)")
    conn.execute("create table if not exists scheduler_state (id integer primary key check(id=1), dispatch_enabled integer not null, heartbeat_at text not null, instance_id text, last_tick_at text)")
    conn.execute("create table if not exists scheduler_leases (lease_key text primary key, owner_id text not null, fencing_token integer not null, expires_at real not null)")
    conn.execute("create table if not exists production_executions (execution_id text primary key, task_id text not null, definition_version integer not null, trigger_source text not null, scheduled_for text, state text not null, outcome text, created_at text not null, finished_at text, coalesced_count integer not null default 0)")
    conn.execute("create unique index if not exists production_execution_slot on production_executions(task_id, scheduled_for) where scheduled_for is not null")
    conn.execute("create unique index if not exists production_one_active_execution on production_executions(task_id) where state in ('pending','running','pausing')")
    conn.execute("create table if not exists production_steps (step_id text primary key, execution_id text not null, stage text not null, window_start text, window_end text, state text not null, block_reason text, run_id text, created_at text not null)")
    conn.execute("create index if not exists production_steps_execution on production_steps(execution_id, created_at)")
    conn.execute("create table if not exists production_progress (task_id text primary key, payload text not null, updated_at text not null)")


def _production_semantics_migration(conn) -> None:
    """Version 2: ownership history, content-bound idempotency and slot identity.

    Ownership rows become append-only history: releasing a key must not require
    deleting the row, otherwise a later plan could never claim the same key and
    the release would silently break on the primary key.  The active-claim
    uniqueness now comes from a partial index over ``enabled``/``paused``.
    """
    ownership_columns = {row[1] for row in conn.execute("pragma table_info(plan_ownership)")}
    if "id" not in ownership_columns:
        conn.execute("alter table plan_ownership rename to plan_ownership_legacy")
        conn.execute("create table plan_ownership (id integer primary key autoincrement, "
                     "ownership_key text not null, task_id text not null, state text not null, "
                     "updated_at text not null)")
        conn.execute("insert into plan_ownership(ownership_key, task_id, state, updated_at) "
                     "select ownership_key, task_id, state, updated_at from plan_ownership_legacy")
        conn.execute("drop table plan_ownership_legacy")
    conn.execute("create index if not exists plan_ownership_task on plan_ownership(task_id)")
    conn.execute("drop index if exists plan_ownership_active")
    conn.execute("create unique index if not exists plan_ownership_active "
                 "on plan_ownership(ownership_key) where state in ('enabled','paused')")
    _add_columns(conn, "production_tasks", {"deleted_digest": "text", "provider": "text",
                                            "symbol": "text", "health": "text",
                                            "next_run_at": "text"})
    conn.execute("create index if not exists production_tasks_filters "
                 "on production_tasks(provider, symbol, desired_state)")
    conn.execute("create index if not exists production_tasks_page "
                 "on production_tasks(updated_at desc, task_id desc)")
    # The due scan must be an indexed range read, never a full table scan that
    # deserialises every definition (spec 7.4.2, AC14).
    conn.execute("create index if not exists production_tasks_due "
                 "on production_tasks(desired_state, next_run_at)")
    _add_columns(conn, "scheduler_state", {"global_dispatch_enabled": "integer not null default 1",
                                           "last_error": "text", "tick_count": "integer not null default 0"})
    _add_columns(conn, "production_idempotency", {"request_fingerprint": "text", "state": "text",
                                                  "completed_at": "text", "actor": "text"})
    _add_columns(conn, "production_executions", {"schedule_revision": "integer not null default 0",
                                                 "updated_at": "text", "error": "text",
                                                 "next_attempt_at": "text", "lease_owner": "text",
                                                 "fencing_token": "integer", "lease_expires_at": "real"})
    # A paused execution is still non-terminal, so it must keep occupying the
    # single-active-execution slot (spec 4).
    conn.execute("drop index if exists production_one_active_execution")
    conn.execute("create unique index if not exists production_one_active_execution "
                 "on production_executions(task_id) where state in ('pending','running','pausing','paused')")
    conn.execute("drop index if exists production_execution_slot")
    conn.execute("create unique index if not exists production_execution_slot "
                 "on production_executions(task_id, schedule_revision, scheduled_for) where scheduled_for is not null")
    conn.execute("create index if not exists production_executions_due "
                 "on production_executions(state, scheduled_for)")
    _add_columns(conn, "production_steps", {"submission_generation": "integer not null default 0",
                                            "updated_at": "text", "attempt_count": "integer not null default 0"})


MIGRATIONS = {
    1: _baseline_migration,
    2: _production_semantics_migration,
}


class IdempotencyConflict(ValueError):
    """The same idempotency key was replayed with different content."""

    code = "idempotency_conflict"


class ProductionConflict(ValueError):
    """A request conflicts with existing production state."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


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
            conn.execute("create table if not exists schema_migrations "
                         "(version integer primary key, applied_at text not null)")
            recorded = conn.execute("pragma user_version").fetchone()[0]
            if recorded > SCHEMA_VERSION:
                raise RuntimeError(
                    f"ledger schema version {recorded} is newer than supported {SCHEMA_VERSION}")
            for version in sorted(MIGRATIONS):
                if version <= recorded:
                    continue
                MIGRATIONS[version](conn)
                conn.execute("insert or replace into schema_migrations(version, applied_at) values (?, ?)",
                             (version, self._now()))
                conn.execute(f"pragma user_version={int(version)}")

    def schema_version(self) -> int:
        with sqlite3.connect(self.path, timeout=5.0) as conn:
            return conn.execute("pragma user_version").fetchone()[0]

    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=5.0)
        conn.execute("pragma busy_timeout=5000")
        return conn

    @contextmanager
    def _transaction(self, conn=None):
        """One short write transaction on one connection.

        Every write path that must be all-or-nothing (task state, delete,
        idempotent actions, batch enqueue) goes through here so a crash cannot
        leave a half-applied change behind.  Passing an existing ``conn`` joins
        the caller's transaction instead of opening a second one, which is what
        lets an idempotent action and its audit commit together.
        """
        if conn is not None:
            yield conn
            return
        own_conn = self._connect()
        try:
            own_conn.execute("begin immediate")
            yield own_conn
            own_conn.commit()
        except BaseException:
            own_conn.rollback()
            raise
        finally:
            own_conn.close()

    def _now(self) -> str:
        return datetime.fromtimestamp(self.clock(), tz=timezone.utc).isoformat()

    # -- production tasks -------------------------------------------------

    ACTIVE_EXECUTION_STATES = ("pending", "running", "pausing", "paused")
    OWNERSHIP_ACTIVE_STATES = ("enabled", "paused")

    @staticmethod
    def _task_row(conn, task_id: str):
        return conn.execute(
            "select name,alias,desired_state,definition_version,payload,created_at,updated_at,deleted_at,"
            "provider,symbol,next_run_at from production_tasks where task_id=?", (task_id,)).fetchone()

    def _has_active_execution(self, conn, task_id: str) -> bool:
        placeholders = ",".join("?" for _ in self.ACTIVE_EXECUTION_STATES)
        row = conn.execute(
            f"select 1 from production_executions where task_id=? and state in ({placeholders}) limit 1",
            (task_id, *self.ACTIVE_EXECUTION_STATES)).fetchone()
        return row is not None

    def _audit(self, conn, *, action: str, outcome: str, actor: str | None = None,
               request_id: str | None = None, task_id: str | None = None,
               code: str | None = None, message: str | None = None, **extra) -> int:
        """Append a write-audit row on an existing connection.

        Keeping this connection-scoped lets a state change and its audit entry
        commit or roll back together (spec 7.2, 5.4).
        """
        stamp = extra.pop("at", None) or self._now()
        cursor = conn.execute(
            "insert into write_audit(at, action, actor, request_id, task_id, run_ids, run_kind, run_scope, "
            "dataset_id, selector, time_range, outcome, code, message) values (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (stamp, action, actor, request_id, task_id, json.dumps(extra.get("run_ids") or []),
             extra.get("run_kind"), extra.get("run_scope"), extra.get("dataset_id"),
             json.dumps(extra.get("selector") or {}, sort_keys=True),
             json.dumps(extra.get("time_range") or {}, sort_keys=True), outcome, code, message),
        )
        return cursor.lastrowid

    def create_production_task(self, *, task_id: str, name: str, payload: dict,
                               ownership_keys: list[str], desired_state: str = "paused",
                               alias: str | None = None, config_digest: str | None = None,
                               provider: str | None = None, symbol: str | None = None,
                               next_run_at: str | None = None, conn=None,
                               audit: dict | None = None) -> dict:
        """Create a versioned production task and claim its ownership atomically.

        ``next_run_at`` is stored as its own indexed column because the due scan
        must not deserialise every definition.  A value left inside ``payload``
        by an older caller is migrated into that column, so the column stays the
        single source of truth.
        """
        if desired_state not in {"enabled", "paused"}:
            raise ValueError("production task must start enabled or paused")
        if "next_run_at" in payload:
            next_run_at = next_run_at or payload.get("next_run_at")
            payload = {key: value for key, value in payload.items() if key != "next_run_at"}
        if next_run_at is not None:
            next_run_at = str(next_run_at)
        stamp = self._now()
        with self._transaction(conn) as tx:
            # ``begin immediate`` serialises these checks with the inserts, so a
            # conflict cannot slip between the two statements.
            if tx.execute("select 1 from production_tasks where task_id=?", (task_id,)).fetchone():
                raise ProductionConflict("task_exists", "production task already exists")
            if alias and tx.execute("select 1 from production_tasks where alias=?", (alias,)).fetchone():
                raise ProductionConflict("alias_exists", "production task alias already exists")
            for key in ownership_keys:
                holder = tx.execute(
                    f"select task_id from plan_ownership where ownership_key=? "
                    f"and state in ({','.join('?' for _ in self.OWNERSHIP_ACTIVE_STATES)})",
                    (key, *self.OWNERSHIP_ACTIVE_STATES)).fetchone()
                if holder is not None:
                    raise ProductionConflict(
                        "ownership_conflict",
                        f"output ownership {key} is already held by plan {holder[0]}")
            tx.execute("insert into production_tasks(task_id,alias,name,desired_state,definition_version,payload,created_at,updated_at,provider,symbol,next_run_at) values (?,?,?,?,?,?,?,?,?,?,?)",
                         (task_id, alias, name, desired_state, 1, json.dumps(payload), stamp, stamp,
                          provider, symbol, next_run_at))
            tx.execute("insert into production_task_versions(task_id,definition_version,payload,config_digest,created_at) values (?,?,?,?,?)",
                         (task_id, 1, json.dumps(payload), config_digest, stamp))
            for key in ownership_keys:
                tx.execute("insert into plan_ownership(ownership_key,task_id,state,updated_at) values (?,?,?,?)",
                             (key, task_id, desired_state, stamp))
            self._write_audit(tx, audit, outcome="created", task_id=task_id)
        return {"task_id": task_id, "alias": alias, "name": name, "desired_state": desired_state,
                "definition_version": 1, "payload": payload, "created_at": stamp, "updated_at": stamp,
                "provider": provider, "symbol": symbol, "next_run_at": next_run_at}

    _TASK_COLUMNS = ("task_id,alias,name,desired_state,definition_version,payload,created_at,updated_at,"
                     "deleted_at,provider,symbol,next_run_at")

    @classmethod
    def _task_document(cls, row) -> dict:
        return {"task_id": row[0], "alias": row[1], "name": row[2], "desired_state": row[3],
                "definition_version": row[4], "payload": json.loads(row[5]), "created_at": row[6],
                "updated_at": row[7], "deleted_at": row[8], "provider": row[9], "symbol": row[10],
                "next_run_at": row[11]}

    def list_production_tasks(self, *, include_deleted: bool = False) -> list[dict]:
        with self._connect() as conn:
            sql = f"select {self._TASK_COLUMNS} from production_tasks"
            if not include_deleted:
                sql += " where deleted_at is null"
            sql += " order by updated_at desc, task_id desc"
            rows = conn.execute(sql).fetchall()
        return [self._task_document(row) for row in rows]

    def get_production_task(self, task_id: str, *, include_deleted: bool = True) -> dict | None:
        """Resolve a task by id or by its migration alias; tombstones stay resolvable."""
        with self._connect() as conn:
            sql = f"select {self._TASK_COLUMNS} from production_tasks where task_id=? or alias=?"
            if not include_deleted:
                sql += " and deleted_at is null"
            row = conn.execute(sql, (task_id, task_id)).fetchone()
        return None if row is None else self._task_document(row)

    def list_production_tasks_page(self, *, provider: str | None = None, symbol: str | None = None,
                                   desired_state: str | None = None, include_deleted: bool = False,
                                   page_size: int = 50,
                                   before: tuple[str, str] | None = None) -> dict:
        """Keyset-paginated task list; filters and paging both stay in SQL."""
        clauses, params = [], []
        if not include_deleted:
            clauses.append("deleted_at is null")
        if provider:
            clauses.append("provider=?")
            params.append(provider)
        if symbol:
            clauses.append("symbol=?")
            params.append(symbol)
        if desired_state:
            clauses.append("desired_state=?")
            params.append(desired_state)
        if before is not None:
            clauses.append("(updated_at < ? or (updated_at = ? and task_id < ?))")
            params.extend([before[0], before[0], before[1]])
        where = f" where {' and '.join(clauses)}" if clauses else ""
        with self._connect() as conn:
            rows = conn.execute(
                f"select {self._TASK_COLUMNS} from production_tasks{where} "
                f"order by updated_at desc, task_id desc limit ?",
                (*params, max(1, page_size) + 1)).fetchall()
        has_more = len(rows) > page_size
        return {"items": [self._task_document(row) for row in rows[:page_size]], "has_more": has_more}

    def ownership_holders(self, keys: builtins.list[str]) -> builtins.list[dict]:
        """Active holders of the given ownership keys, for conflict reporting."""
        if not keys:
            return []
        placeholders = ",".join("?" for _ in keys)
        with self._connect() as conn:
            rows = conn.execute(
                f"select ownership_key, task_id, state from plan_ownership where ownership_key in ({placeholders}) "
                f"and state in ({','.join('?' for _ in self.OWNERSHIP_ACTIVE_STATES)}) "
                "order by ownership_key", (*keys, *self.OWNERSHIP_ACTIVE_STATES)).fetchall()
        return [{"ownership_key": row[0], "task_id": row[1], "state": row[2]} for row in rows]

    def replace_task_ownership(self, conn, task_id: str, keys: builtins.list[str], *,
                               state: str) -> None:
        """Swap the ownership keys a plan holds inside the caller's transaction.

        Editing an output set must stay conflict-free at every instant: keys the
        plan keeps are untouched, new keys are claimed (and rejected if another
        active plan holds them), and dropped keys are released as history.
        """
        current = {row[0] for row in conn.execute(
            f"select ownership_key from plan_ownership where task_id=? and state in "
            f"({','.join('?' for _ in self.OWNERSHIP_ACTIVE_STATES)})",
            (task_id, *self.OWNERSHIP_ACTIVE_STATES)).fetchall()}
        wanted = set(keys)
        for key in sorted(wanted - current):
            holder = conn.execute(
                f"select task_id from plan_ownership where ownership_key=? and task_id<>? "
                f"and state in ({','.join('?' for _ in self.OWNERSHIP_ACTIVE_STATES)})",
                (key, task_id, *self.OWNERSHIP_ACTIVE_STATES)).fetchone()
            if holder is not None:
                raise ProductionConflict("ownership_conflict",
                                         f"output ownership {key} is already held by plan {holder[0]}")
        stamp = self._now()
        for key in sorted(wanted - current):
            conn.execute("insert into plan_ownership(ownership_key,task_id,state,updated_at) values (?,?,?,?)",
                         (key, task_id, state, stamp))
        for key in sorted(current - wanted):
            conn.execute("update plan_ownership set state='released', updated_at=? "
                         "where ownership_key=? and task_id=?", (stamp, key, task_id))

    def active_execution_for_task(self, task_id: str) -> dict | None:
        """Read-only lookup of a plan's single non-terminal execution."""
        with self._connect() as conn:
            return self.active_execution(conn, task_id)

    def get_production_execution(self, execution_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "select execution_id,task_id,definition_version,trigger_source,scheduled_for,state,outcome,"
                "created_at,finished_at,coalesced_count,schedule_revision from production_executions "
                "where execution_id=?", (execution_id,)).fetchone()
        return None if row is None else self._execution_row(row)

    def active_execution(self, conn, task_id: str) -> dict | None:
        placeholders = ",".join("?" for _ in self.ACTIVE_EXECUTION_STATES)
        row = conn.execute(
            "select execution_id,task_id,definition_version,trigger_source,scheduled_for,state,outcome,"
            "created_at,finished_at,coalesced_count,schedule_revision from production_executions "
            f"where task_id=? and state in ({placeholders}) order by created_at limit 1",
            (task_id, *self.ACTIVE_EXECUTION_STATES)).fetchone()
        return None if row is None else self._execution_row(row)

    def acknowledge_config_drift(self, conn, task_id: str, *, expected_version: int | None = None) -> dict:
        """Clear a config-drift hold after an explicit confirmation (spec 5.3)."""
        row = self._task_row(conn, task_id)
        if row is None or row[7] is not None:
            raise KeyError(task_id)
        if expected_version is not None and row[3] != expected_version:
            raise ProductionConflict("version_conflict", "definition version conflict")
        stamp = self._now()
        conn.execute("update production_tasks set health=NULL, updated_at=? where task_id=?",
                     (stamp, task_id))
        return {"task_id": task_id, "health": "healthy", "definition_version": row[3],
                "acknowledged_at": stamp}

    def set_task_health(self, task_id: str, health: str | None) -> None:
        with self._transaction() as conn:
            conn.execute("update production_tasks set health=?, updated_at=? where task_id=?",
                         (health, self._now(), task_id))

    def ownership_of(self, task_id: str) -> builtins.list[dict]:
        with self._connect() as conn:
            rows = conn.execute("select ownership_key,state,updated_at from plan_ownership "
                                "where task_id=? order by id", (task_id,)).fetchall()
        return [{"ownership_key": r[0], "state": r[1], "updated_at": r[2]} for r in rows]

    def _set_task_state(self, conn, task_id: str, state: str, expected_version: int | None) -> dict:
        if state not in {"enabled", "paused", "archived"}:
            raise ValueError("unsupported production task state")
        row = self._task_row(conn, task_id)
        if row is None or row[7] is not None:
            raise KeyError(task_id)
        if expected_version is not None and row[3] != expected_version:
            raise ProductionConflict("version_conflict", "definition version conflict")
        stamp = self._now()
        if state == "archived":
            if self._has_active_execution(conn, task_id):
                raise ProductionConflict("active_execution",
                                         "archive requires that no execution is still active")
            # Release the ownership keys so another plan can take them over;
            # the rows stay as history of who produced the data (spec 3.3).
            conn.execute("update plan_ownership set state='archived', updated_at=? "
                         "where task_id=? and state in ('enabled','paused')", (stamp, task_id))
        elif state == "enabled":
            held = conn.execute(
                f"select ownership_key,task_id from plan_ownership where ownership_key in "
                f"(select ownership_key from plan_ownership where task_id=? and state in ('archived','deleted')) "
                f"and task_id<>? and state in ({','.join('?' for _ in self.OWNERSHIP_ACTIVE_STATES)})",
                (task_id, task_id, *self.OWNERSHIP_ACTIVE_STATES)).fetchall()
            if held:
                raise ProductionConflict(
                    "ownership_conflict",
                    f"output ownership {held[0][0]} is already held by plan {held[0][1]}")
            conn.execute("update plan_ownership set state=?, updated_at=? where task_id=? and state in ('archived','deleted')",
                         (state, stamp, task_id))
        else:
            conn.execute("update plan_ownership set state=?, updated_at=? where task_id=? and state in ('enabled','paused')",
                         (state, stamp, task_id))
        conn.execute("update production_tasks set desired_state=?,updated_at=? where task_id=?", (state, stamp, task_id))
        return {"task_id": task_id, "alias": row[1], "name": row[0], "desired_state": state,
                "definition_version": row[3], "payload": json.loads(row[4]), "created_at": row[5],
                "updated_at": stamp, "provider": row[8], "symbol": row[9]}

    def _write_audit(self, conn, audit: dict | None, *, outcome: str, task_id: str,
                     code: str | None = None, message: str | None = None) -> None:
        """Write the caller's audit entry on this transaction, when one is asked for.

        The caller decides *what* the operation is audited as; the ledger decides
        *when* it becomes durable, so a state change and its audit commit or roll
        back together (spec 7.2, 5.4).
        """
        if not audit:
            return
        self._audit(conn, action=audit.get("action"), actor=audit.get("actor"),
                    request_id=audit.get("request_id"), task_id=task_id, outcome=outcome,
                    code=code, message=message)

    def set_production_task_state(self, task_id: str, state: str, *, expected_version: int | None = None,
                                  conn=None, audit: dict | None = None) -> dict:
        """Move a task between enabled/paused/archived, releasing ownership on archive."""
        if conn is not None:
            result = self._set_task_state(conn, task_id, state, expected_version)
            self._write_audit(conn, audit, outcome="updated", task_id=task_id)
            return result
        with self._transaction() as own_conn:
            result = self._set_task_state(own_conn, task_id, state, expected_version)
            self._write_audit(own_conn, audit, outcome="updated", task_id=task_id)
            return result

    def update_production_task(self, task_id: str, payload: dict, *, expected_version: int,
                               config_digest: str | None = None, conn=None,
                               name: str | None = None, alias: str | None = None,
                               audit: dict | None = None) -> dict:
        if conn is not None:
            return self._update_task(conn, task_id, payload, expected_version, config_digest,
                                     name=name, alias=alias, audit=audit)
        with self._transaction() as own_conn:
            return self._update_task(own_conn, task_id, payload, expected_version, config_digest,
                                     name=name, alias=alias, audit=audit)

    def _update_task(self, conn, task_id: str, payload: dict, expected_version: int,
                     config_digest: str | None, *, name: str | None = None,
                     alias: str | None = None, audit: dict | None = None) -> dict:
        stamp = self._now()
        row = self._task_row(conn, task_id)
        if row is None or row[7] is not None:
            raise KeyError(task_id)
        if row[3] != expected_version:
            raise ProductionConflict("version_conflict", "definition version conflict")
        version = row[3] + 1
        # Editing never changes desired_state: resuming stays an explicit action.
        conn.execute("update production_tasks set payload=?,definition_version=?,updated_at=?,"
                     "name=coalesce(?,name),alias=coalesce(?,alias),provider=?,symbol=? where task_id=?",
                     (json.dumps(payload), version, stamp, name, alias,
                      payload.get("provider"), payload.get("symbol"), task_id))
        conn.execute("insert into production_task_versions(task_id,definition_version,payload,config_digest,created_at) values (?,?,?,?,?)",
                     (task_id, version, json.dumps(payload), config_digest, stamp))
        self._write_audit(conn, audit, outcome="updated", task_id=task_id)
        return {"task_id": task_id, "alias": alias or row[1], "name": name or row[0],
                "desired_state": row[2], "definition_version": version, "payload": payload,
                "created_at": row[5], "updated_at": stamp, "provider": payload.get("provider") or row[8],
                "symbol": payload.get("symbol") or row[9]}

    def _delete_task(self, conn, task_id: str) -> dict:
        row = self._task_row(conn, task_id)
        if row is None or row[7] is not None:
            raise KeyError(task_id)
        if row[2] not in {"paused", "archived"}:
            raise ProductionConflict("invalid_state", "task must be paused or archived before deletion")
        if self._has_active_execution(conn, task_id):
            raise ProductionConflict("active_execution", "task still has an active execution")
        stamp = self._now()
        digest = hashlib.sha256(row[4].encode()).hexdigest()
        conn.execute("update production_tasks set deleted_at=?,deleted_digest=?,updated_at=? where task_id=?",
                     (stamp, digest, stamp, task_id))
        # Deleting the definition releases the key for a future plan; the
        # canonical data, runs and receipts are untouched (spec 5.4).
        conn.execute("update plan_ownership set state='deleted', updated_at=? "
                     "where task_id=? and state in ('enabled','paused')", (stamp, task_id))
        return {"task_id": task_id, "deleted_at": stamp, "definition_version": row[3],
                "payload_digest": digest, "outcome": "deleted"}

    def delete_production_task(self, task_id: str, *, conn=None, audit: dict | None = None) -> dict:
        """Tombstone a task definition while retaining history and ownership auditability."""
        if conn is not None:
            result = self._delete_task(conn, task_id)
            self._write_audit(conn, audit, outcome="deleted", task_id=task_id)
            return result
        try:
            with self._transaction() as own_conn:
                result = self._delete_task(own_conn, task_id)
                self._write_audit(own_conn, audit, outcome="deleted", task_id=task_id)
                return result
        except (KeyError, ProductionConflict) as exc:
            # The rejected attempt is audited too, but in its own transaction
            # because the failed one has already rolled back (spec 5.4).
            with self._transaction() as own_conn:
                self._write_audit(own_conn, audit, outcome="rejected", task_id=task_id,
                                  code=getattr(exc, "code", "not_found"), message=str(exc))
            raise

    @staticmethod
    def _request_fingerprint(task_id: str | None, command: str, request: dict | None) -> str:
        material = json.dumps({"task_id": task_id, "command": command, "request": request or {}},
                              sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(material.encode()).hexdigest()

    def production_idempotent(self, key: str | None, *, task_id: str | None, command: str,
                              action, request: dict | None = None, actor: str | None = None,
                              audit_action: str | None = None):
        """Run a task action once; replaying a key returns its original response.

        The key reservation, the action's writes and the stored response share
        one transaction, so a crash or a lost response can never replay the
        side effects (spec 7.2).  The same key with different content is a
        conflict rather than a silent replay of an unrelated result.
        """
        fingerprint = self._request_fingerprint(task_id, command, request)
        with self._transaction() as conn:
            row = conn.execute("select request_fingerprint,response,state from production_idempotency "
                               "where idempotency_key=?", (key,)).fetchone() if key else None
            if row is not None:
                stored_fingerprint, response, state = row
                if stored_fingerprint is not None and stored_fingerprint != fingerprint:
                    raise IdempotencyConflict(
                        f"idempotency key {key} was already used for a different request")
                if state in (None, "completed"):
                    if audit_action:
                        # A replay is visible in the audit trail but must not
                        # repeat the side effect or pretend to be a new change.
                        self._audit(conn, action=audit_action, actor=actor, task_id=task_id,
                                    outcome="replayed", message=command)
                    return json.loads(response)
            if key:
                conn.execute("insert into production_idempotency(idempotency_key,task_id,command,"
                             "request_fingerprint,response,state,actor,created_at) values (?,?,?,?,?,'in_progress',?,?)",
                             (key, task_id, command, fingerprint, json.dumps(None), actor, self._now()))
            result = action(conn)
            if key:
                conn.execute("update production_idempotency set response=?,state='completed',completed_at=? "
                             "where idempotency_key=?", (json.dumps(result), self._now(), key))
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

    def scheduler_heartbeat(self, *, instance_id: str, dispatch_enabled: bool, error: str | None = None) -> str:
        """Record liveness and the instance's own mode.

        ``dispatch_enabled`` here is the *instance* mode (shadow vs real) and is
        kept for observability only.  The operator's global pause lives in
        ``global_dispatch_enabled`` and is never rewritten by a heartbeat, so a
        restarted scheduler cannot silently resume a paused platform.
        """
        stamp = self._now()
        with self._transaction() as conn:
            conn.execute(
                "insert into scheduler_state(id,dispatch_enabled,heartbeat_at,instance_id,last_tick_at,"
                "global_dispatch_enabled,tick_count,last_error) values (1,?,?,?,?,1,1,?) "
                "on conflict(id) do update set dispatch_enabled=excluded.dispatch_enabled,"
                "heartbeat_at=excluded.heartbeat_at,instance_id=excluded.instance_id,"
                "last_tick_at=excluded.last_tick_at,tick_count=scheduler_state.tick_count+1,"
                "last_error=excluded.last_error",
                (int(dispatch_enabled), stamp, instance_id, stamp, error))
        return stamp

    def set_global_dispatch(self, enabled: bool, *, actor: str | None = None,
                            request_id: str | None = None, conn=None) -> dict:
        """Flip the operator switch that pauses every scheduler-managed plan (spec 3.2)."""
        stamp = self._now()

        def apply(target):
            target.execute(
                "insert into scheduler_state(id,dispatch_enabled,heartbeat_at,instance_id,last_tick_at,"
                "global_dispatch_enabled) values (1,0,?,null,null,?) "
                "on conflict(id) do update set global_dispatch_enabled=excluded.global_dispatch_enabled",
                (stamp, int(enabled)))
            self._audit(target, action="scheduler.pause_dispatch" if not enabled else "scheduler.resume_dispatch",
                        actor=actor, request_id=request_id, outcome="updated",
                        message="dispatch paused" if not enabled else "dispatch resumed")

        if conn is not None:
            apply(conn)
        else:
            with self._transaction() as own_conn:
                apply(own_conn)
        return {"dispatch_enabled": bool(enabled), "updated_at": stamp}

    def dispatch_enabled(self) -> bool:
        """The persisted global dispatch switch; absent state means dispatch is allowed."""
        with self._connect() as conn:
            row = conn.execute("select global_dispatch_enabled from scheduler_state where id=1").fetchone()
        return True if row is None else bool(row[0])

    def scheduler_state(self) -> dict:
        """Read model for the unified scheduler view (spec 3.2, 7.3)."""
        with self._connect() as conn:
            row = conn.execute(
                "select dispatch_enabled,heartbeat_at,instance_id,last_tick_at,coalesce(global_dispatch_enabled,1),"
                "coalesce(tick_count,0),last_error from scheduler_state where id=1").fetchone()
            lease = conn.execute("select owner_id,fencing_token,expires_at from scheduler_leases "
                                 "where lease_key='global'").fetchone()
        if row is None:
            return {"dispatch_enabled": True, "heartbeat_at": None, "instance_id": None,
                    "last_tick_at": None, "tick_count": 0, "last_error": None, "lease": None}
        return {"dispatch_enabled": bool(row[4]), "heartbeat_at": row[1], "instance_id": row[2],
                "last_tick_at": row[3], "instance_dispatch_enabled": bool(row[0]),
                "tick_count": row[5], "last_error": row[6],
                "lease": None if lease is None else {"owner_id": lease[0], "fencing_token": lease[1],
                                                     "expires_at": lease[2]}}

    def create_production_execution(self, *, execution_id: str, task_id: str,
                                    definition_version: int, trigger_source: str,
                                    scheduled_for: str | None = None,
                                    schedule_revision: int = 0, conn=None) -> dict:
        """Create one execution for a task; the single-active-slot index is the arbiter."""
        stamp = self._now()
        with self._transaction(conn) as tx:
            try:
                tx.execute("insert into production_executions(execution_id,task_id,definition_version,trigger_source,scheduled_for,state,created_at,schedule_revision) values (?,?,?,?,?,?,?,?)",
                             (execution_id, task_id, definition_version, trigger_source, scheduled_for,
                              "pending", stamp, schedule_revision))
            except sqlite3.IntegrityError as exc:
                raise ProductionConflict("active_execution",
                                         "task already has an active execution") from exc
        return {"execution_id": execution_id, "task_id": task_id, "definition_version": definition_version,
                "trigger_source": trigger_source, "scheduled_for": scheduled_for, "state": "pending",
                "created_at": stamp, "schedule_revision": schedule_revision}

    def list_due_production_tasks(self, *, now, limit: int = 50) -> list[dict]:
        """Bounded, indexed due scan over enabled plans (spec 7.4.2, AC14)."""
        with self._connect() as conn:
            rows = conn.execute(
                f"select {self._TASK_COLUMNS} from production_tasks "
                "where desired_state='enabled' and deleted_at is null and next_run_at is not null "
                "and next_run_at <= ? order by next_run_at, task_id limit ?",
                (str(now), max(1, limit))).fetchall()
        return [self._task_document(row) for row in rows]

    def set_task_next_run_at(self, *, task_id: str, next_run_at: str | None, conn=None,
                             expected_value: str | None = None) -> bool:
        """Advance (or clear) a plan's next slot.

        ``expected_value`` makes the update a compare-and-swap: a late tick that
        still holds a stale slot cannot rewind a schedule another tick already
        advanced.
        """
        def apply(target):
            stamp = self._now()
            if expected_value is None:
                cursor = target.execute(
                    "update production_tasks set next_run_at=?, updated_at=? where task_id=?",
                    (next_run_at, stamp, task_id))
            else:
                cursor = target.execute(
                    "update production_tasks set next_run_at=?, updated_at=? where task_id=? and next_run_at=?",
                    (next_run_at, stamp, task_id, expected_value))
            return cursor.rowcount > 0

        if conn is not None:
            return apply(conn)
        with self._transaction() as own_conn:
            return apply(own_conn)

    def claim_due_execution(self, *, task_id: str, owner_id: str, scheduled_for: str,
                            definition_version: int, fencing_token: int,
                            trigger_source: str = "scheduled", schedule_revision: int = 0,
                            next_run_at: str | None = None, coalesced_count: int = 0) -> dict | None:
        """Atomically accept one scheduled slot for a plan.

        Acceptance re-verifies everything the tick decided outside the
        transaction: the lease and its fencing token, the task still being
        enabled and at the same definition version, and the global dispatch
        switch.  The unique slot key makes a retry after a lost response return
        the existing execution instead of creating a second logical execution,
        and the next slot is advanced in this same transaction so a crash can
        never accept a slot without moving the plan forward (spec 7.2).
        """
        stamp = self._now()
        execution_id = str(uuid4())
        with self._transaction() as conn:
            lease = conn.execute("select owner_id,fencing_token,expires_at from scheduler_leases where lease_key=?",
                                 ("global",)).fetchone()
            if not lease or lease[0] != owner_id or lease[1] != fencing_token or lease[2] <= self.clock():
                return None
            if conn.execute("select 1 from scheduler_state where id=1 and global_dispatch_enabled=0").fetchone():
                return None
            task = conn.execute("select desired_state,definition_version,deleted_at,next_run_at "
                                "from production_tasks where task_id=?", (task_id,)).fetchone()
            if task is None or task[2] is not None or task[0] != "enabled" or task[1] != definition_version:
                return None
            if task[3] != scheduled_for:
                return None
            existing = self._find_execution(conn, task_id, schedule_revision, scheduled_for)
            if existing is not None:
                return {**existing, "created": False}
            try:
                conn.execute("insert into production_executions(execution_id,task_id,definition_version,"
                             "trigger_source,scheduled_for,state,created_at,schedule_revision,coalesced_count) "
                             "values (?,?,?,?,?,?,?,?,?)",
                             (execution_id, task_id, definition_version, trigger_source, scheduled_for,
                              "pending", stamp, schedule_revision, max(0, coalesced_count)))
            except sqlite3.IntegrityError:
                raced = self._find_execution(conn, task_id, schedule_revision, scheduled_for)
                return None if raced is None else {**raced, "created": False}
            conn.execute("update production_tasks set next_run_at=?, updated_at=? where task_id=?",
                         (next_run_at, stamp, task_id))
            return {"execution_id": execution_id, "task_id": task_id,
                    "definition_version": definition_version, "trigger_source": trigger_source,
                    "scheduled_for": scheduled_for, "state": "pending", "outcome": None,
                    "created_at": stamp, "finished_at": None,
                    "coalesced_count": max(0, coalesced_count), "schedule_revision": schedule_revision,
                    "created": True}

    def _find_execution(self, conn, task_id: str, schedule_revision: int,
                        scheduled_for: str) -> dict | None:
        row = conn.execute(
            "select execution_id,task_id,definition_version,trigger_source,scheduled_for,state,outcome,"
            "created_at,finished_at,coalesced_count,schedule_revision from production_executions "
            "where task_id=? and schedule_revision=? and scheduled_for=?",
            (task_id, schedule_revision, scheduled_for)).fetchone()
        return None if row is None else self._execution_row(row)

    def finish_production_execution(self, execution_id: str, *, state: str, outcome: str,
                                    error: str | None = None) -> dict | None:
        """Close an execution with its terminal state and outcome in one write."""
        if state not in {"completed", "failed", "skipped"}:
            raise ValueError("unsupported terminal execution state")
        if outcome not in {"pass", "degraded", "failed", "skipped"}:
            raise ValueError("unsupported execution outcome")
        stamp = self._now()
        with self._transaction() as conn:
            cursor = conn.execute(
                "update production_executions set state=?, outcome=?, error=?, finished_at=?, updated_at=? "
                "where execution_id=? and state in ('pending','running','pausing','paused')",
                (state, outcome, error, stamp, stamp, execution_id))
            if cursor.rowcount == 0:
                return None
            row = conn.execute(
                "select execution_id,task_id,definition_version,trigger_source,scheduled_for,state,outcome,"
                "created_at,finished_at,coalesced_count,schedule_revision from production_executions "
                "where execution_id=?", (execution_id,)).fetchone()
        return self._execution_row(row)

    @staticmethod
    def _execution_row(row) -> dict:
        return {"execution_id": row[0], "task_id": row[1], "definition_version": row[2],
                "trigger_source": row[3], "scheduled_for": row[4], "state": row[5],
                "outcome": row[6], "created_at": row[7], "finished_at": row[8],
                "coalesced_count": row[9], "schedule_revision": row[10]}

    def list_production_executions(self, task_id: str, *, limit: int = 100) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute("select execution_id,task_id,definition_version,trigger_source,scheduled_for,state,outcome,created_at,finished_at,coalesced_count from production_executions where task_id=? order by created_at desc limit ?", (task_id, max(1, min(limit, 500)))).fetchall()
        return [{"execution_id": r[0], "task_id": r[1], "definition_version": r[2], "trigger_source": r[3],
                 "scheduled_for": r[4], "state": r[5], "outcome": r[6], "created_at": r[7],
                 "finished_at": r[8], "coalesced_count": r[9]} for r in rows]

    def add_production_step(self, *, step_id: str, execution_id: str, stage: str,
                            window_start: str | None = None, window_end: str | None = None) -> dict:
        stamp = self._now()
        with self._connect() as conn:
            conn.execute("insert into production_steps(step_id,execution_id,stage,window_start,window_end,state,created_at) values (?,?,?,?,?,?,?)",
                         (step_id, execution_id, stage, window_start, window_end, "pending", stamp))
        return {"step_id": step_id, "execution_id": execution_id, "stage": stage,
                "window_start": window_start, "window_end": window_end, "state": "pending", "created_at": stamp}

    def list_production_steps(self, execution_id: str, *, limit: int = 100) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute("select step_id,execution_id,stage,window_start,window_end,state,block_reason,run_id,created_at from production_steps where execution_id=? order by created_at limit ?", (execution_id, max(1, min(limit, 500)))).fetchall()
        return [{"step_id": r[0], "execution_id": r[1], "stage": r[2], "window_start": r[3],
                 "window_end": r[4], "state": r[5], "block_reason": r[6], "run_id": r[7], "created_at": r[8]} for r in rows]

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
        with self._transaction() as conn:
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
        with self._transaction() as conn:
            audit_id = self._audit(
                conn, action=entry.get("action"), actor=entry.get("actor"),
                request_id=entry.get("request_id"), task_id=entry.get("task_id"),
                outcome=entry.get("outcome", "unknown"), code=entry.get("code"),
                message=entry.get("message"), at=entry.get("at"), run_ids=entry.get("run_ids"),
                run_kind=entry.get("run_kind"), run_scope=entry.get("run_scope"),
                dataset_id=entry.get("dataset_id"), selector=entry.get("selector"),
                time_range=entry.get("time_range"),
            )
        return {**entry, "audit_id": audit_id, "at": entry.get("at") or self._now()}

    def write_audit_entries(self, limit: int = 100) -> builtins.list[dict]:
        with self._connect() as conn:
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

    def claim_next_job(self, *, scan_limit: int = 50) -> dict | None:
        """Claim the oldest runnable job, honouring ownership and pause rules.

        Pause is decided by ownership columns instead of by reading every queued
        row: a job owned by a paused/archived/deleted plan, by a paused
        execution, or by a dispatcher that is globally paused, is invisible to
        the worker.  Legacy rows without ownership columns keep the historical
        ``job_id`` prefix rule, but only over a bounded candidate scan.
        """
        with self._transaction() as conn:
            candidates = conn.execute(
                """
                select j.job_id, j.run_id, j.payload, j.attempts, j.owner_plan_id
                from jobs j
                where j.status = 'queued'
                  and (j.available_at is null or j.available_at <= ?)
                  and (j.owner_plan_id is null or (
                        exists (select 1 from production_tasks t
                                where t.task_id = j.owner_plan_id
                                  and t.desired_state = 'enabled' and t.deleted_at is null)
                        and not exists (select 1 from scheduler_state s
                                        where s.id = 1 and s.global_dispatch_enabled = 0)))
                  and (j.owner_execution_id is null or not exists (
                        select 1 from production_executions e
                        where e.execution_id = j.owner_execution_id
                          and e.state in ('pausing','paused')))
                order by j.rowid limit ?
                """,
                (self.clock(), max(1, scan_limit)),
            ).fetchall()
            if not candidates:
                return None
            paused_prefixes = [row[0] for row in conn.execute(
                "select task_id from maintenance_tasks where status = 'paused'").fetchall()]
            row = next((candidate for candidate in candidates if not (
                candidate[4] is None and any(
                    json.loads(candidate[2]).get("job_id", "").startswith(prefix)
                    for prefix in paused_prefixes))), None)
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
