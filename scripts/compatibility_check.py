"""Validate dependency pins and data contract compatibility used by production code."""
from __future__ import annotations

import importlib.metadata

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised on Python 3.10
    import tomli as tomllib
from pathlib import Path

from data_center.catalog.manifest import ECONOMIC_PIT_SCHEMA_VERSION
from data_center.domain.schema import ECONOMIC_PIT_REQUIRED_FIELDS


def _requirement_name(requirement: str) -> str:
    return requirement.split("[", 1)[0].split("=", 1)[0].split(">", 1)[0].split("<", 1)[0].strip()


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    project = tomllib.loads((root / "backend/pyproject.toml").read_text())
    dependencies = project["project"]["dependencies"]
    required = {_requirement_name(item): item for item in dependencies}
    for name, requirement in required.items():
        try:
            installed = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError as exc:
            raise SystemExit(f"missing dependency: {name}") from exc
        if "==" in requirement:
            expected = requirement.split("==", 1)[1]
            if installed != expected:
                raise SystemExit(f"dependency drift: {name} {installed} != {expected}")

    if ECONOMIC_PIT_SCHEMA_VERSION != "economic_observations.v2":
        raise SystemExit("economic PIT schema version drift")
    required_fields = set(ECONOMIC_PIT_REQUIRED_FIELDS)
    if not {"release_ts", "asof_ts", "vintage_start", "vintage_end", "availability_policy",
            "availability_lag_days", "source", "missing_reason"}.issubset(required_fields):
        raise SystemExit("economic PIT required fields are incomplete")
    print("compatibility checks passed")


if __name__ == "__main__":
    main()
