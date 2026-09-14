"""Contract probes for the third-party warning ignored during app creation.

FastAPI builds a ``FieldInfo(alias=<parameter name>)`` for every endpoint parameter and passes it to a
pydantic ``TypeAdapter``; pydantic 2.13 reports that alias as ineffective in that internal usage. The
gate therefore ignores exactly that warning class (``backend/pyproject.toml``). These probes keep the
decision honest instead of hiding it: no *other* warning category may appear while building the app,
concurrent creation must stay stable, and the query-parameter contract the warning is about must keep
working. See issue #95.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi.testclient import TestClient

from data_center.api.app import create_app
from data_center.settings import Settings

REPO_ROOT = Path(__file__).resolve().parents[2]

# The warnings registry suppresses every repeat after the first creation inside a process, so the
# warning-count probe must run in a fresh interpreter to observe what app creation actually emits.
_WARNING_PROBE = r'''
import json, os, pathlib, tempfile, warnings

root = tempfile.TemporaryDirectory(prefix="mdc-warning-probe-")
os.environ.update(
    DATACENTER_CANONICAL_ROOT=f"{root.name}/lake",
    DATACENTER_LEDGER_PATH=f"{root.name}/ledger.sqlite",
    DATACENTER_EVIDENCE_ROOT=f"{root.name}/evidence",
)

from pydantic.warnings import UnsupportedFieldAttributeWarning

counts = {"known": 0}
unknown = {}
original = warnings.warn


def patched(message, category=UserWarning, *args, **kwargs):
    if isinstance(category, type) and issubclass(category, UnsupportedFieldAttributeWarning):
        counts["known"] += 1
    else:
        name = f"{getattr(category, '__module__', '?')}.{getattr(category, '__name__', category)}"
        unknown[name] = unknown.get(name, 0) + 1
    return original(message, category, *args, **kwargs)


warnings.warn = patched

from data_center.api.app import create_app
from data_center.settings import Settings

# Importing data_center.api.app already builds the module-level app; only the explicit call below is
# measured, so the probe reports one app creation rather than import plus creation.
baseline = counts["known"]
unknown.clear()

tmp = pathlib.Path(tempfile.mkdtemp())
create_app(Settings(canonical_root=tmp / "lake", ledger_path=tmp / "ledger.sqlite",
                    evidence_root=tmp / "evidence", auth_cookie_secure=False))
print(json.dumps({"per_create_app": counts["known"] - baseline, "unknown": unknown}))
'''


def _settings(tmp_path: Path, **overrides) -> Settings:
    values = {
        "canonical_root": tmp_path / "lake",
        "ledger_path": tmp_path / "ledger.sqlite",
        "evidence_root": tmp_path / "evidence",
        "auth_cookie_secure": False,
    }
    values.update(overrides)
    return Settings(**values)


def test_app_creation_emits_only_the_known_third_party_warning() -> None:
    environment = {key: value for key, value in os.environ.items() if not key.startswith("DATACENTER_")}
    environment["PYTHONPATH"] = str(REPO_ROOT / "backend" / "src")

    result = subprocess.run([sys.executable, "-c", _WARNING_PROBE], cwd=REPO_ROOT, env=environment,
                            capture_output=True, text=True, timeout=180, check=False)

    assert result.returncode == 0, result.stderr[-2000:]
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["unknown"] == {}, f"unexpected warning categories during app creation: {payload['unknown']}"
    assert payload["per_create_app"] > 0, (
        "FastAPI no longer reports the parameter-alias warning; the narrow ignore entry in "
        "backend/pyproject.toml is obsolete and should be removed together with this probe"
    )


def test_concurrent_app_creation_stays_stable(tmp_path: Path) -> None:
    """The reported gate failure came from concurrent create_app calls, so keep stressing that shape."""

    def build(_: int) -> TestClient:
        return TestClient(create_app(_settings(tmp_path, auth_state_path=tmp_path / "auth-state.json")))

    with ThreadPoolExecutor(max_workers=2) as pool:
        clients = list(pool.map(build, range(20)))

    assert len(clients) == 20
    assert all(client.get("/api/v1/health").status_code == 200 for client in clients)


def test_query_parameter_contract_survives_the_ignored_warning(tmp_path: Path) -> None:
    client = TestClient(create_app(_settings(tmp_path)))

    parameters = {
        item["name"]
        for item in client.get("/openapi.json").json()["paths"]["/api/v1/bars"]["get"]["parameters"]
    }
    assert {"provider", "symbol", "timeframe", "start", "end"} <= parameters
    assert client.get("/api/v1/bars", params={
        "provider": "dukascopy", "symbol": "EURUSD", "timeframe": "1m", "start": "not-a-time",
    }).status_code == 422
