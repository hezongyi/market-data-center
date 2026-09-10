"""Start isolated real API/worker processes and verify durable restart and retry."""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import requests


def main():
    repo = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="mdc-service-acceptance-") as directory:
        root = Path(directory)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        env = {**os.environ, "DATACENTER_CANONICAL_ROOT": str(root / "lake"),
               "DATACENTER_LEDGER_PATH": str(root / "ledger.sqlite"), "DATACENTER_EVIDENCE_ROOT": str(root / "evidence"), "DATACENTER_PORT": str(port),
               "DATACENTER_WEBUI_DIST": str(repo / "webui/dist"), "DATACENTER_API_KEY": "acceptance"}
        api = subprocess.Popen([sys.executable, "-m", "data_center.api"], env=env, stdout=subprocess.DEVNULL)
        worker = None
        session = requests.Session()
        session.trust_env = False
        session.headers["X-API-Key"] = "acceptance"

        def call(method, path, **kwargs):
            response = session.request(method, f"http://127.0.0.1:{port}/api/v1" + path, timeout=5, **kwargs)
            response.raise_for_status()
            return response.json()["data"]

        def wait(predicate):
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                try:
                    if predicate():
                        return
                except requests.RequestException:
                    pass
                time.sleep(0.1)
            raise AssertionError("acceptance deadline exceeded")

        try:
            wait(lambda: call("GET", "/health/live")["status"] == "ok")
            job = {"job_id": "restart-acceptance", "symbol": "TEST", "provider": "fixture",
                   "start": "2026-01-01T00:00:00Z", "end": "2026-01-02T00:00:00Z"}
            run = call("POST", "/ingest/runs", json=job)
            api.terminate()
            api.wait(timeout=5)
            api = subprocess.Popen([sys.executable, "-m", "data_center.api"], env=env, stdout=subprocess.DEVNULL)
            wait(lambda: call("GET", "/runs/" + run["run_id"])["status"] == "queued")
            worker = subprocess.Popen([sys.executable, "-m", "data_center.worker_main"], env=env, stdout=subprocess.DEVNULL)
            wait(lambda: call("GET", "/runs/" + run["run_id"])["status"] == "pass")
            assert len(call("GET", "/bars", params={"provider": "fixture", "symbol": "TEST"})) == 2
            failed = call("POST", "/ingest/runs", json={**job, "provider": "unknown"})
            wait(lambda: call("GET", "/runs/" + failed["run_id"])["status"] == "failed")
            old = call("GET", "/runs/" + failed["run_id"])
            retried = call("POST", "/runs/" + failed["run_id"] + "/retry")
            assert retried["retry_of"] == failed["run_id"]
            assert call("GET", "/runs/" + failed["run_id"]) == old
            wait(lambda: call("GET", "/runs/" + retried["run_id"])["status"] == "failed")
            subprocess.run(["bash", str(repo / "scripts/smoke.sh")], check=True,
                           env={**env, "DATACENTER_SMOKE_URL": f"http://127.0.0.1:{port}"})
            print(json.dumps({"status": "pass", "checks": ["api_restart_preserves_queue", "process_worker",
                                                              "parquet_readback", "immutable_retry", "smoke"]}))
        finally:
            for process in (worker, api):
                if process is not None:
                    process.terminate()
                    process.wait(timeout=10)


if __name__ == "__main__":
    main()
