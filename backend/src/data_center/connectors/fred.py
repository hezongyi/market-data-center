from datetime import datetime, timezone
import os

import requests


class FredConnector:
    provider = "fred"

    def __init__(self, api_key: str | None = None, endpoint: str = "https://api.stlouisfed.org/fred/series/observations", metadata_endpoint: str = "https://api.stlouisfed.org/fred/series"):
        self.api_key = api_key or os.getenv("FRED_API_KEY")
        self.endpoint = endpoint
        self.metadata_endpoint = metadata_endpoint

    def fetch_metadata(self, series_id: str) -> dict:
        if not self.api_key:
            raise RuntimeError("FRED_API_KEY is required")
        response = requests.get(self.metadata_endpoint, params={"series_id": series_id, "api_key": self.api_key, "file_type": "json"}, timeout=20)
        response.raise_for_status()
        series = (response.json().get("seriess") or [{}])[0]
        return {
            "frequency": series.get("frequency") or "native",
            "units": series.get("units") or "native",
            "seasonal_adjustment": series.get("seasonal_adjustment") or "native",
        }

    def fetch_observations(self, series_id: str, start: str | None = None, end: str | None = None) -> list[dict]:
        if not self.api_key:
            raise RuntimeError("FRED_API_KEY is required")
        params = {"series_id": series_id, "api_key": self.api_key, "file_type": "json"}
        if start:
            params["observation_start"] = start
        if end:
            params["observation_end"] = end
        response = requests.get(self.endpoint, params=params, timeout=20)
        response.raise_for_status()
        ingest_ts = datetime.now(timezone.utc).isoformat()
        metadata = self.fetch_metadata(series_id)
        return [
            {
                "series_id": series_id,
                "provider": self.provider,
                "observation_date": item["date"],
                "value": None if item["value"] == "." else float(item["value"]),
                "release_ts": item.get("release_ts"),
                "asof_ts": ingest_ts,
                "frequency": metadata["frequency"],
                "units": metadata["units"],
                "seasonal_adjustment": metadata["seasonal_adjustment"],
                "vintage_start": item.get("realtime_start"),
                "vintage_end": item.get("realtime_end"),
                "availability_policy": "realtime_vintage" if item.get("realtime_start") and item.get("realtime_end") else "release_date_unknown_ingest_asof",
                "availability_lag_days": None,
                "ingest_ts": ingest_ts,
            }
            for item in response.json().get("observations", [])
        ]
