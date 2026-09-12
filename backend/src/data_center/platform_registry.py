"""Built-in market-data platform control-plane configuration."""
from __future__ import annotations

import hashlib
import json

from data_center.control_plane import (
    ControlPlaneRegistry,
    InstrumentMetadata,
    MaintenancePolicy,
    ProviderCapability,
    QualityProfile,
    SessionProfile,
    TransformRecipe,
)

REGISTRY = ControlPlaneRegistry()

REGISTRY.register_session(SessionProfile(profile_id="utc_24x7", mode="continuous"))
REGISTRY.register_session(SessionProfile(profile_id="weekdays_utc", mode="weekdays"))
REGISTRY.register_session(SessionProfile(profile_id="dukascopy_fx_weekdays_utc", mode="weekdays"))
REGISTRY.register_session(SessionProfile(profile_id="dukascopy_metals_weekdays_utc", mode="weekdays"))
REGISTRY.register_session(SessionProfile(profile_id="dukascopy_crypto_24x7", mode="continuous"))
REGISTRY.register_session(SessionProfile(profile_id="exchange", mode="weekdays"))
REGISTRY.register_session(SessionProfile(profile_id="instrument", mode="continuous"))
REGISTRY.register_maintenance_policy(MaintenancePolicy(
    policy_id="default", max_window_days=31, tail_days=2, shard_days=7, closed_bar_lag_minutes=1,
))
REGISTRY.register_maintenance_policy(MaintenancePolicy(
    policy_id="dukascopy_1m", max_window_days=31, tail_days=2, shard_days=7,
    shard_minutes=60, closed_bar_lag_minutes=180,
))
REGISTRY.register_quality_profile(QualityProfile(profile_id="provider_bars"))
REGISTRY.register_quality_profile(QualityProfile(profile_id="market_bars"))
REGISTRY.register_quality_profile(QualityProfile(profile_id="economic_observations",
                                                  require_session_coverage=False,
                                                  require_ohlc=False))

for capability in (
    ProviderCapability(provider="fixture", asset_classes=("*",), timeframes=("1m", "1d"),
                       price_bases=("raw",), max_window_days=366, session_profile="utc_24x7"),
    ProviderCapability(provider="binance", asset_classes=("crypto",), timeframes=("1m", "1h", "1d"),
                       price_bases=("raw",), max_window_days=31, session_profile="utc_24x7"),
    ProviderCapability(provider="yfinance", asset_classes=("equity", "etf"), timeframes=("1d",),
                       price_bases=("adjusted", "raw"), max_window_days=3660, session_profile="exchange"),
    ProviderCapability(provider="dukascopy", asset_classes=("fx", "commodity", "crypto"),
                       timeframes=("1m", "5m", "15m", "30m", "1h", "4h", "1d"),
                       maintenance_timeframes=("1m",),
                       price_bases=("bid",), max_window_days=31, session_profile="instrument"),
):
    REGISTRY.register_capability(capability)


