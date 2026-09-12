from __future__ import annotations

import builtins
import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


class RunLedger:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path) as conn:
            conn.execute("create table if not exists runs (run_id text primary key, payload text not null)")
            conn.execute("create table if not exists jobs (job_id text primary key, run_id text not null, status text not null, payload text not null, attempts integer not null default 0, available_at real)")
            conn.execute("create table if not exists worker_heartbeat (id integer primary key check (id=1), heartbeat text not null)")
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

    def put(self, run_id: str, payload: dict) -> None:
        with sqlite3.connect(self.path) as conn:
            conn.execute("begin immediate")
            row = conn.execute("select payload from runs where run_id=?", (run_id,)).fetchone()
            original = json.loads(row[0]) if row else {}
            self._assert_immutable(original, payload)
            if original.get("status") in {"pass", "failed", "dead_letter"} and original != payload:
                raise ValueError("terminal receipt is immutable")
            conn.execute("insert into runs values (?, ?) on conflict(run_id) do update set payload=excluded.payload",
                         (run_id, json.dumps({**original, **payload})))

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
        with sqlite3.connect(self.path) as conn:
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
        with sqlite3.connect(self.path) as conn:
            row = conn.execute("select payload from runs where run_id = ?", (run_id,)).fetchone()
            state = conn.execute("select state,acknowledged_at,resolved_by_run_id,resolved_at from dead_letter_state where run_id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(run_id)
        payload = json.loads(row[0])
        if state:
            payload["dead_letter_state"] = {"state": state[0], "acknowledged_at": state[1],
                                            "resolved_by_run_id": state[2], "resolved_at": state[3]}
        return payload

    def findings(self) -> builtins.list[dict]:
        with sqlite3.connect(self.path) as conn:
            conn.execute("create table if not exists quality_findings (id integer primary key, payload text not null)")
            rows = conn.execute("select payload from quality_findings order by id desc").fetchall()
        return [json.loads(row[0]) for row in rows]

    def add_findings(self, payloads: builtins.list[dict]) -> None:
        with sqlite3.connect(self.path) as conn:
            conn.execute("create table if not exists quality_findings (id integer primary key, payload text not null)")
            conn.executemany("insert into quality_findings(payload) values (?)", [(json.dumps(item),) for item in payloads])

    def enqueue_provider_bars(self, job_payload: dict) -> str:
        return self.enqueue_job(job_payload)

    def enqueue_job(self, job_payload: dict) -> str:
        import json
        run_id = str(uuid4())
        run_payload = {"run_id": run_id, "job_id": job_payload["job_id"], "dataset_id": job_payload["dataset_id"],
                       "provider": job_payload.get("provider"), "request_id": job_payload.get("request_id"),
                       "symbol": job_payload.get("symbol"), "recipe_id": job_payload.get("recipe_id"),
                       "recipe_version": job_payload.get("recipe_version"),
                       "input_snapshot_id": job_payload.get("input_snapshot_id"),
                       "run_scope": job_payload.get("run_scope", "production"),
                       "run_kind": job_payload.get("run_kind", "ingest"),
                       "execution_plan": job_payload.get("execution_plan"),
                       "status": "queued", "created_at": datetime.now(timezone.utc).isoformat()}
        with sqlite3.connect(self.path) as conn:
            conn.execute("insert into runs values (?, ?)", (run_id, json.dumps(run_payload)))
            conn.execute("insert into jobs(job_id, run_id, status, payload) values (?, ?, ?, ?)", (str(uuid4()), run_id, "queued", json.dumps(job_payload)))
        return run_id

    def claim_next_job(self) -> dict | None:
        import json

        with sqlite3.connect(self.path) as conn:
            conn.execute("begin immediate")
            row = conn.execute("select job_id, run_id, payload, attempts from jobs where status = 'queued' and (available_at is null or available_at <= ?) order by rowid limit 1", (time.time(),)).fetchone()
            if row is None:
                return None
            conn.execute("update jobs set status = 'running' where job_id = ?", (row[0],))
            run_row = conn.execute("select payload from runs where run_id = ?", (row[1],)).fetchone()
            run = json.loads(run_row[0])
            run["status"] = "running"
            run["started_at"] = datetime.now(timezone.utc).isoformat()
            conn.execute("update runs set payload = ? where run_id = ?", (json.dumps(run), row[1]))
            conn.execute("update jobs set attempts = attempts + 1 where job_id = ?", (row[0],))
            return {"job_id": row[0], "run_id": row[1], "payload": json.loads(row[2]), "attempts": row[3] + 1}

    def running_jobs(self) -> list[dict]:
        with sqlite3.connect(self.path) as conn:
            rows = conn.execute("select job_id, run_id, payload, attempts from jobs where status='running'").fetchall()
        return [{"job_id": r[0], "run_id": r[1], "payload": json.loads(r[2]), "attempts": r[3]} for r in rows]

    def complete_job(self, job_id: str) -> None:
        with sqlite3.connect(self.path) as conn:
            conn.execute("update jobs set status = 'completed' where job_id = ?", (job_id,))

    def finish_job(self, job_id: str, run_id: str, receipt: dict) -> None:
        with sqlite3.connect(self.path) as conn:
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
                       "finished_at": datetime.now(timezone.utc).isoformat(), "error": None, "error_type": None,
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
        with sqlite3.connect(self.path) as conn:
            conn.execute("begin immediate")
            row = conn.execute("select attempts from jobs where job_id = ?", (job_id,)).fetchone()
            attempts = row[0] if row else 1
            original = json.loads(conn.execute("select payload from runs where run_id=?", (run_id,)).fetchone()[0])
            if original.get("status") != "running":
                raise ValueError("only a running job can fail")
            status = "failed" if not retryable else ("dead_letter" if attempts >= 3 else "queued")
            available = time.time() + delay_seconds * (2 ** max(0, attempts - 1))
            failure = {"attempt": attempts, "error_type": error_type or "IngestError", "failure_stage": failure_stage, "error": error,
                       "retryable": retryable, "at": datetime.now(timezone.utc).isoformat()}
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
        with sqlite3.connect(self.path) as conn:
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
                       "created_at": datetime.now(timezone.utc).isoformat()}
            conn.execute("insert into runs values (?, ?)", (new_id, json.dumps(payload)))
            conn.execute("insert into jobs(job_id,run_id,status,payload) values (?,?,?,?)", (str(uuid4()), new_id, "queued", job[0]))
        return new_id

    def acknowledge_dead_letter(self, run_id: str) -> dict:
        stamp = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(self.path) as conn:
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
        with sqlite3.connect(self.path) as conn:
            rows = conn.execute(
                "select action,at,related_run_id from dead_letter_audit where run_id=? order by id", (run_id,),
            ).fetchall()
        return [{"action": row[0], "at": row[1], "related_run_id": row[2]} for row in rows]

    def heartbeat(self) -> str:
        stamp = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(self.path) as conn:
            conn.execute("insert into worker_heartbeat(id, heartbeat) values (1, ?) on conflict(id) do update set heartbeat=excluded.heartbeat", (stamp,))
        return stamp

    def heartbeat_age_seconds(self) -> float | None:
        with sqlite3.connect(self.path) as conn:
            row = conn.execute("select heartbeat from worker_heartbeat where id=1").fetchone()
        if not row:
            return None
        return max(0.0, (datetime.now(timezone.utc) - datetime.fromisoformat(row[0])).total_seconds())
