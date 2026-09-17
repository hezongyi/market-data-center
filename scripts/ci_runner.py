"""Run explicit checks, preserving per-check timing and exact source evidence."""
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from ci_receipt import environment_identity, reusable, software_version
from code_identity import checkout_identity

ROOT = Path(__file__).resolve().parents[1]


def checks(scope: str, selectors: list[str]) -> list[tuple[str, list[str]]]:
    py = sys.executable
    docs = [("docs-consistency", [py, "scripts/docs_consistency_check.py"]),
            ("docs-links", [py, "scripts/docs_link_check.py"]),
            ("secrets", [py, "scripts/secret_scan.py"]),
            ("diff-format", ["git", "diff", "--check"])]
    # Preserve Ruff's backend working directory / first-party import inference.
    lint = [("ruff-backend", ["bash", "-c", 'cd backend && exec "$@"', "--", py, "-m", "ruff", "check", "src", "tests"]),
            ("ruff-scripts", [py, "-m", "ruff", "check", "scripts"])]
    backend = lint + [
        ("pip-check", [py, "-m", "pip", "check"]),
        ("compatibility", [py, "scripts/compatibility_check.py"]),
        ("dependency-lock", [py, "scripts/dependency_lock_check.py"]),
        ("production-env", [py, "scripts/production_env_check.py"]),
        ("pytest", [py, "-m", "pytest", "-q", "backend/tests"]),
        ("secrets", [py, "scripts/secret_scan.py"]),
        ("operations", [py, "scripts/operations_acceptance.py"]),
        ("snapshot-benchmark", [py, "scripts/operational_snapshot_benchmark.py"]),
    ]
    build = [("web-build", ["npm", "--prefix", "webui", "run", "build"])]
    web = build + [
        ("preview-isolation", [py, "scripts/dev_preview_acceptance.py", "--python", py]),
        ("browser", ["bash", "scripts/web-preflight.sh", "npm", "--prefix", "webui", "run", "test:e2e"]),
        ("service", [py, "scripts/service_acceptance.py"]),
    ]
    if scope == "backend-fast":
        if not selectors or any(not value.startswith("backend/tests/") or not (ROOT / value.split("::")[0]).is_file() for value in selectors):
            raise ValueError("backend-fast requires explicit backend/tests/test_*.py[::test_name] selectors")
        return lint + [("pytest-selected", [py, "-m", "pytest", "-q", *selectors])]
    if scope == "web-fast":
        if any(not route.startswith("/") or route.startswith("//") or ":" in route for route in selectors):
            raise ValueError("web-fast takes local routes such as /datasets /tasks")
        return build + [("browser-smoke", ["bash", "scripts/web-preflight.sh", "node", "scripts/browser_smoke.cjs", *(selectors or ["/datasets", "/tasks"])])]
    if selectors:
        raise ValueError("selectors are supported only by backend-fast and web-fast")
    return {"docs": docs, "backend": backend, "web": web, "all": docs + backend + web}[scope]


def run_checks(selected: list[tuple[str, list[str]]], repo: Path, env: dict) -> tuple[int, list[dict]]:
    results = []
    status = 0
    for name, command in selected:
        print(f"check: {name}", flush=True)
        start = time.monotonic()
        try:
            status = subprocess.run(command, cwd=repo, env=env, check=False).returncode
        except OSError:
            status = 127
        except KeyboardInterrupt:
            status = 130
        results.append({"name": name, "command": shlex.join(command), "exit_code": status,
                        "result": "pass" if status == 0 else "failed",
                        "duration_seconds": round(time.monotonic() - start, 3)})
        if status:
            break
    return status, results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scope", nargs="?", default="all", choices=("docs", "backend-fast", "web-fast", "backend", "web", "all"))
    parser.add_argument("selectors", nargs="*")
    parser.add_argument("--reuse-from", type=Path, help="explicitly reuse matching local evidence; disabled in hosted CI")
    args = parser.parse_args()
    try:
        selected = checks(args.scope, args.selectors)
    except ValueError as exc:
        parser.error(str(exc))
    if args.reuse_from and os.getenv("GITHUB_ACTIONS") == "true":
        parser.error("hosted CI always executes checks")
    started = datetime.now(timezone.utc)
    source = checkout_identity(ROOT)
    environment = environment_identity(ROOT)
    request = {"scope": args.scope, "selectors": args.selectors,
               "checks": [{"name": name, "command": shlex.join(command)} for name, command in selected]}
    result = None
    if args.reuse_from:
        try:
            candidate = json.loads(args.reuse_from.read_text())
            if reusable(candidate, source, environment, request):
                result = candidate
        except (OSError, ValueError):
            pass
        print("evidence: reused" if result else "evidence: not reusable; executing checks", flush=True)
    env = {**os.environ, "PYTHONPATH": str(ROOT / "backend/src"), "DATACENTER_PYTHON": sys.executable}
    if result:
        status, stages = 0, result["details"]["checks"]
    else:
        status, stages = run_checks(selected, ROOT, env)
    finished_source = checkout_identity(ROOT)
    finished_environment = environment_identity(ROOT)
    stable = source == finished_source and environment == finished_environment
    if not stable:
        status = status or 1
        print("evidence: source/environment changed during checks; result is not reusable", flush=True)
    completed = datetime.now(timezone.utc)
    name = f"{args.scope}-{started.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}.json"
    archive = ROOT / "acceptance-receipts/ci" / name
    payload = {
        "receipt_version": "operational-receipt.v1", "action": os.getenv("DATACENTER_CI_ACTION", "ci"),
        "commit": source["commit"], "source": source, "source_after": finished_source,
        "environment": environment, "command": shlex.join(["bash", "scripts/ci.sh", args.scope, *args.selectors]),
        "started_at": started.isoformat(), "completed_at": completed.isoformat(),
        "software_version": software_version(), "result": "pass" if status == 0 else "failed",
        "archive_path": str(archive),
        "failure_stage": None if status == 0 else "identity-changed" if not stable else stages[-1]["name"],
        "error_category": None if status == 0 else "CheckFailed",
        "details": {"scope": args.scope, "request": request, "checks": stages,
                    "identity_stable": stable, "duration_seconds": (completed - started).total_seconds(),
                    "reused_from": result.get("archive_path", str(args.reuse_from.resolve())) if result else None},
    }
    archive.parent.mkdir(parents=True, exist_ok=True)
    archive.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    # Preserve an immutable run copy even when CI requests a stable artifact name.
    if os.getenv("DATACENTER_CI_RECEIPT"):
        alias = Path(os.environ["DATACENTER_CI_RECEIPT"])
        alias.parent.mkdir(parents=True, exist_ok=True)
        alias.write_text(archive.read_text())
    print(f"receipt: {archive}", flush=True)
    return status if status >= 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
