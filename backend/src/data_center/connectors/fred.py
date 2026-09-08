from datetime import datetime, timezone
import os

import requests


class FredConnector:
    provider = "fred"

    def __init__(self, api_key: str | None = None, endpoint: str = "https://api.stlouisfed.org/fred/series/observations"):
        self.api_key = api_key or os.getenv("FRED_API_KEY")
        self.endpoint = endpoint

    def fetch_observations(self, series_id: str, start: str | None = None, end: str | None = None) -> list[dict]:
        if not self.api_key:
            raise RuntimeError("FRED_API_KEY is required")
        params = {"series_id": series_id, "api_key": self.api_key, "file_type": "json"}
        if start: params["observation_start"] = start
        if end: params["observation_end"] = end
        response = requests.get(self.endpoint, params=params, timeout=20)
        response.raise_for_status()
        ingest_ts = datetime.now(timezone.utc).isoformat()
        return [
            {
                "series_id": series_id,
                "provider": self.provider,
                "observation_date": item["date"],
                "value": None if item["value"] == "." else float(item["value"]),
                "vintage_start": item.get("realtime_start"),
                "vintage_end": item.get("realtime_end"),
                "availability_policy": "fred_realtime_vintage",
                "ingest_ts": ingest_ts,
            }
            for item in response.json().get("observations", [])
        ]
