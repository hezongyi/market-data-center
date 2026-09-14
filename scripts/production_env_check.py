"""Guard: production services never take configuration from a repository checkout.

Two modes:

* default (CI): every systemd template under ``deploy/systemd`` must bind its
  ``EnvironmentFile`` to the machine-level configuration directory, so a
  committed unit can never source a checkout.
* ``--installed`` (production host): the units and drop-ins actually installed
  for this user must follow the same rule, which catches host-local drift
  before it silently becomes a production fact.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_PREFIX = "%h/.config/market-data-center/"
UNIT_PREFIX = "market-data-center-"


def environment_file_values(text: str) -> list[str]:
    """Every ``EnvironmentFile=`` value in a unit or drop-in, in file order."""
    values: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        if key.strip() != "EnvironmentFile":
            continue
        values.extend(token.lstrip("-") for token in value.split())
    return values


def unexpected_templates(directory: Path, allowed_prefix: str = TEMPLATE_PREFIX) -> list[str]:
    """Template files whose EnvironmentFile escapes the machine-level config dir."""
    findings: list[str] = []
    for path in sorted(directory.glob("*.service")):
        for value in environment_file_values(path.read_text()):
            if not value.startswith(allowed_prefix):
                findings.append(f"{path.name}: EnvironmentFile={value}")
    return findings


def unexpected_installed(unit_dir: Path, config_dir: Path) -> list[str]:
    """Installed Data Center units and drop-ins that source anything else."""
    expanded = str(config_dir).rstrip(os.sep) + os.sep
    candidates = sorted(unit_dir.glob(f"{UNIT_PREFIX}*.service"))
    candidates += sorted(unit_dir.glob(f"{UNIT_PREFIX}*.timer"))
    candidates += sorted(unit_dir.glob(f"{UNIT_PREFIX}*.service.d/*.conf"))
    findings: list[str] = []
    for path in candidates:
        if not path.is_file():
            continue
        for value in environment_file_values(path.read_text()):
            resolved = value.replace("%h", str(Path.home()), 1)
            if not resolved.startswith(expanded):
                findings.append(f"{path}: EnvironmentFile={value}")
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--installed", action="store_true",
                        help="audit the units installed for this user instead of the repository templates")
    parser.add_argument("--unit-dir", type=Path, default=Path.home() / ".config/systemd/user")
    parser.add_argument("--config-dir", type=Path, default=Path.home() / ".config/market-data-center")
    args = parser.parse_args()

    if args.installed:
        findings = unexpected_installed(args.unit_dir, args.config_dir)
        scope = f"installed units under {args.unit_dir}"
    else:
        findings = unexpected_templates(ROOT / "deploy/systemd")
        scope = "deploy/systemd templates"

    if findings:
        for finding in findings:
            print(f"production-env: {finding}")
        print("production-env: FAIL - production configuration must come from the machine-level "
              f"directory {TEMPLATE_PREFIX} (see docs/operations-runbook.md)")
        return 1
    print(f"production-env: pass ({scope})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
