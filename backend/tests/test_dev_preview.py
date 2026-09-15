import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

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


def test_preview_directories_reject_a_symlinked_child(tmp_path):
    root = tmp_path / "preview"
    (root / "data").mkdir(parents=True)
    target = tmp_path / "outside"
    target.mkdir()
    (root / "data/canonical").symlink_to(target, target_is_directory=True)
    with pytest.raises(dev_preview.PreviewError, match="symbolic link"):
        dev_preview.prepare_preview_directories(root)


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
    assert env["DATACENTER_PROVIDER_ALLOWLIST"] == "dukascopy,fixture"
    assert env["DATACENTER_PREVIEW_SYMBOLS"] == "EURUSD"
    assert env["DATACENTER_ALERTS_ENABLED"] == "false"
    assert env["DATACENTER_AUTH_COOKIE_NAME"].startswith("mdc_preview_")
    assert env["VITE_API_PROXY_TARGET"] == "http://127.0.0.1:21000"
    assert "DATACENTER_DEPLOYMENT_MANIFEST" not in env


def test_live_preview_environment_records_explicit_bounds_and_budgets(tmp_path):
    metadata = {
        "id": "live-one", "mode": "live", "token": "preview-token",
        "ports": {"api": 21000, "ui": 21001},
        "identity": {"commit": "a" * 40, "dirty": False},
        "live_limits": {
            "start": "2026-09-14T00:00:00Z", "end": "2026-09-15T00:00:00Z",
            "request_budget": 30, "byte_budget": 104857600,
        },
    }
    env = dev_preview.process_environment(tmp_path / "live-one", metadata)
    assert env["DATACENTER_DATA_MODE"] == "live"
    assert env["DATACENTER_PROVIDER_ALLOWLIST"] == "dukascopy"
    assert env["DATACENTER_PREVIEW_LIVE_REQUEST_BUDGET"] == "30"
    assert env["DATACENTER_PREVIEW_LIVE_BUDGET_PATH"].endswith("data/live-budget.json")


def test_live_mode_requires_a_bounded_window():
    args = SimpleNamespace(
        command="start", mode="live", live_start="2026-09-14T00:00:00Z",
        live_end="2026-09-16T00:00:00Z", live_request_budget=30,
        live_byte_budget_mib=100,
    )
    with pytest.raises(dev_preview.PreviewError, match="no longer than 24 hours"):
        dev_preview.validate_mode_args(args)


def test_preview_api_identity_must_match_recorded_checkout():
    metadata = {
        "id": "alpha",
        "identity": {"commit": "a" * 40, "dirty": False},
    }
    matching = {"data": {
        "source_commit": "a" * 40,
        "source_dirty": False,
        "environment": "preview:alpha",
        "data_mode": "fixture",
    }}
    assert dev_preview.api_identity_matches(matching, metadata) is True

    wrong_commit = {"data": {**matching["data"], "source_commit": "b" * 40}}
    assert dev_preview.api_identity_matches(wrong_commit, metadata) is False
    assert dev_preview.api_identity_matches(None, metadata) is False


def test_preview_scheduler_status_uses_api_effective_dispatch():
    fresh = {"data": {
        "effective_dispatch": True,
        "scheduler": {
            "dispatch_enabled": True,
            "instance_dispatch_enabled": True,
        },
    }}
    assert dev_preview.scheduler_status(True, fresh) == {
        "process_enabled": True,
        "ledger_enabled": True,
        "effective_dispatch": True,
    }

    stale = {"data": {
        "effective_dispatch": False,
        "heartbeat_status": "stale",
        "scheduler": {
            "dispatch_enabled": True,
            "instance_dispatch_enabled": True,
        },
    }}
    assert dev_preview.scheduler_status(True, stale)["effective_dispatch"] is False


def test_preview_metadata_is_private(tmp_path):
    path = tmp_path / "preview.json"
    dev_preview.write_json(path, {"token": "secret"})
    assert path.stat().st_mode & 0o777 == 0o600


