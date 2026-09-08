from data_center.connectors.registry import get_connector


def test_connector_registry_includes_fixture_and_yfinance() -> None:
    assert get_connector("fixture").provider == "fixture"
    assert get_connector("yfinance").provider == "yfinance"
