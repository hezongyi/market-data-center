from typing import Iterable

from data_center.domain.models import ProviderBar


def check_provider_bars(rows: Iterable[ProviderBar]) -> list[dict]:
    findings: list[dict] = []
    for row in rows:
        if row.high < max(row.open, row.close) or row.low > min(row.open, row.close):
            findings.append({"severity": "error", "code": "ohlc_inconsistent", "bar_ts": row.bar_ts.isoformat()})
        if row.volume is not None and row.volume < 0:
            findings.append({"severity": "error", "code": "negative_volume", "bar_ts": row.bar_ts.isoformat()})
    return findings

