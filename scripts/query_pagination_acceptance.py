"""Verify paged/unpaged API and macro-market-lab consumer parity."""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests


def commit(repo: Path) -> str:
    try:
        return subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def digest(rows: list[dict[str, Any]]) -> str:
    encoded = json.dumps(rows, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


class Api:
    def __init__(self, base_url: str, api_key: str | None):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.trust_env = False
        if api_key:
            self.session.headers["X-API-Key"] = api_key

    def envelope(self, path: str, params: dict[str, Any]) -> dict:
        response = self.session.get(f"{self.base_url}/api/v1{path}", params=params, timeout=30)
        response.raise_for_status()
        payload = response.json()
        if payload.get("errors"):
            raise RuntimeError(str(payload["errors"]))
        return payload

    def paged(self, path: str, params: dict[str, Any], page_size: int) -> tuple[list[dict], list[str]]:
        rows, snapshots = [], []
        cursor = None
        while True:
            page_params = {**params, "page_size": page_size}
            if cursor:
                page_params["cursor"] = cursor
            payload = self.envelope(path, page_params)
            rows.extend(payload["data"])
            snapshots.append(payload["meta"]["snapshot_id"])
            cursor = payload["meta"].get("next_cursor")
            if not cursor:
                return rows, snapshots


def summary(rows: list[dict], time_key: str) -> dict:
    return {"row_count": len(rows), "min": min((str(row[time_key]) for row in rows), default=None),
            "max": max((str(row[time_key]) for row in rows), default=None), "hash": digest(rows)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:18380")
    parser.add_argument("--api-key")
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument("--bars-provider", default="yfinance")
    parser.add_argument("--bars-symbol", default="SPY")
    parser.add_argument("--bars-timeframe", default="1d")
    parser.add_argument("--bars-start", default="2026-01-01")
    parser.add_argument("--bars-end", default="2026-09-10")
    parser.add_argument("--economic-provider", default="fred")
    parser.add_argument("--economic-series", default="PAYEMS")
    parser.add_argument("--economic-start", default="2025-01-01")
    parser.add_argument("--economic-end", default="2026-09-10")
    parser.add_argument("--asof-ts", default="2026-09-10T00:00:00Z")
    parser.add_argument("--macro-repo", type=Path, default=Path("/home/quant/repos/macro-market-lab"))
    parser.add_argument("--output", type=Path,
                        default=Path("acceptance-receipts/query-pagination-acceptance.json"))
    args = parser.parse_args()
    api = Api(args.base_url, args.api_key)
    cases = [
        ("bars", "/bars", {"provider": args.bars_provider, "symbol": args.bars_symbol,
                            "timeframe": args.bars_timeframe, "start": args.bars_start,
                            "end": args.bars_end}, "bar_ts"),
        ("economic_current", "/economic/observations",
         {"provider": args.economic_provider, "series_id": args.economic_series,
          "start": args.economic_start, "end": args.economic_end, "mode": "current"},
         "observation_date"),
        ("economic_pit", "/economic/observations",
         {"provider": args.economic_provider, "series_id": args.economic_series,
          "start": args.economic_start, "end": args.economic_end, "mode": "pit",
          "asof_ts": args.asof_ts}, "observation_date"),
    ]
    results = {}
    for name, path, params, time_key in cases:
        unpaged = api.envelope(path, params)["data"]
        paged, snapshots = api.paged(path, params, args.page_size)
        results[name] = {"unpaged": summary(unpaged, time_key), "paged": summary(paged, time_key),
                         "single_snapshot": len(set(snapshots)) == 1, "parity": unpaged == paged}

    sys.path.insert(0, str(args.macro_repo / "src"))
    from macro_market_lab.integrations.data_center import DataCenterReadClient

    consumer = DataCenterReadClient(base_url=args.base_url, api_key=args.api_key)
    consumer_bars = consumer.bars(provider=args.bars_provider, symbol=args.bars_symbol,
                                  timeframe=args.bars_timeframe, start=args.bars_start,
                                  end=args.bars_end, page_size=args.page_size)
    consumer_economic = consumer.economic_observations(
        provider=args.economic_provider, series_id=args.economic_series, start=args.economic_start,
        end=args.economic_end, mode="current", page_size=args.page_size,
    )
    consumer_parity = {
        "bars": digest(consumer_bars) == results["bars"]["paged"]["hash"],
        "economic_current": digest(consumer_economic) == results["economic_current"]["paged"]["hash"],
    }
    checks = [result["parity"] and result["single_snapshot"] and result["paged"]["row_count"] > 0
              for result in results.values()]
    checks.extend(consumer_parity.values())
    report = {
        "status": "pass" if all(checks) else "failed",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "environment": {"data_center_commit": commit(Path(__file__).resolve().parents[1]),
                        "macro_market_lab_commit": commit(args.macro_repo),
                        "host": platform.node(), "python": platform.python_version()},
        "page_size": args.page_size, "cases": results, "consumer_parity": consumer_parity,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True))
    print(json.dumps(report, sort_keys=True))
    raise SystemExit(0 if report["status"] == "pass" else 1)


if __name__ == "__main__":
    main()
