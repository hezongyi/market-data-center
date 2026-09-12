from data_center.settings import Settings


def test_maintenance_symbol_allowlist_normalizes_csv_and_whitespace(monkeypatch):
    monkeypatch.setenv("DATACENTER_MAINTENANCE_SYMBOLS", "eurusd, GBPUSD  xauusd")

    assert Settings().maintenance_symbol_list() == ("EURUSD", "GBPUSD", "XAUUSD")


def test_empty_maintenance_symbol_allowlist_is_unset(monkeypatch):
    monkeypatch.delenv("DATACENTER_MAINTENANCE_SYMBOLS", raising=False)

    assert Settings().maintenance_symbol_list() == ()
