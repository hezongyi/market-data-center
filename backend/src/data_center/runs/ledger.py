import sqlite3
from pathlib import Path


class RunLedger:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path) as conn:
            conn.execute("create table if not exists runs (run_id text primary key, payload text not null)")

    def put(self, run_id: str, payload: dict) -> None:
        import json
        with sqlite3.connect(self.path) as conn:
            conn.execute("insert or replace into runs values (?, ?)", (run_id, json.dumps(payload)))

    def list(self) -> list[dict]:
        import json
        with sqlite3.connect(self.path) as conn:
            rows = conn.execute("select payload from runs order by rowid desc").fetchall()
        return [json.loads(row[0]) for row in rows]

    def get(self, run_id: str) -> dict:
        import json
        with sqlite3.connect(self.path) as conn:
            row = conn.execute("select payload from runs where run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(run_id)
        return json.loads(row[0])

