"""Generate the immutable JSON receipt attached to a GitHub release."""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--ci-run-id", required=True)
    parser.add_argument("--ci-url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    baseline = args.tag == "v0.1.0"
    payload = {
        "receipt_version": "release-receipt.v1",
        "version": args.tag,
        "tag": args.tag,
        "commit": args.commit,
        "released_at": datetime.now(timezone.utc).isoformat(),
        "result": "pass",
        "hosted_ci": {
            "workflow": "Checks", "run_id": int(args.ci_run_id),
            "required_check": "verify", "conclusion": "success", "url": args.ci_url,
        },
        "runtime_matrix": {
            "python_declared": ">=3.10",
            "hosted_python": ["3.11"] if baseline else ["3.10", "3.11", "3.12"],
            "node": "22",
        },
        "dependency_artifacts": (
            ["backend/pyproject.toml", "webui/package-lock.json"] if baseline else
            ["backend/constraints/py310.txt", "backend/constraints/py311.txt",
             "backend/constraints/py312.txt", "webui/package-lock.json"]
        ),
        "compatibility": "docs/version-compatibility.md",
        "backup_format": "market-data-center-backup.v1" if baseline else "market-data-center-backup.v2",
        "migration_status": {
            "provider_bars_read": "available", "economic_preview": "feature_flag",
            "other_consumers": "not_migrated",
        },
        "rollback": [
            "checkout previous immutable tag", "install its matching dependencies",
            "restart API and worker", "run smoke, query parity, and browser acceptance",
        ],
        "evidence": [
            "docs/release-checklist.md", "docs/version-compatibility.md",
            "docs/operations-runbook.md",
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
