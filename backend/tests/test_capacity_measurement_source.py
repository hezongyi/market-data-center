"""The capacity measurement source must travel with the numbers (issue #86).

A pinned acceptance measurement is deterministic rather than a live disk reading, so every surface that
reports capacity - readiness, metrics, the capacity history view and the capacity receipts - has to say
which one it is. Otherwise a screenshot or a receipt quoting "0.03 / ok" reads as a real disk state.
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from data_center.api.app import create_app
from data_center.capacity import CapacityPolicy, FixedCapacityPolicy
from data_center.observability import AlertSink
from data_center.operations import retention_audit
from data_center.settings import Settings


def settings_for(tmp_path: Path, **overrides) -> Settings:
    values = {
        "canonical_root": tmp_path / "lake",
        "ledger_path": tmp_path / "ledger.sqlite",
        "evidence_root": tmp_path / "evidence",
        "auth_cookie_secure": False,
    }
    values.update(overrides)
    return Settings(**values)


def test_live_policy_marks_its_measurement_as_live(tmp_path: Path) -> None:
    policy = CapacityPolicy()

    assert policy.inspect(tmp_path).as_dict()["measurement_source"] == "live"


def test_pinned_policy_marks_its_measurement_as_fixed_acceptance(tmp_path: Path) -> None:
    policy = FixedCapacityPolicy.for_free_ratio(0.03)

    snapshot = policy.inspect(tmp_path).as_dict()

    assert snapshot["measurement_source"] == "fixed_acceptance"
    assert snapshot["free_ratio"] == 0.03


def test_capacity_views_report_the_measurement_source(tmp_path: Path) -> None:
    http = TestClient(create_app(settings_for(tmp_path)))

    live_history = http.get("/api/v1/operations/capacity-history").json()["data"]
    assert live_history["measurement_source"] == "live"
    assert live_history["live"]["measurement_source"] == "live"
    assert live_history["live"]["fixed_measurement"] is False
    assert live_history["live"]["recorded_only"] is False
    assert http.get("/api/v1/health/ready").json()["data"]["capacity_measurement_source"] == "live"
    assert http.get("/api/v1/metrics").json()["data"]["capacity"]["measurement_source"] == "live"

    pinned = TestClient(create_app(settings_for(tmp_path, capacity_fixed_free_ratio=0.03)))

    pinned_history = pinned.get("/api/v1/operations/capacity-history").json()["data"]
    assert pinned_history["measurement_source"] == "fixed_acceptance"
    assert pinned_history["live"]["fixed_measurement"] is True
    assert pinned.get("/api/v1/health/ready").json()["data"]["capacity_measurement_source"] == "fixed_acceptance"
    assert pinned.get("/api/v1/metrics").json()["data"]["capacity"]["measurement_source"] == "fixed_acceptance"


def test_recorded_transitions_are_marked_as_recorded(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    sink = AlertSink(settings.evidence_root / "alerts", True)
    sink.emit("capacity_warning", identity="test", fields={
        "status": "warning", "free_ratio": 0.12, "warning_free_ratio": 0.15, "critical_free_ratio": 0.10,
    })
    http = TestClient(create_app(settings))

    history = http.get("/api/v1/operations/capacity-history").json()["data"]

    assert history["recorded_only"] is True
    assert history["events"][0]["event"] == "capacity_warning"
    assert history["events"][0]["recorded_only"] is True
    assert history["live"]["recorded_only"] is False


def test_capacity_receipt_carries_the_measurement_source(tmp_path: Path) -> None:
    evidence_root = tmp_path / "evidence"

    live = retention_audit(tmp_path / "lake", capacity_policy=CapacityPolicy(), evidence_root=evidence_root)
    pinned = retention_audit(tmp_path / "lake", capacity_policy=FixedCapacityPolicy.for_free_ratio(0.03),
                             evidence_root=evidence_root)

    assert live["capacity"]["measurement_source"] == "live"
    assert pinned["capacity"]["measurement_source"] == "fixed_acceptance"

    receipts = sorted((evidence_root / "operations" / "capacity_check").glob("*.json"))
    recorded = [json.loads(path.read_text())["details"]["capacity"]["measurement_source"] for path in receipts]
    assert sorted(recorded) == ["fixed_acceptance", "live"]
