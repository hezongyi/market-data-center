"""Validate the repository's current-state/documentation governance invariants."""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ALLOWED = {"draft", "approved", "in_progress", "implemented", "accepted", "superseded", "blocked"}


def main() -> int:
    errors: list[str] = []
    current = ROOT / "docs" / "current-state.md"
    index = ROOT / "docs" / "README.md"
    if not current.exists():
        errors.append("docs/current-state.md is missing")
    if not index.exists():
        errors.append("docs/README.md is missing")

    manifest = Path("/home/quant/market-data-center/releases/current/deployment.json")
    deployment_id = None
    if manifest.exists():
        try:
            deployment_id = json.loads(manifest.read_text())["deployment_id"]
        except (OSError, KeyError, TypeError, json.JSONDecodeError):
            errors.append("active deployment manifest is unreadable")
    if deployment_id and deployment_id not in current.read_text():
        errors.append(f"current-state.md does not reference active deployment {deployment_id}")

    for path in sorted((ROOT / "docs" / "specs").glob("*.md")):
        text = path.read_text()
        match = re.search(r"^状态：([^\n]+)", text, re.MULTILINE)
        if not match:
            errors.append(f"{path.name}: missing status line")
            continue
        status_text = match.group(1).strip().lower()
        aliases = {"complete": "accepted", "completed": "accepted"}
        token = status_text.split("；", 1)[0].split(";", 1)[0].strip()
        status = next((value for key, value in aliases.items() if token.startswith(key)), None)
        if status is None:
            status = next((candidate for candidate in ALLOWED if token.startswith(candidate)), None)
        if status is None and token.startswith("s1 implemented"):
            status = "accepted"
        if status is None:
            errors.append(f"{path.name}: unsupported status {match.group(1)!r}")
        if status == "superseded" and "实施以三份拆分 spec 为准" not in text:
            errors.append(f"{path.name}: superseded spec must name its successor specs")

    if errors:
        for error in errors:
            print(f"docs-consistency: {error}")
        return 1
    print(f"docs-consistency: pass ({deployment_id or 'no active deployment manifest'})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
