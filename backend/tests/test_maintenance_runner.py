from data_center.settings import Settings


def test_maintenance_symbol_allowlist_normalizes_csv_and_whitespace(monkeypatch):
    monkeypatch.setenv("DATACENTER_MAINTENANCE_SYMBOLS", "eurusd, GBPUSD  xauusd")

    assert Settings().maintenance_symbol_list() == ("EURUSD", "GBPUSD", "XAUUSD")


def test_empty_maintenance_symbol_allowlist_is_unset(monkeypatch):
    monkeypatch.delenv("DATACENTER_MAINTENANCE_SYMBOLS", raising=False)

    assert Settings().maintenance_symbol_list() == ()


def test_scheduled_end_applies_provider_availability_lag():
    from datetime import datetime, timezone

    from data_center.maintenance_runner import _scheduled_end

    now = datetime(2026, 9, 12, 4, 7, 31, tzinfo=timezone.utc)
    assert _scheduled_end(now, lag_minutes=180) == datetime(2026, 9, 12, 1, 7, tzinfo=timezone.utc)
