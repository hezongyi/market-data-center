"""Read-only projections: the catalog matrix and the governance unit list.

Both answer "what is actually registered, planned and installed" without
changing anything, and both name their evidence so a difference is visible
instead of silently reconciled (spec 3.3, 3.4, AC23).
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from data_center.api.app import create_app
from data_center.operations_views import governance_units
from data_center.production_tasks import ProductionTasks
from data_center.runs.ledger import RunLedger
from data_center.settings import Settings

KEY = "read-model-key"
NOW = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)


def plan_definition(**overrides) -> dict:
    base = {
        "provider": "dukascopy", "symbol": "EURUSD", "raw_timeframe": "1m", "price_basis": "bid",
        "bar_timeframes": [],
        "window_policy": {"mode": "continuous", "history_start": "2026-01-01T00:00:00+00:00"},
        "schedule": {"schedule": "manual"},
    }
    base.update(overrides)
    return base


@pytest.fixture()
def ledger(tmp_path) -> RunLedger:
    return RunLedger(tmp_path / "ledger.sqlite")


@pytest.fixture()
def service(ledger) -> ProductionTasks:
    return ProductionTasks(ledger, canonical_root=None)


def test_matrix_separates_planned_unplanned_and_unavailable(service, ledger):
    matrix = service.catalog_matrix()
    statuses = {row["status"] for row in matrix["rows"]}
    assert statuses <= {"planned", "unplanned", "unavailable", "config_drift"}
    # Nothing is planned yet, so no row claims a plan.
    assert matrix["counts"].get("planned", 0) == 0
    assert matrix["counts"]["unplanned"] > 0
    # A weekly output can be planned (its daily input is registered), and the
    # matrix reports the ownership key the plan would hold.
    weekly = next(row for row in matrix["rows"]
                  if row["dataset_id"] == "market_bars" and row["timeframe"] == "1w"
                  and row["provider"] == "dukascopy" and row["symbol"] == "EURUSD")
    assert weekly["status"] == "unplanned"
    assert weekly["ownership_key"] == "market_bars:dukascopy:EURUSD:1w:bid"
    # Every registered output of every approved instrument is represented.
    assert len({(row["provider"], row["symbol"]) for row in matrix["rows"]}) >= 9

    service.create(definition=plan_definition(), name="EURUSD", task_id="p1", now=NOW)
    after = service.catalog_matrix()
    planned = [row for row in after["rows"] if row["status"] == "planned"]
    assert {row["ownership_key"] for row in planned} == {"provider_bars:dukascopy:EURUSD:1m:bid"}
    assert planned[0]["task_id"] == "p1" and planned[0]["plan_state"] == "paused"


def test_matrix_marks_a_drifting_plan_as_config_drift(service, ledger):
    service.create(definition=plan_definition(), name="EURUSD", task_id="p1", now=NOW,
                   desired_state="enabled")
    ledger.set_config_digest("p1", 1, "stale-digest")
    service.reconcile_config_digest()
    row = next(item for item in service.catalog_matrix()["rows"]
               if item["ownership_key"] == "provider_bars:dukascopy:EURUSD:1m:bid")
    assert row["status"] == "config_drift"


def test_matrix_never_writes(service, ledger):
    before = (ledger.write_audit_entries(), len(ledger.list_production_tasks()))
    service.catalog_matrix()
    assert (ledger.write_audit_entries(), len(ledger.list_production_tasks())) == before


def test_matrix_route_reports_counts(tmp_path):
    config = Settings(canonical_root=tmp_path / "lake", ledger_path=tmp_path / "runs.sqlite",
                      evidence_root=tmp_path / "evidence", api_key=KEY)
    client = TestClient(create_app(config))
    response = client.get("/api/v1/production/catalog-matrix", headers={"X-API-Key": KEY})
    assert response.status_code == 200
    payload = response.json()["data"]
    assert payload["rows"] and payload["counts"]["unplanned"] > 0
    assert "never creates or widens a plan" in payload["note"]


def test_governance_units_report_declaration_differences(tmp_path):
    declared = tmp_path / "systemd"
    declared.mkdir()
    (declared / "market-data-center-monitor.timer").write_text(
        "[Timer]\nOnUnitInactiveSec=60s\nOnCalendar=*-*-* 03:00:00 UTC\n")
    (declared / "market-data-center-smoke.timer").write_text("[Timer]\nOnCalendar=*-*-* 04:00:00 UTC\n")
    view = governance_units(
        declared_root=declared,
        # The host runs the monitor and something the repository does not declare.
        installed=["market-data-center-monitor.timer", "market-data-center-legacy.timer"],
        receipt_index=None)
    assert view["available"] is True
    assert view["declared_not_installed"] == ["market-data-center-smoke.timer"]
    assert view["installed_not_declared"] == ["market-data-center-legacy.timer"]
    monitor = next(unit for unit in view["units"] if unit["unit"] == "market-data-center-monitor.timer")
    assert monitor["declaration"] == "installed"
    assert monitor["cadence"] == {"OnUnitInactiveSec": "60s", "OnCalendar": "*-*-* 03:00:00 UTC"}
    assert monitor["read_only"] is True and monitor["receipt_action"] == "monitor"
    assert all("command" not in unit for unit in view["units"])


def test_governance_units_report_an_unreadable_host_as_unknown(tmp_path):
    declared = tmp_path / "systemd"
    declared.mkdir()
    (declared / "market-data-center-monitor.timer").write_text("[Timer]\nOnCalendar=*-*-* 03:00:00 UTC\n")
    view = governance_units(declared_root=declared, installed=None, receipt_index=None)
    # Not knowing the host must never be reported as "nothing is installed".
    assert view["available"] is False
    assert view["declared_not_installed"] == []
    assert view["units"][0]["declaration"] == "unknown"


def test_governance_units_route_uses_the_repository_declaration(tmp_path):
    config = Settings(canonical_root=tmp_path / "lake", ledger_path=tmp_path / "runs.sqlite",
                      evidence_root=tmp_path / "evidence", api_key=KEY)
    client = TestClient(create_app(config))
    response = client.get("/api/v1/operations/units", headers={"X-API-Key": KEY})
    assert response.status_code == 200
    payload = response.json()["data"]
    assert any(unit["unit"] == "market-data-center-monitor.timer" for unit in payload["units"])
    assert all(unit["read_only"] for unit in payload["units"])
