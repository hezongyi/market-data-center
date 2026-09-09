from datetime import datetime, timezone
import hashlib
import os
from urllib.parse import quote

from data_center.domain.models import IngestJob, ProviderBar


class YFinanceConnector:
    provider = "yfinance"
    version = "yfinance-v2"

    def fetch_bars(self, job: IngestJob) -> list[ProviderBar]:
        import yfinance as yf
        from curl_cffi.requests import Session

        if job.timeframe != "1d":
            raise ValueError("YFinanceConnector supports timeframe=1d")
        proxy = os.getenv("YFINANCE_PROXY_URL") or os.getenv("DATACENTER_PROXY_URL")
        with Session(impersonate="chrome") as session:
            if proxy:
                session.proxies = {"http": proxy, "https": proxy}
            frame = yf.download(job.symbol, start=job.start.date(), end=job.end.date(),
                                interval="1d", auto_adjust=False, progress=False,
                                session=session, threads=False, timeout=20)
            if frame is None or frame.empty:
                frame = self._chart_frame(session, job)
        if frame is None or frame.empty:
            raise ValueError("yfinance returned no bars for the requested date range")
        if frame.columns.nlevels > 1:
            frame.columns = frame.columns.get_level_values(0)
        frame = frame.dropna(subset=["Open", "High", "Low", "Close"])
        rows = []
        for index, row in frame.iterrows():
            ts = index.to_pydatetime()
            ts = ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts.astimezone(timezone.utc)
            close = float(row["Close"])
            rows.append(ProviderBar(symbol=job.symbol, asset_class=job.asset_class, provider=self.provider, timeframe=job.timeframe, bar_ts=ts, open=float(row["Open"]), high=float(row["High"]), low=float(row["Low"]), close=close, volume=float(row["Volume"]), currency="USD", ingest_ts=datetime.now(timezone.utc), source_hash=hashlib.sha256(f"{job.symbol}:{ts.isoformat()}".encode()).hexdigest()))
        return rows

    @staticmethod
    def _chart_frame(session, job):
        import pandas as pd

        response = session.get(
            "https://query1.finance.yahoo.com/v8/finance/chart/" + quote(job.symbol, safe=""),
            params={"period1": int(job.start.timestamp()), "period2": int(job.end.timestamp()),
                    "interval": "1d", "events": "div,splits"}, timeout=20)
        response.raise_for_status()
        chart = response.json().get("chart", {})
        if chart.get("error"):
            raise ValueError("Yahoo chart request failed")
        result = chart.get("result") or []
        if not result:
            return pd.DataFrame()
        item = result[0]
        values = item["indicators"]["quote"][0]
        # Daily bars use the exchange trading date, matching yf.download's date index.
        index = pd.to_datetime(item.get("timestamp", []), unit="s", utc=True)
        index = index.tz_convert(item["meta"]["exchangeTimezoneName"]).tz_localize(None).normalize()
        return pd.DataFrame({name.title(): values.get(name, [])
                             for name in ("open", "high", "low", "close", "volume")}, index=index)
