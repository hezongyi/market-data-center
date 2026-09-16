"""Deterministic market bars for an explicitly isolated fixture preview.

The connector preserves the requested business identity (for example
``dukascopy/EURUSD/1m/bid``) while making no provider request. It is selected
only when ``DATACENTER_DATA_MODE=fixture`` and is never registered as a
production connector.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from data_center.control_plane import timeframe_delta
from data_center.domain.models import IngestJob, ProviderBar
from data_center.platform_registry import REGISTRY


class PreviewFixtureConnector:
    """Generate complete, session-aware bars for one governed provider job."""

    version = "isolated-preview-fixture-v1"
    end_inclusive = False

    def fetch_bars(self, job: IngestJob) -> list[ProviderBar]:
        instrument = REGISTRY.instrument(job.provider, job.symbol)
        session = REGISTRY.session(instrument.session_profile)
        cadence = timeframe_delta(job.timeframe)
        price_basis = REGISTRY.capability(job.provider).price_bases[0]
        cursor = job.start.astimezone(timezone.utc)
        boundary = job.end.astimezone(timezone.utc)
        rows: list[ProviderBar] = []
        while cursor < boundary:
            if session.is_open(cursor):
                seed = int(hashlib.sha256(
                    f"{job.provider}:{job.symbol}:{job.timeframe}:{cursor.isoformat()}".encode()
                ).hexdigest()[:12], 16)
                close = 1.05 + (seed % 20_000) / 1_000_000
                spread = 0.00008 + (seed % 7) / 1_000_000
                rows.append(ProviderBar(
                    symbol=job.symbol.upper(), asset_class=job.asset_class,
                    provider=job.provider, timeframe=job.timeframe, bar_ts=cursor,
                    open=close - spread / 2, high=close + spread,
                    low=close - spread, close=close,
                    volume=100 + seed % 900, currency=instrument.currency,
                    price_type=price_basis, ingest_ts=datetime.now(timezone.utc),
                    source_hash=hashlib.sha256(
                        f"isolated-preview:{job.symbol}:{cursor.isoformat()}".encode()
                    ).hexdigest(),
                ))
            cursor += cadence
        return rows
