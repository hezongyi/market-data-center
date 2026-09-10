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
    backup_root: Path | None = None
    api_key: str | None = None
    webui_dist: Path | None = None
    deployment_manifest: Path | None = None
    release_root: Path = Path("./var/releases")
    restore_staging_root: Path = Path("./var/restore-staging")
    monitor_runtime_max_seconds: float = 30.0
    monitor_delivery_batch_size: int = 50
    monitor_delivery_timeout_seconds: float = 5.0
    monitor_delivery_budget_seconds: float = 20.0
    worker_timeout_seconds: float = 120.0
    capacity_warning_free_ratio: float = 0.15
    capacity_critical_free_ratio: float = 0.10

    @model_validator(mode="after")
    def validate_network_boundary(self):
        import ipaddress
        try:
            local = ipaddress.ip_address(self.host).is_loopback
        except ValueError:
            local = self.host == "localhost"
        if not local and not self.api_key:
            raise ValueError("DATACENTER_API_KEY is required for non-loopback binding")
        if not 0 <= self.capacity_critical_free_ratio < self.capacity_warning_free_ratio <= 1:
            raise ValueError("capacity ratios require 0 <= critical < warning <= 1")
        return self

    def capacity_policy(self):
        from data_center.capacity import CapacityPolicy

        return CapacityPolicy(
            warning_free_ratio=self.capacity_warning_free_ratio,
            critical_free_ratio=self.capacity_critical_free_ratio,
        )
