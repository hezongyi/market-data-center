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


def check_economic_observations(rows: Iterable[dict]) -> list[dict]:
    findings: list[dict] = []
    seen: set[tuple[str, str, str | None]] = set()
    for row in rows:
        key = (str(row["series_id"]), str(row["observation_date"]), row.get("vintage_start"))
        if key in seen:
            findings.append({"severity": "error", "code": "duplicate_observation_vintage", "observation_date": row["observation_date"]})
        seen.add(key)
        if row.get("vintage_start") and row.get("vintage_end") and row["vintage_start"] > row["vintage_end"]:
            findings.append({"severity": "error", "code": "invalid_vintage_interval", "observation_date": row["observation_date"]})
    return findings
