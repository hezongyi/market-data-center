from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, ClassVar

import dukascopy_python
import pandas as pd

from data_center.domain.models import IngestJob, ProviderBar


class DukascopyConnector:
    """Historical provider-native BID bars through dukascopy-python."""

    provider = "dukascopy"
    version = "dukascopy-python-4.0.1-bid-v1"

    _INTERVALS: ClassVar[dict[str, str]] = {
        "1m": dukascopy_python.INTERVAL_MIN_1,
        "5m": dukascopy_python.INTERVAL_MIN_5,
        "15m": dukascopy_python.INTERVAL_MIN_15,
        "30m": dukascopy_python.INTERVAL_MIN_30,
        "1h": dukascopy_python.INTERVAL_HOUR_1,
        "4h": dukascopy_python.INTERVAL_HOUR_4,
        "1d": dukascopy_python.INTERVAL_DAY_1,
    }
    _TIMEFRAME_SECONDS: ClassVar[dict[str, int]] = {
        "1m": 60,
        "5m": 300,
        "15m": 900,
        "30m": 1800,
        "1h": 3600,
        "4h": 14400,
        "1d": 86400,
    }
    _MAX_RANGE_DAYS: ClassVar[dict[str, int]] = {
        "1m": 31,
        "5m": 31,
        "15m": 31,
        "30m": 31,
        "1h": 366,
        "4h": 366,
        "1d": 3660,
    }
    _ASSET_CLASSES: ClassVar[set[str]] = {"fx", "commodity", "crypto"}
    _REQUIRED_COLUMNS: ClassVar[tuple[str, ...]] = ("open", "high", "low", "close", "volume")

    def __init__(
        self,
        *,
        fetch: Callable[..., Any] | None = None,
        now_func: Callable[[], datetime] | None = None,
        proxy_url: str | None = None,
        request_timeout_seconds: float | None = None,
    ) -> None:
        self._fetch = fetch or dukascopy_python.fetch
        self._now_func = now_func or (lambda: datetime.now(timezone.utc))
        self.proxy_url = proxy_url
        configured_timeout = os.getenv("DUKASCOPY_REQUEST_TIMEOUT_SECONDS")
        self.request_timeout_seconds = (
            float(configured_timeout)
            if configured_timeout and configured_timeout.strip()
            else float(request_timeout_seconds) if request_timeout_seconds is not None else 30.0
        )

    @staticmethod
    def _utc(value: datetime, *, label: str) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"Dukascopy {label} must be timezone-aware")
        return value.astimezone(timezone.utc)

    @staticmethod
    def _provider_symbol(symbol: str) -> tuple[str, str, str]:
        normalized = symbol.strip().upper()
        if "/" in normalized:
            pieces = normalized.split("/")
            if len(pieces) != 2 or any(len(piece) != 3 or not piece.isalnum() for piece in pieces):
                raise ValueError(f"unsupported Dukascopy symbol: {symbol}")
            base, quote = pieces
        else:
            if len(normalized) != 6 or not normalized.isalnum():
                raise ValueError(f"unsupported Dukascopy symbol: {symbol}")
            base, quote = normalized[:3], normalized[3:]
        return f"{base}/{quote}", f"{base}{quote}", quote

    @staticmethod
    def _naive_utc(value: datetime) -> datetime:
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    def _effective_end(self, requested_end: datetime, timeframe: str) -> datetime:
        now = self._utc(self._now_func(), label="clock")
        seconds = self._TIMEFRAME_SECONDS[timeframe]
        closed_boundary = datetime.fromtimestamp((int(now.timestamp()) // seconds) * seconds, tz=timezone.utc)
        return min(requested_end, closed_boundary)

    @contextmanager
    def _http_policy(self) -> Iterator[None]:
        requests_module = getattr(dukascopy_python, "requests", None)
        if requests_module is None or not hasattr(requests_module, "get"):
            yield
            return
        original_get = requests_module.get
        proxy = self.proxy_url or os.getenv("DUKASCOPY_PROXY_URL") or os.getenv("DATACENTER_PROXY_URL")

        def governed_get(*args, **kwargs):
            kwargs.setdefault("timeout", self.request_timeout_seconds)
            if proxy:
                kwargs.setdefault("proxies", {"http": proxy, "https": proxy})
            response = original_get(*args, **kwargs)
            response.raise_for_status()
            return response

        requests_module.get = governed_get
        try:
            yield
        finally:
            requests_module.get = original_get

    @classmethod
    def _validate_job(cls, job: IngestJob) -> tuple[datetime, datetime]:
        if job.provider != cls.provider:
            raise ValueError(f"DukascopyConnector cannot fetch provider={job.provider}")
        if job.asset_class not in cls._ASSET_CLASSES:
            raise ValueError(f"unsupported Dukascopy asset class: {job.asset_class}")
        if job.timeframe not in cls._INTERVALS:
            raise ValueError(f"unsupported Dukascopy timeframe: {job.timeframe}")
        start = cls._utc(job.start, label="start")
        end = cls._utc(job.end, label="end")
        if end <= start:
            raise ValueError("Dukascopy ingest requires end after start")
        if end - start > pd.Timedelta(days=cls._MAX_RANGE_DAYS[job.timeframe]):
            raise ValueError(f"Dukascopy {job.timeframe} request exceeds bounded range")
        return start, end

    @classmethod
    def _normalized_frame(cls, frame) -> pd.DataFrame:
        if frame is None or frame.empty:
            raise ValueError("Dukascopy returned no bars for the requested range")
        normalized = frame.copy()
        normalized.columns = [str(column).strip().lower() for column in normalized.columns]
        missing = [column for column in cls._REQUIRED_COLUMNS if column not in normalized.columns]
        if missing:
            raise ValueError(f"Dukascopy payload missing required columns: {missing}")
        if not isinstance(normalized.index, pd.DatetimeIndex):
            raise TypeError("Dukascopy payload must use a DatetimeIndex")
        if normalized.index.tz is None:
            normalized.index = normalized.index.tz_localize("UTC")
        else:
            normalized.index = normalized.index.tz_convert("UTC")
        if not normalized.index.is_monotonic_increasing:
            raise ValueError("Dukascopy payload timestamps must be sorted ascending")
        if normalized.index.has_duplicates:
            raise ValueError("Dukascopy payload contains duplicate timestamps")
        return normalized

    def fetch_bars(self, job: IngestJob) -> list[ProviderBar]:
        start, requested_end = self._validate_job(job)
        provider_symbol, canonical_symbol, quote_currency = self._provider_symbol(job.symbol)
        effective_end = self._effective_end(requested_end, job.timeframe)
        if effective_end <= start:
            raise ValueError("Dukascopy requested range has no completed bars")
        with self._http_policy():
            frame = self._fetch(
                instrument=provider_symbol,
                interval=self._INTERVALS[job.timeframe],
                offer_side=dukascopy_python.OFFER_SIDE_BID,
                start=self._naive_utc(start),
                end=self._naive_utc(effective_end),
                max_retries=0,
                limit=30_000,
            )
        frame = self._normalized_frame(frame)
        ingest_ts = datetime.now(timezone.utc)
        rows: list[ProviderBar] = []
        for timestamp, values in frame.iterrows():
            bar_ts = timestamp.to_pydatetime().astimezone(timezone.utc)
            if not (start <= bar_ts < effective_end):
                continue
            if any(pd.isna(values[column]) for column in ("open", "high", "low", "close")):
                raise ValueError("Dukascopy payload contains missing OHLC values")
            volume = None if pd.isna(values["volume"]) else float(values["volume"])
            identity = {
                "provider": self.provider,
                "symbol": canonical_symbol,
                "timeframe": job.timeframe,
                "bar_ts": bar_ts.isoformat(),
                "price_type": "bid",
                "open": float(values["open"]),
                "high": float(values["high"]),
                "low": float(values["low"]),
                "close": float(values["close"]),
                "volume": volume,
            }
            rows.append(
                ProviderBar(
                    symbol=canonical_symbol,
                    asset_class=job.asset_class,
                    provider=self.provider,
                    timeframe=job.timeframe,
                    bar_ts=bar_ts,
                    open=identity["open"],
                    high=identity["high"],
                    low=identity["low"],
                    close=identity["close"],
                    volume=volume,
                    currency=quote_currency,
                    price_type="bid",
                    ingest_ts=ingest_ts,
                    source_hash=hashlib.sha256(
                        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
                    ).hexdigest(),
                )
            )
        if not rows:
            raise ValueError("Dukascopy returned no completed bars for the requested range")
        return rows
