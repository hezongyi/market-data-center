"""Audited, reversible maintenance under the same supervisor ownership lock."""
from __future__ import annotations

import argparse
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from data_center.catalog.manifest import immutable_json
from data_center.ingest.worker import LocalWorker
from data_center.runs.ledger import RunLedger
from data_center.settings import Settings


def cleanup_staging(root: Path, ledger: RunLedger, evidence_root: Path, *, apply: bool = False,
                    operation_id: str | None = None) -> dict:
    operation_id = operation_id or uuid4().hex
    if Path(operation_id).name != operation_id or operation_id in {".", ".."}:
        raise ValueError("invalid operation id")
    evidence_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        evidence_root.chmod(0o700)
    except OSError:
        pass
    report_path = evidence_root / "cleanup" / (operation_id + ".json")
    if report_path.exists():
        return json.loads(report_path.read_text())
    with LocalWorker(root, ledger)._ownership() as lock:
        if lock is None:
            raise RuntimeError("worker is busy; retry cleanup later")
        started = datetime.now(timezone.utc).isoformat()
        plan_path = evidence_root / "cleanup" / (operation_id + ".plan.json")
        if plan_path.exists():
            plan = json.loads(plan_path.read_text())
            if plan["apply"] != apply or plan["canonical_root"] != str(root.resolve()):
                raise ValueError("cleanup request differs from recorded plan")
        else:
            items = []
            for run_dir in sorted((root / ".ingest-staging").glob("*")):
                if not run_dir.is_dir() or run_dir.is_symlink():
                    continue
                try:
                    receipt = ledger.get(run_dir.name)
                except KeyError:
                    continue
                if receipt.get("status") not in {"pass", "failed", "dead_letter"}:
                    continue
                if any(p.is_symlink() for p in run_dir.rglob("*")):
                    continue
                files = [p for p in run_dir.rglob("*") if p.is_file()]
                items.append({"run_id": run_dir.name, "status": receipt["status"],
                              "files": len(files), "bytes": sum(p.stat().st_size for p in files)})
            plan = {"operation_id": operation_id, "event": "staging_cleanup", "started_at": started,
                    "canonical_root": str(root.resolve()), "ledger": str(ledger.path.resolve()),
                    "apply": apply, "items": items}
            immutable_json(plan_path, plan)
        for item in plan["items"]:
            if not apply:
                continue
            if ledger.get(item["run_id"])["status"] not in {"pass", "failed", "dead_letter"}:
                raise ValueError("cleanup target is no longer terminal")
            source = root / ".ingest-staging" / item["run_id"]
            archive = evidence_root / "cleanup" / "quarantine" / operation_id / item["run_id"]
            if source.exists():
                archive.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                if archive.exists():
                    raise ValueError("quarantine already exists")
                shutil.move(str(source), str(archive))
        report = {**plan, "finished_at": datetime.now(timezone.utc).isoformat(), "status": "pass",
                  "result": "quarantined" if apply else "audit_only", "directory_count": len(plan["items"]),
                  "file_count": sum(item["files"] for item in plan["items"]),
                  "bytes": sum(item["bytes"] for item in plan["items"])}
        immutable_json(report_path, report)
        return report


def main():
    settings = Settings()
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["cleanup-staging"])
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--operation-id")
    args = parser.parse_args()
    report = cleanup_staging(settings.canonical_root, RunLedger(settings.ledger_path), settings.evidence_root,
                             apply=args.apply, operation_id=args.operation_id)
    print(json.dumps(report), flush=True)


if __name__ == "__main__":
    main()
