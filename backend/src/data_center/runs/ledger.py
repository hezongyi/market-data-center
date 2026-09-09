import sqlite3
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import List
from uuid import uuid4


class RunLedger:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path) as conn:
            conn.execute("create table if not exists runs (run_id text primary key, payload text not null)")
            conn.execute("create table if not exists jobs (job_id text primary key, run_id text not null, status text not null, payload text not null, attempts integer not null default 0)")
            conn.execute("create table if not exists worker_heartbeat (id integer primary key check (id=1), heartbeat text not null)")
            columns = {row[1] for row in conn.execute("pragma table_info(jobs)")}
            if "attempts" not in columns:
                conn.execute("alter table jobs add column attempts integer not null default 0")

    def put(self, run_id: str, payload: dict) -> None:
        with sqlite3.connect(self.path) as conn:
            conn.execute("insert or replace into runs values (?, ?)", (run_id, json.dumps(payload)))

    def update(self, run_id: str, **fields) -> None:
        payload = self.get(run_id)
        payload.update(fields)
        self.put(run_id, payload)

    def list(self) -> list[dict]:
        with sqlite3.connect(self.path) as conn:
            rows = conn.execute("select payload from runs order by rowid desc").fetchall()
        return [json.loads(row[0]) for row in rows]

    def get(self, run_id: str) -> dict:
        with sqlite3.connect(self.path) as conn:
            row = conn.execute("select payload from runs where run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(run_id)
        return json.loads(row[0])

    def findings(self) -> List[dict]:
        with sqlite3.connect(self.path) as conn:
            conn.execute("create table if not exists quality_findings (id integer primary key, payload text not null)")
            rows = conn.execute("select payload from quality_findings order by id desc").fetchall()
        return [json.loads(row[0]) for row in rows]

    def add_findings(self, payloads: List[dict]) -> None:
        with sqlite3.connect(self.path) as conn:
            conn.execute("create table if not exists quality_findings (id integer primary key, payload text not null)")
            conn.executemany("insert into quality_findings(payload) values (?)", [(json.dumps(item),) for item in payloads])

    def enqueue_provider_bars(self, job_payload: dict) -> str:
        import json

        run_id = str(uuid4())
        run_payload = {
            "run_id": run_id,
            "job_id": job_payload["job_id"],
            "dataset_id": job_payload["dataset_id"],
            "status": "queued",
        }
        with sqlite3.connect(self.path) as conn:
            conn.execute("insert into runs values (?, ?)", (run_id, json.dumps(run_payload)))
            conn.execute("insert into jobs(job_id, run_id, status, payload) values (?, ?, ?, ?)", (str(uuid4()), run_id, "queued", json.dumps(job_payload)))
        return run_id

    def enqueue_job(self, job_payload: dict) -> str:
        import json
        run_id = str(uuid4())
        run_payload = {"run_id": run_id, "job_id": job_payload["job_id"], "dataset_id": job_payload["dataset_id"], "status": "queued"}
        with sqlite3.connect(self.path) as conn:
            conn.execute("insert into runs values (?, ?)", (run_id, json.dumps(run_payload)))
            conn.execute("insert into jobs(job_id, run_id, status, payload) values (?, ?, ?, ?)", (str(uuid4()), run_id, "queued", json.dumps(job_payload)))
        return run_id

    def claim_next_job(self) -> dict | None:
        import json

        with sqlite3.connect(self.path) as conn:
            conn.execute("begin immediate")
            row = conn.execute("select job_id, run_id, payload from jobs where status = 'queued' order by rowid limit 1").fetchone()
            if row is None:
                return None
            conn.execute("update jobs set status = 'running' where job_id = ?", (row[0],))
            run_row = conn.execute("select payload from runs where run_id = ?", (row[1],)).fetchone()
            run = json.loads(run_row[0])
            run["status"] = "running"
            run["started_at"] = datetime.now(timezone.utc).isoformat()
            conn.execute("update runs set payload = ? where run_id = ?", (json.dumps(run), row[1]))
            conn.execute("update jobs set attempts = attempts + 1 where job_id = ?", (row[0],))
            return {"job_id": row[0], "run_id": row[1], "payload": json.loads(row[2])}

    def recover_running_jobs(self) -> int:
        """Requeue jobs left running by a worker process that stopped."""
        with sqlite3.connect(self.path) as conn:
            rows = conn.execute("select run_id from jobs where status = 'running'").fetchall()
            conn.execute("update jobs set status = 'queued' where status = 'running'")
            for (run_id,) in rows:
                run = json.loads(conn.execute("select payload from runs where run_id = ?", (run_id,)).fetchone()[0])
                run.update(status="queued", recovered_at=datetime.now(timezone.utc).isoformat())
                conn.execute("update runs set payload = ? where run_id = ?", (json.dumps(run), run_id))
        return len(rows)

    def complete_job(self, job_id: str) -> None:
        with sqlite3.connect(self.path) as conn:
            conn.execute("update jobs set status = 'completed' where job_id = ?", (job_id,))

    def fail_job(self, job_id: str, run_id: str, error: str) -> None:
        with sqlite3.connect(self.path) as conn:
            row = conn.execute("select attempts from jobs where job_id = ?", (job_id,)).fetchone()
            attempts = row[0] if row else 1
            status = "dead_letter" if attempts >= 3 else "queued"
            conn.execute("update jobs set status = ? where job_id = ?", (status, job_id))
        self.update(run_id, status=status, error=error, retry_count=attempts, retryable=status != "dead_letter")

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
