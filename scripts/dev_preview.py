#!/usr/bin/env python3
"""Manage a persistent, isolated four-process development preview."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import secrets
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DEFAULT_PRODUCTION_ROOTS = (
    Path("/home/quant/market_lake"),
    Path("/home/quant/market_lake/canonical"),
    Path("/home/quant/market_lake/evidence/data-center"),
)
ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
BUSINESS_ENV_PREFIXES = ("DATACENTER_", "DUKASCOPY_", "FRED_", "BINANCE_", "YFINANCE_")
PARENT_ENV_ALLOWLIST = {"DATACENTER_PYTHON", "DATACENTER_PREVIEW_BASE"}


class PreviewError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_git(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=REPO, text=True).strip()


def checkout_identity() -> dict:
    status = subprocess.check_output(
        ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"], cwd=REPO
    )
    fingerprint = hashlib.sha256(status)
    fingerprint.update(
        subprocess.check_output(["git", "diff", "--binary", "HEAD"], cwd=REPO)
    )
    for record in status.split(b"\0"):
        if record.startswith(b"?? "):
            path = REPO / os.fsdecode(record[3:])
            if path.is_file():
                fingerprint.update(record[3:])
                fingerprint.update(path.read_bytes())
    return {
        "checkout": str(REPO.resolve()),
        "branch": run_git("branch", "--show-current") or "detached",
        "commit": run_git("rev-parse", "HEAD"),
        "dirty": bool(status),
        "worktree_fingerprint": fingerprint.hexdigest(),
    }


def validate_id(preview_id: str) -> None:
    if not ID_PATTERN.fullmatch(preview_id):
        raise PreviewError("preview id must match [a-z0-9][a-z0-9-]{0,31}")


def overlaps(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def reject_symlink_components(path: Path) -> None:
    current = path
    while True:
        if current.exists() and current.is_symlink():
            raise PreviewError(f"preview path contains a symbolic link: {current}")
        if current.parent == current:
            return
        current = current.parent


def preview_root(preview_id: str, base: str | None) -> Path:
    configured = Path(base or os.getenv("DATACENTER_PREVIEW_BASE") or REPO / ".preview")
    reject_symlink_components(configured)
    root = (configured / preview_id).absolute()
    reject_symlink_components(root)
    resolved = root.resolve(strict=False)
    repo = REPO.resolve()
    default_root = repo / ".preview" / preview_id
    if resolved == repo or resolved in repo.parents or (repo in resolved.parents and resolved != default_root):
        raise PreviewError(
            f"preview root is too broad or overlaps the checkout: {resolved}"
        )
    for production in DEFAULT_PRODUCTION_ROOTS:
        if overlaps(resolved, production.resolve(strict=False)):
            raise PreviewError(
                f"preview root overlaps a production data root: {production}"
            )
    return resolved


def validate_parent_environment() -> None:
    unexpected = sorted(
        name
        for name in os.environ
        if name not in PARENT_ENV_ALLOWLIST and name.startswith(BUSINESS_ENV_PREFIXES)
    )
    if unexpected:
        raise PreviewError(
            "unexpected business environment is set: " + ", ".join(unexpected)
        )


def python_executable(argument: str | None) -> Path:
    value = argument or os.getenv("DATACENTER_PYTHON") or str(REPO / ".venv/bin/python")
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = REPO / candidate
    if not candidate.exists() or not os.access(candidate, os.X_OK):
        raise PreviewError(
            f"Python environment is unavailable: {candidate}. Create .venv with the locked constraint file."
        )
    probe = subprocess.run(
        [str(candidate), "-c", "import fastapi, uvicorn, data_center"],
        cwd=REPO,
        env={**tool_environment(), "PYTHONPATH": str(REPO / "backend/src")},
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode:
        raise PreviewError(f"Python dependencies are incomplete in {candidate}")
    return candidate.resolve()


def vite_executable() -> Path:
    candidate = REPO / "webui/node_modules/.bin/vite"
    if not candidate.exists() or not os.access(candidate, os.X_OK):
        raise PreviewError(
            "Web dependencies are unavailable. Run: npm --prefix webui ci --include=dev"
        )
    node = subprocess.run(
        ["node", "-p", "process.versions.node.split('.')[0]"],
        capture_output=True,
        text=True,
        env=tool_environment(),
        check=False,
    )
    if node.returncode or node.stdout.strip() != "22":
        raise PreviewError("Node 22 is required for the preview")
    return candidate


def tool_environment() -> dict[str, str]:
    keep = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "LD_LIBRARY_PATH")
    return {name: os.environ[name] for name in keep if name in os.environ}


def port_available(port: int) -> bool:
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def allocate_ports(preview_id: str) -> dict[str, int]:
    digest = int(
        hashlib.sha256(f"{REPO.resolve()}:{preview_id}".encode()).hexdigest()[:8], 16
    )
    first = 20000 + (digest % 3500) * 2
    for offset in range(0, 7000, 2):
        api = 20000 + ((first - 20000 + offset) % 7000)
        ui = api + 1
        if port_available(api) and port_available(ui):
            return {"api": api, "ui": ui}
    raise PreviewError("no free loopback port pair is available")


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    temporary.chmod(0o600)
    temporary.replace(path)


def read_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PreviewError(f"preview metadata is unreadable: {path}") from exc


def proc_start_ticks(pid: int) -> int | None:
    try:
        suffix = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return int(suffix[19])
    except (OSError, ValueError, IndexError):
        return None


def proc_environment(pid: int) -> dict[str, str]:
    try:
        values = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
    except OSError:
        return {}
    result = {}
    for value in values:
        if b"=" in value:
            name, content = value.split(b"=", 1)
            result[name.decode(errors="replace")] = content.decode(errors="replace")
    return result


def process_matches(record: dict, token: str) -> bool:
    pid = int(record["pid"])
    try:
        cwd = Path(f"/proc/{pid}/cwd").resolve()
    except OSError:
        return False
    return (
        proc_start_ticks(pid) == record.get("start_ticks")
        and cwd == REPO.resolve()
        and secrets.compare_digest(
            proc_environment(pid).get("MDC_PREVIEW_TOKEN", ""), token
        )
    )


def matching_token_pids(token: str) -> list[int]:
    matches = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        try:
            cwd = (entry / "cwd").resolve()
        except OSError:
            continue
        if cwd == REPO.resolve() and secrets.compare_digest(
            proc_environment(pid).get("MDC_PREVIEW_TOKEN", ""), token
        ):
            matches.append(pid)
    return matches


def terminate(metadata: dict) -> list[str]:
    token = metadata.get("token", "")
    records = metadata.get("processes", {})
    verified = {
        int(record["pid"])
        for record in records.values()
        if process_matches(record, token)
    }
    token_pids = set(matching_token_pids(token)) if token else set()
    targets = verified | token_pids
    refused = [
        name
        for name, record in records.items()
        if Path(f"/proc/{record.get('pid')}").exists()
        and int(record["pid"]) not in verified
    ]
    if refused:
        raise PreviewError(
            "refusing to stop processes whose identity changed: " + ", ".join(refused)
        )
    for pid in sorted(targets, reverse=True):
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline and any(
        Path(f"/proc/{pid}").exists() for pid in targets
    ):
        time.sleep(0.1)
    for pid in targets:
        if Path(f"/proc/{pid}").exists() and secrets.compare_digest(
            proc_environment(pid).get("MDC_PREVIEW_TOKEN", ""), token
        ):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    return sorted(records)


def process_environment(root: Path, metadata: dict) -> dict[str, str]:
    identity = metadata["identity"]
    ports = metadata["ports"]
    cookie_digest = hashlib.sha256(
        f"{REPO.resolve()}:{metadata['id']}".encode()
    ).hexdigest()[:16]
    data = root / "data"
    env = {
        **tool_environment(),
        "PYTHONUNBUFFERED": "1",
        "PYTHONPATH": str(REPO / "backend/src"),
        "MDC_PREVIEW_TOKEN": metadata["token"],
        "DATACENTER_HOST": "127.0.0.1",
        "DATACENTER_PORT": str(ports["api"]),
        "DATACENTER_APP_NAME": f"Market Data Center Preview {metadata['id']}",
        "DATACENTER_CANONICAL_ROOT": str(data / "canonical"),
        "DATACENTER_LEDGER_PATH": str(data / "ledger/data_center.sqlite"),
        "DATACENTER_EVIDENCE_ROOT": str(data / "evidence"),
        "DATACENTER_BACKUP_ROOT": str(data / "backup"),
        "DATACENTER_AUTH_STATE_PATH": str(data / "auth/legacy-state.json"),
        "DATACENTER_AUTH_COOKIE_SECURE": "false",
        "DATACENTER_AUTH_COOKIE_NAME": f"mdc_preview_{cookie_digest}",
        "DATACENTER_ALERTS_ENABLED": "false",
        "DATACENTER_PROVIDER_ALLOWLIST": "fixture",
        "DATACENTER_ENVIRONMENT_NAME": f"preview:{metadata['id']}",
        "DATACENTER_DATA_MODE": "fixture",
        "DATACENTER_SOURCE_COMMIT": identity["commit"],
        "DATACENTER_SOURCE_DIRTY": str(identity["dirty"]).lower(),
        "DATACENTER_SCHEDULER_DISPATCH_ENABLED": "true",
        "DATACENTER_SCHEDULER_INSTANCE_ID": f"preview-{metadata['id']}",
        "VITE_API_PROXY_TARGET": f"http://127.0.0.1:{ports['api']}",
        "VITE_PREVIEW_ID": metadata["id"],
        "VITE_PREVIEW_DATA_MODE": "fixture",
        "VITE_PREVIEW_COMMIT": identity["commit"],
        "VITE_PREVIEW_DIRTY": str(identity["dirty"]).lower(),
        "NODE_ENV": "development",
    }
    return env


def spawn_component(
    name: str, command: list[str], root: Path, env: dict[str, str]
) -> tuple[subprocess.Popen, dict]:
    log_path = root / "logs" / f"{name}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = log_path.open("ab", buffering=0)
    try:
        process = subprocess.Popen(
            command,
            cwd=REPO,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    finally:
        log.close()
    ticks = None
    for _ in range(20):
        ticks = proc_start_ticks(process.pid)
        if ticks is not None:
            break
        time.sleep(0.01)
    return process, {
        "pid": process.pid,
        "start_ticks": ticks,
        "log": str(log_path),
        "command": command,
    }


def http_json(url: str) -> dict | None:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=2) as response:
            return json.loads(response.read())
    except (OSError, ValueError, urllib.error.URLError):
        return None


def http_ok(url: str) -> bool:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=2) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError):
        return False


def wait_until(
    predicate, message: str, processes: list[subprocess.Popen], timeout: float = 45
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        failed = [process for process in processes if process.poll() is not None]
        if failed:
            raise PreviewError(f"a preview process exited while waiting for {message}")
        if predicate():
            return
        time.sleep(0.2)
    raise PreviewError(f"timed out waiting for {message}")


def status_payload(root: Path, metadata: dict) -> dict:
    actual = checkout_identity()
    processes = {
        name: {**record, "running": process_matches(record, metadata.get("token", ""))}
        for name, record in metadata.get("processes", {}).items()
    }
    identity_ok = all(
        actual.get(key) == metadata.get("identity", {}).get(key)
        for key in ("checkout", "commit", "dirty", "worktree_fingerprint")
    )
    all_running = len(processes) == 4 and all(
        item["running"] for item in processes.values()
    )
    scheduler_view = (
        http_json(
            f"http://127.0.0.1:{metadata['ports']['api']}/api/v1/operations/scheduler"
        )
        if all_running
        else None
    )
    scheduler_data = (scheduler_view or {}).get("data", {})
    scheduler_state = scheduler_data.get("scheduler", {})
    process_enabled = bool(
        processes.get("scheduler", {}).get("running", False)
    ) and bool(scheduler_state.get("instance_dispatch_enabled", False))
    ledger_enabled = bool(scheduler_state.get("dispatch_enabled", False))
    effective_dispatch = process_enabled and ledger_enabled
    return {
        "id": metadata["id"],
        "state": "running"
        if all_running and identity_ok
        else "identity_mismatch"
        if all_running
        else "stopped",
        "identity_ok": identity_ok,
        "recorded_identity": metadata.get("identity"),
        "actual_identity": actual,
        "mode": "fixture",
        "scheduler": {
            "process_enabled": process_enabled,
            "ledger_enabled": ledger_enabled,
            "effective_dispatch": effective_dispatch,
        },
        "ui_url": f"http://127.0.0.1:{metadata['ports']['ui']}",
        "api_docs_url": f"http://127.0.0.1:{metadata['ports']['api']}/docs",
        "api_url": f"http://127.0.0.1:{metadata['ports']['api']}/api/v1",
        "data_root": str(root / "data"),
        "logs": str(root / "logs"),
        "started_at": metadata.get("started_at"),
        "stopped_at": metadata.get("stopped_at"),
        "observed_at": utc_now(),
        "processes": processes,
    }


def print_status(payload: dict, *, as_json: bool = False) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return
    print(f"Preview: {payload['id']} ({payload['state']}, {payload['mode']})")
    print(f"UI: {payload['ui_url']}")
    print(f"API docs: {payload['api_docs_url']}")
    identity = payload["actual_identity"]
    print(
        f"Identity: {identity['branch']} / {identity['commit']} / dirty={str(identity['dirty']).lower()}"
    )
    dispatch = payload["scheduler"]
    print(
        "Scheduler: process="
        + ("on" if dispatch["process_enabled"] else "off")
        + " ledger="
        + ("on" if dispatch["ledger_enabled"] else "off")
        + " effective_dispatch="
        + ("on" if dispatch["effective_dispatch"] else "off")
    )
    print(f"Data: {payload['data_root']} (preserved by stop)")
    print(f"Logs: {payload['logs']}")
    for name, record in payload["processes"].items():
        print(f"  {name}: pid={record['pid']} running={str(record['running']).lower()}")


def start(args, root: Path, metadata_path: Path) -> int:
    validate_parent_environment()
    python = python_executable(args.python)
    vite = vite_executable()
    old = read_json(metadata_path)
    current = checkout_identity()
    if old:
        old_status = status_payload(root, old)
        if old_status["state"] == "running":
            print_status(old_status, as_json=args.json)
            return 0
        terminate(old)
        changed = any(
            current.get(key) != old.get("identity", {}).get(key)
            for key in ("checkout", "commit", "dirty", "worktree_fingerprint")
        )
        if changed and not args.update:
            raise PreviewError(
                "preview identity changed while stopped; inspect status, then restart explicitly with --update"
            )
        ports = old["ports"]
        if not port_available(ports["api"]) or not port_available(ports["ui"]):
            raise PreviewError(
                "the preview's recorded port is occupied; no process was started"
            )
        created_at = old.get("created_at", utc_now())
    else:
        ports = allocate_ports(args.id)
        created_at = utc_now()
    metadata = {
        "schema_version": "dev-preview.v1",
        "id": args.id,
        "root": str(root),
        "identity": current,
        "ports": ports,
        "token": secrets.token_urlsafe(32),
        "created_at": created_at,
        "started_at": utc_now(),
        "stopped_at": None,
        "processes": {},
    }
    for path in (
        root / "data/canonical",
        root / "data/ledger",
        root / "data/evidence",
        root / "data/backup",
        root / "data/auth",
        root / "logs",
    ):
        path.mkdir(parents=True, exist_ok=True)
    reject_symlink_components(root)
    if root.resolve() != Path(metadata["root"]):
        raise PreviewError("preview root changed while it was being prepared")
    env = process_environment(root, metadata)
    commands = {
        "api": [str(python), "-m", "data_center.api"],
        "worker": [str(python), "-m", "data_center.worker_main"],
        "scheduler": [
            str(python),
            "-m",
            "data_center.scheduler_main",
            "--dispatch",
            "--instance-id",
            f"preview-{args.id}",
        ],
        "vite": [
            str(vite),
            "webui",
            "--host",
            "127.0.0.1",
            "--port",
            str(ports["ui"]),
            "--strictPort",
        ],
    }
    processes: list[subprocess.Popen] = []
    try:
        for name in ("api", "worker", "scheduler", "vite"):
            process, record = spawn_component(name, commands[name], root, env)
            processes.append(process)
            metadata["processes"][name] = record
        write_json(metadata_path, metadata)
        api_base = f"http://127.0.0.1:{ports['api']}"
        wait_until(
            lambda: (
                (http_json(api_base + "/api/v1/health/ready") or {})
                .get("data", {})
                .get("status")
                == "ready"
            ),
            "API, worker and storage readiness",
            processes,
        )
        wait_until(
            lambda: http_json(api_base + "/api/v1/operations/scheduler") is not None,
            "scheduler status",
            processes,
        )
        wait_until(
            lambda: http_ok(f"http://127.0.0.1:{ports['ui']}"), "Vite UI", processes
        )
    except BaseException:
        try:
            terminate(metadata)
        finally:
            metadata["stopped_at"] = utc_now()
            write_json(metadata_path, metadata)
        raise
    payload = status_payload(root, metadata)
    print_status(payload, as_json=args.json)
    return 0


def command_status(args, root: Path, metadata_path: Path) -> int:
    metadata = read_json(metadata_path)
    if not metadata:
        raise PreviewError(f"preview does not exist: {args.id}")
    print_status(status_payload(root, metadata), as_json=args.json)
    return 0


def stop(args, root: Path, metadata_path: Path) -> int:
    metadata = read_json(metadata_path)
    if not metadata:
        raise PreviewError(f"preview does not exist: {args.id}")
    terminate(metadata)
    metadata["stopped_at"] = utc_now()
    write_json(metadata_path, metadata)
    print_status(status_payload(root, metadata), as_json=args.json)
    return 0


def parse_args(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("start", "status", "stop"))
    parser.add_argument("--id", required=True)
    parser.add_argument("--base", help="controlled parent directory for preview state")
    parser.add_argument(
        "--python", help="Python executable with locked backend dependencies"
    )
    parser.add_argument(
        "--update",
        action="store_true",
        help="accept a visible checkout identity change on restart",
    )
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    validate_id(args.id)
    root = preview_root(args.id, args.base)
    root.parent.mkdir(parents=True, exist_ok=True)
    root.parent.chmod(0o700)
    lock_path = root.parent / ".dev-preview.lock"
    with lock_path.open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        metadata_path = root / "preview.json"
        return {"start": start, "status": command_status, "stop": stop}[args.command](
            args, root, metadata_path
        )


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PreviewError as exc:
        print(f"dev-preview: {exc}", file=sys.stderr)
        raise SystemExit(2)
