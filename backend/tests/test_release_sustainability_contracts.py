import json
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib

from data_center import __version__

ROOT = Path(__file__).resolve().parents[2]


def test_runtime_version_and_release_notes_are_consistent():
    backend = tomllib.loads((ROOT / "backend/pyproject.toml").read_text())
    web = json.loads((ROOT / "webui/package.json").read_text())
    assert backend["project"]["version"] == web["version"] == __version__ == "0.2.0"
    assert (ROOT / "docs/releases/v0.2.0.md").is_file()


def test_portable_environment_template_has_no_machine_defaults():
    template = (ROOT / ".env.example").read_text()
    assert "/home/" not in template
    assert "192.168." not in template
    assert "DATACENTER_PROXY_URL=\n" in template


def test_hosted_ci_and_dependency_refresh_cover_supported_runtimes():
    ci = (ROOT / ".github/workflows/ci.yml").read_text()
    refresh = (ROOT / ".github/workflows/dependency-refresh.yml").read_text()
    for version in ("3.10", "3.11", "3.12"):
        assert version in ci
        assert version in refresh
    for workflow in (ci, refresh):
        assert "permissions:" in workflow
        assert "timeout-minutes:" in workflow
        assert "cancel-in-progress:" in workflow
    assert "node-version: \"22\"" in ci
    assert "test:e2e" in (ROOT / "webui/package.json").read_text()
    assert json.loads((ROOT / "webui/package.json").read_text())["devDependencies"]["playwright"] == "1.63.0"


def test_release_receipt_schema_and_baseline_receipt_have_required_fields():
    schema = json.loads((ROOT / "docs/schemas/release-receipt.schema.json").read_text())
    receipt = json.loads((ROOT / "docs/releases/v0.1.0-receipt.json").read_text())
    assert schema["properties"]["receipt_version"]["const"] == "release-receipt.v1"
    assert set(schema["required"]).issubset(receipt)
    assert receipt["commit"] == "679dafff539d8e64938cd098f61088e173ae114d"
    assert receipt["hosted_ci"]["run_id"] == 34442430045


def test_lock_artifacts_exist_for_every_supported_python_minor():
    for suffix in ("310", "311", "312"):
        content = (ROOT / f"backend/constraints/py{suffix}.txt").read_text()
        assert "fastapi==" in content
        assert "yfinance==1.7.0" in content
