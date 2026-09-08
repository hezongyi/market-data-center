from datetime import datetime, timezone
import hashlib

from data_center.domain.models import IngestJob, ProviderBar


class YFinanceConnector:
    provider = "yfinance"

    def fetch_bars(self, job: IngestJob) -> list[ProviderBar]:
        import yfinance as yf
        frame = yf.download(job.symbol, start=job.start.date(), end=job.end.date(), auto_adjust=False, progress=False)
        rows = []
        for index, row in frame.iterrows():
            ts = index.to_pydatetime().replace(tzinfo=timezone.utc)
            close = float(row["Close"])
            rows.append(ProviderBar(symbol=job.symbol, asset_class=job.asset_class, provider=self.provider, timeframe=job.timeframe, bar_ts=ts, open=float(row["Open"]), high=float(row["High"]), low=float(row["Low"]), close=close, volume=float(row["Volume"]), currency="USD", ingest_ts=datetime.now(timezone.utc), source_hash=hashlib.sha256(f"{job.symbol}:{ts.isoformat()}".encode()).hexdigest()))
        return rows

