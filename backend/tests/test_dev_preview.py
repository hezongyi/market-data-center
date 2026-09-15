import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/dev_preview.py"
SPEC = importlib.util.spec_from_file_location("dev_preview", SCRIPT)
dev_preview = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(dev_preview)


def test_preview_root_rejects_symlinked_parent(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(real, target_is_directory=True)
    with pytest.raises(dev_preview.PreviewError, match="symbolic link"):
        dev_preview.preview_root("alpha", str(linked))


def test_preview_root_rejects_workspace_and_production_roots():
    with pytest.raises(dev_preview.PreviewError, match="overlaps the checkout"):
        dev_preview.preview_root("alpha", str(dev_preview.REPO))
    with pytest.raises(dev_preview.PreviewError, match="production data root"):
        dev_preview.preview_root("alpha", "/home/quant/market_lake")


def test_unexpected_business_environment_is_rejected(monkeypatch):
    monkeypatch.setenv("DATACENTER_CANONICAL_ROOT", "/tmp/not-inherited")
    with pytest.raises(dev_preview.PreviewError, match="DATACENTER_CANONICAL_ROOT"):
        dev_preview.validate_parent_environment()


def test_preview_environment_is_explicit_and_fixture_only(tmp_path):
    metadata = {
        "id": "alpha",
        "token": "preview-token",
        "ports": {"api": 21000, "ui": 21001},
        "identity": {"commit": "a" * 40, "dirty": False},
    }
    env = dev_preview.process_environment(tmp_path / "alpha", metadata)
    assert env["DATACENTER_PROVIDER_ALLOWLIST"] == "fixture"
    assert env["DATACENTER_ALERTS_ENABLED"] == "false"
    assert env["DATACENTER_AUTH_COOKIE_NAME"].startswith("mdc_preview_")
    assert env["VITE_API_PROXY_TARGET"] == "http://127.0.0.1:21000"
    assert "DATACENTER_DEPLOYMENT_MANIFEST" not in env


def test_preview_metadata_is_private(tmp_path):
    path = tmp_path / "preview.json"
    dev_preview.write_json(path, {"token": "secret"})
    assert path.stat().st_mode & 0o777 == 0o600
