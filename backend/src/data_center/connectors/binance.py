import hashlib
import os
from datetime import datetime, timezone

import requests

from data_center.domain.models import IngestJob, ProviderBar


class BinanceConnector:
    provider = "binance"
    version = "binance-v1"
    endpoint = "https://api.binance.com/api/v3/klines"

    def fetch_bars(self, job: IngestJob) -> list[ProviderBar]:
        interval = {"1d": "1d", "1h": "1h", "1m": "1m"}.get(job.timeframe)
        if interval is None:
            raise ValueError(f"unsupported Binance timeframe: {job.timeframe}")
        proxy = os.getenv("BINANCE_PROXY_URL") or os.getenv("DATACENTER_PROXY_URL")
        response = requests.get(self.endpoint, params={"symbol": job.symbol.upper(), "interval": interval, "startTime": int(job.start.timestamp() * 1000), "endTime": int(job.end.timestamp() * 1000), "limit": 1000}, timeout=20, proxies={"http": proxy, "https": proxy} if proxy else None)
        response.raise_for_status()
        rows = []
        for item in response.json():
            ts = datetime.fromtimestamp(item[0] / 1000, tz=timezone.utc)
            rows.append(ProviderBar(symbol=job.symbol.upper(), asset_class=job.asset_class, provider=self.provider, timeframe=job.timeframe, bar_ts=ts, open=float(item[1]), high=float(item[2]), low=float(item[3]), close=float(item[4]), volume=float(item[5]), currency="USDT", ingest_ts=datetime.now(timezone.utc), source_hash=hashlib.sha256(f"{job.symbol}:{item[0]}".encode()).hexdigest()))
        return rows