def test_python_executable_keeps_virtualenv_launcher_symlink(tmp_path, monkeypatch):
    launcher = tmp_path / "venv/bin/python"
    launcher.parent.mkdir(parents=True)
    launcher.symlink_to(sys.executable)
    monkeypatch.setattr(
        dev_preview.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0),
    )
    assert dev_preview.python_executable(str(launcher)) == launcher.absolute()
    assert dev_preview.python_executable(str(launcher)) != launcher.resolve()


def test_stop_refuses_a_reused_pid(monkeypatch):
    metadata = {
        "token": "not-this-process",
        "processes": {"api": {"pid": os.getpid(), "start_ticks": -1}},
    }
    monkeypatch.setattr(dev_preview, "matching_token_pids", lambda _token: [])
    with pytest.raises(dev_preview.PreviewError, match="identity changed"):
        dev_preview.terminate(metadata)


def test_start_refuses_identity_change_without_stopping_running_preview(tmp_path, monkeypatch):
    root = tmp_path / "preview"
    metadata_path = root / "preview.json"
    dev_preview.write_json(metadata_path, {
        "id": "alpha", "identity": {"checkout": "old", "commit": "a", "dirty": False,
                                         "worktree_fingerprint": "old"},
        "ports": {"api": 21000, "ui": 21001}, "token": "token", "processes": {},
    })
    current = {"checkout": "new", "commit": "b", "dirty": False, "worktree_fingerprint": "new"}
    monkeypatch.setattr(dev_preview, "validate_parent_environment", lambda: None)
    monkeypatch.setattr(dev_preview, "python_executable", lambda _value: Path("/python"))
    monkeypatch.setattr(dev_preview, "vite_executable", lambda: Path("/vite"))
    monkeypatch.setattr(dev_preview, "checkout_identity", lambda: current)
    monkeypatch.setattr(dev_preview, "status_payload", lambda *_: {"state": "identity_mismatch"})
    stopped = []
    monkeypatch.setattr(dev_preview, "terminate", lambda _metadata: stopped.append(True))
    args = SimpleNamespace(id="alpha", python=None, update=False, json=False)
    with pytest.raises(dev_preview.PreviewError, match="identity changed"):
        dev_preview.start(args, root, metadata_path)
    assert stopped == []


def test_start_failure_cleans_up_a_started_child(tmp_path, monkeypatch):
    root = tmp_path / "preview"
    metadata_path = root / "preview.json"
    identity = {"checkout": str(dev_preview.REPO), "branch": "test", "commit": "a" * 40,
                "dirty": False, "worktree_fingerprint": "clean"}
    monkeypatch.setattr(dev_preview, "validate_parent_environment", lambda: None)
    monkeypatch.setattr(dev_preview, "python_executable", lambda _value: Path(sys.executable))
    monkeypatch.setattr(dev_preview, "vite_executable", lambda: Path("/vite"))
    monkeypatch.setattr(dev_preview, "checkout_identity", lambda: identity)
    monkeypatch.setattr(dev_preview, "allocate_ports", lambda _id: {"api": 21000, "ui": 21001})
    child = None

    def spawn(name, _command, _root, env):
        nonlocal child
        if name != "api":
            raise dev_preview.PreviewError("injected startup failure")
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"], cwd=dev_preview.REPO,
            env=env, start_new_session=True,
        )
        return child, {"pid": child.pid, "start_ticks": dev_preview.proc_start_ticks(child.pid),
                       "log": str(root / "logs/api.log"), "command": [sys.executable]}

    monkeypatch.setattr(dev_preview, "spawn_component", spawn)
    args = SimpleNamespace(id="alpha", python=None, update=False, json=False)
    with pytest.raises(dev_preview.PreviewError, match="injected startup failure"):
        dev_preview.start(args, root, metadata_path)
    assert child is not None
    child.wait(timeout=10)
    assert child.returncode is not None
