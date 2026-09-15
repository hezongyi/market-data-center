from data_center.connectors.registry import get_connector


def test_connector_registry_includes_fixture_and_yfinance() -> None:
    assert get_connector("fixture").provider == "fixture"
    assert get_connector("yfinance").provider == "yfinance"


def test_connector_allowlist_blocks_real_providers_in_fixture_preview(monkeypatch) -> None:
    monkeypatch.setenv("DATACENTER_PROVIDER_ALLOWLIST", "fixture")
    assert get_connector("fixture").provider == "fixture"
    try:
        get_connector("dukascopy")
    except ValueError as exc:
        assert str(exc) == "provider disabled in this environment: dukascopy"
    else:
        raise AssertionError("fixture preview allowed a real provider connector")
