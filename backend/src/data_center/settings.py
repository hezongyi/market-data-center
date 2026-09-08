from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DATACENTER_", extra="ignore")

    app_name: str = "Market Data Center"
    api_prefix: str = "/api/v1"
    canonical_root: Path = Path("/home/quant/market_lake/canonical")
    ledger_path: Path = Path("/home/quant/market_lake/canonical/audit/data_center.sqlite")
    api_key: str | None = None