# Approved rollout universe.  Adding a production symbol is an explicit
# control-plane change so unknown symbols cannot inherit guessed session or
# currency semantics.  Acceptance runs may still use synthetic symbols.
for _instrument in (
    InstrumentMetadata(provider="dukascopy", symbol="EURUSD", canonical_symbol="EURUSD",
                       provider_symbol="EUR/USD", asset_class="fx", currency="USD",
                       session_profile="dukascopy_fx_weekdays_utc", calendar_profile="fx_weekdays_v1"),
    InstrumentMetadata(provider="dukascopy", symbol="GBPUSD", canonical_symbol="GBPUSD",
                       provider_symbol="GBP/USD", asset_class="fx", currency="USD",
                       session_profile="dukascopy_fx_weekdays_utc", calendar_profile="fx_weekdays_v1"),
    InstrumentMetadata(provider="dukascopy", symbol="USDCAD", canonical_symbol="USDCAD",
                       provider_symbol="USD/CAD", asset_class="fx", currency="CAD",
                       session_profile="dukascopy_fx_weekdays_utc", calendar_profile="fx_weekdays_v1"),
    InstrumentMetadata(provider="dukascopy", symbol="USDJPY", canonical_symbol="USDJPY",
                       provider_symbol="USD/JPY", asset_class="fx", currency="JPY",
                       session_profile="dukascopy_fx_weekdays_utc", calendar_profile="fx_weekdays_v1"),
    InstrumentMetadata(provider="dukascopy", symbol="AUDJPY", canonical_symbol="AUDJPY",
                       provider_symbol="AUD/JPY", asset_class="fx", currency="JPY",
                       session_profile="dukascopy_fx_weekdays_utc", calendar_profile="fx_weekdays_v1"),
    InstrumentMetadata(provider="dukascopy", symbol="GBPJPY", canonical_symbol="GBPJPY",
                       provider_symbol="GBP/JPY", asset_class="fx", currency="JPY",
                       session_profile="dukascopy_fx_weekdays_utc", calendar_profile="fx_weekdays_v1"),
    InstrumentMetadata(provider="dukascopy", symbol="XAUUSD", canonical_symbol="XAUUSD",
                       provider_symbol="XAU/USD", asset_class="commodity", currency="USD",
                       session_profile="dukascopy_metals_weekdays_utc", calendar_profile="metals_weekdays_v1"),
    InstrumentMetadata(provider="dukascopy", symbol="BTCUSD", canonical_symbol="BTCUSD",
                       provider_symbol="BTC/USD", asset_class="crypto", currency="USD",
                       session_profile="dukascopy_crypto_24x7", calendar_profile="crypto_24x7_v1"),
    InstrumentMetadata(provider="binance", symbol="BTCUSDT", canonical_symbol="BTCUSDT",
                       asset_class="crypto", currency="USDT", session_profile="utc_24x7",
                       calendar_profile="crypto_24x7_v1"),
):
    REGISTRY.register_instrument(_instrument)

# Canonical first-hop recipes are configuration, not provider branches.  They
# deliberately start at the governed raw 1m layer.
for _target in ("5m", "15m", "30m", "1h", "4h", "1d"):
    register_id = f"utc-24x7-1m-to-{_target}-ohlcv"
    REGISTRY.register_recipe(TransformRecipe(
        recipe_id=register_id, version="1", input_dataset="provider_bars",
        output_dataset="market_bars", source_timeframe="1m", target_timeframe=_target,
        allowed_schema_versions=("provider_bars.v1",), session_profile="instrument",
        calendar_profile="instrument", aggregation="ohlcv", partial_bucket_policy="drop",
        missing_input_policy="fail", materialization="persisted", publication_policy="canonical",
    ))

for _target in ("1w", "1mo"):
    REGISTRY.register_recipe(TransformRecipe(
        recipe_id=f"utc-24x7-1d-to-{_target}-ohlcv", version="1",
        input_dataset="market_bars", output_dataset="market_bars",
        source_timeframe="1d", target_timeframe=_target,
        allowed_schema_versions=("market_bars.v1",), session_profile="instrument",
        calendar_profile="instrument", aggregation="ohlcv", partial_bucket_policy="drop",
        missing_input_policy="fail", materialization="persisted", publication_policy="canonical",
        input_recipe_id="utc-24x7-1m-to-1d-ohlcv", input_recipe_version="1",
    ))


def config_digest(value) -> str:
    payload = value.model_dump(mode="json") if hasattr(value, "model_dump") else value
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def register_recipe(recipe: TransformRecipe) -> TransformRecipe:
    return REGISTRY.register_recipe(recipe)


def resolve_capability(provider: str, *, allow_unregistered: bool = False) -> ProviderCapability:
    try:
        return REGISTRY.capability(provider)
    except ValueError:
        if not allow_unregistered:
            raise
        return ProviderCapability(provider=provider, asset_classes=("*",), timeframes=("*",),
                                  price_bases=("raw",), max_window_days=31,
                                  session_profile="utc_24x7")


def maintenance_policy_for(provider: str, timeframe: str) -> MaintenancePolicy:
    """Resolve the most specific registered policy, then use the platform default."""
    for policy_id in (f"{provider}_{timeframe}", "default"):
        try:
            return REGISTRY.maintenance_policy(policy_id)
        except ValueError:
            continue
    raise ValueError("default maintenance policy is not registered")
