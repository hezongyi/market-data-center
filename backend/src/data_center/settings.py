from pathlib import Path

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DATACENTER_", extra="ignore")

    host: str = "127.0.0.1"
    port: int = 18380
    alerts_enabled: bool = True
    alert_webhook_url: str | None = None
    app_name: str = "Market Data Center"
    api_prefix: str = "/api/v1"
    canonical_root: Path = Path("/home/quant/market_lake/canonical")
    ledger_path: Path = Path("/home/quant/market_lake/canonical/audit/data_center.sqlite")
    evidence_root: Path = Path("/home/quant/market_lake/evidence/data-center")
    api_key: str | None = None
    webui_dist: Path | None = None
    worker_timeout_seconds: float = 120.0

    @model_validator(mode="after")
    def validate_network_boundary(self):
        import ipaddress
        try:
            local = ipaddress.ip_address(self.host).is_loopback
        except ValueError:
            local = self.host == "localhost"
        if not local and not self.api_key:
            raise ValueError("DATACENTER_API_KEY is required for non-loopback binding")
        return self
