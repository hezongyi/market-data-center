import os
import tempfile
from pathlib import Path

# Module-level app creation must never touch the production ledger during tests.
_root = tempfile.TemporaryDirectory(prefix="mdc-test-app-")
os.environ["DATACENTER_CANONICAL_ROOT"] = str(Path(_root.name) / "lake")
os.environ["DATACENTER_LEDGER_PATH"] = str(Path(_root.name) / "ledger.sqlite")

os.environ["DATACENTER_EVIDENCE_ROOT"] = str(Path(_root.name) / "evidence")
