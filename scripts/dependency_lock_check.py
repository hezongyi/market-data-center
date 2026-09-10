"""Verify the interpreter uses the committed transitive lock and warning allowlist."""
from __future__ import annotations

import argparse
import importlib.metadata
from datetime import date, datetime, timezone
from pathlib import Path

from packaging.requirements import Requirement

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib


def lock_path(root: Path) -> Path:
    import sys

    return root / "backend" / "constraints" / f"py{sys.version_info.major}{sys.version_info.minor}.txt"


def verify_lock(root: Path) -> Path:
    path = lock_path(root)
    if not path.is_file():
        raise SystemExit(f"missing dependency lock for this interpreter: {path}")
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        requirement = Requirement(line)
        if requirement.marker and not requirement.marker.evaluate():
            continue
        try:
            installed = importlib.metadata.version(requirement.name)
        except importlib.metadata.PackageNotFoundError as exc:
            raise SystemExit(f"locked dependency is missing: {requirement.name}") from exc
        if installed not in requirement.specifier:
            raise SystemExit(
                f"dependency lock drift: {requirement.name} {installed} not in {requirement.specifier}"
            )
    return path


def verify_warning_allowlist(root: Path) -> int:
    config = tomllib.loads((root / "backend" / "warnings-allowlist.toml").read_text())
    entries = config.get("warning", [])
    today = datetime.now(timezone.utc).date()
    for entry in entries:
        expires = date.fromisoformat(entry["expires"])
        if expires < today:
            raise SystemExit(f"expired warning allowlist entry: {entry['dependency']} ({expires})")
        if not all(entry.get(field) for field in ("dependency", "message", "category", "module", "reason")):
            raise SystemExit("warning allowlist entries must be exact and documented")
    return len(entries)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    path = verify_lock(args.root)
    warning_count = verify_warning_allowlist(args.root)
    print(f"dependency lock verified: {path.name}; warning allowlist entries: {warning_count}")


if __name__ == "__main__":
    main()
