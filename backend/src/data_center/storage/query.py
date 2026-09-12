"""Snapshot-bound DuckDB query interface with stable keyset pagination."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import duckdb

from data_center.catalog.snapshot import Catalog, CatalogSnapshot

DEFAULT_PAGE_SIZE = 1_000
MAX_PAGE_SIZE = 10_000


class QueryValidationError(ValueError):
    """Stable validation category for public query parameters."""


class CursorError(QueryValidationError):
    """Stable category for invalid, mismatched, or expired cursors."""


@dataclass(frozen=True)
class QueryPage:
    rows: list[dict]
    count: int
    schema_versions: list[str]
    snapshot_id: str
    next_cursor: str | None
    rows_scanned: int
    warning: dict | None = None


class QueryMetrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.queries = 0
        self.rows_scanned = 0
        self.rows_returned = 0
        self.rejected_oversized = 0
        self.durations: list[float] = []

    def record(self, *, duration: float, rows_scanned: int, rows_returned: int) -> None:
        with self._lock:
            self.queries += 1
            self.rows_scanned += rows_scanned
            self.rows_returned += rows_returned
            self.durations.append(duration)

    def reject_oversized(self) -> None:
        with self._lock:
            self.rejected_oversized += 1

    def snapshot(self, catalog: Catalog) -> dict:
        with self._lock:
            durations = list(self.durations)
            return {
                "queries_total": self.queries,
                "rows_scanned_total": self.rows_scanned,
                "rows_returned_total": self.rows_returned,
                "rejected_oversized_query_total": self.rejected_oversized,
                "duration_seconds": {
                    "count": len(durations),
                    "sum": sum(durations),
                    "max": max(durations, default=0.0),
                    "mean": sum(durations) / len(durations) if durations else None,
                },
                "catalog_snapshot_refresh_total": catalog.refresh_count,
                "catalog_cache_hit_total": catalog.cache_hits,
                "catalog_cache_miss_total": catalog.cache_misses,
            }


class QueryEngine:
    def __init__(self, root: Path, *, cursor_secret: str | None = None,
                 cursor_ttl_seconds: float = 3600.0):
        self.root = Path(root)
        self.catalog = Catalog(self.root, snapshot_ttl_seconds=cursor_ttl_seconds)
        secret = cursor_secret or f"market-data-center:{self.root.resolve()}"
        self._cursor_secret = hashlib.sha256(secret.encode()).digest()
        self.cursor_ttl_seconds = cursor_ttl_seconds
        self.metrics = QueryMetrics()

    def _encode_cursor(self, payload: dict) -> str:
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        signature = hmac.new(self._cursor_secret, raw, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(raw + signature).decode().rstrip("=")

    def _decode_cursor(self, cursor: str) -> dict:
        try:
            raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
            if base64.urlsafe_b64encode(raw).decode().rstrip("=") != cursor:
                raise CursorError("cursor is invalid or has been tampered with")
            message, signature = raw[:-32], raw[-32:]
            expected = hmac.new(self._cursor_secret, message, hashlib.sha256).digest()
            if len(signature) != 32 or not hmac.compare_digest(signature, expected):
                raise CursorError("cursor is invalid or has been tampered with")
            payload = json.loads(message)
        except CursorError:
            raise
        except Exception as exc:
            raise CursorError("cursor is invalid or has been tampered with") from exc
        if payload.get("expires_at", 0) < time.time():
            raise CursorError("cursor has expired")
        return payload

    def _resolve_snapshot(self, dataset_id: str, selector: dict[str, str],
                          cursor_payload: dict | None) -> CatalogSnapshot:
        if cursor_payload is None:
            return self.catalog.resolve(dataset_id, selector)
        snapshot = self.catalog.get(cursor_payload["snapshot_id"])
        if snapshot is None:
            raise CursorError("cursor snapshot has expired")
        return snapshot

    def _validate_page_size(self, page_size: int | None, *, explicit: bool) -> int | None:
        if not explicit:
            return None
        effective = DEFAULT_PAGE_SIZE if page_size is None else page_size
        if effective < 1 or effective > MAX_PAGE_SIZE:
            if effective > MAX_PAGE_SIZE:
                self.metrics.reject_oversized()
            raise QueryValidationError(f"page_size must be between 1 and {MAX_PAGE_SIZE}")
        return effective

    @staticmethod
    def _cursor_context(*, dataset_id: str, selector: dict[str, str], start: str | None,
                        end: str | None, mode: str, asof_ts: str | None,
                        sort_keys: list[str]) -> dict:
        return {"dataset": dataset_id, "selector": selector, "start": start, "end": end,
                "mode": mode, "asof_ts": asof_ts, "sort_keys": sort_keys}

    @staticmethod
    def _cursor_placeholder(key: str) -> str:
        return "cast(? as timestamptz)" if key == "bar_ts" else "?"

    @staticmethod
    def _after_clause(sort_keys: list[str]) -> str:
        if len(sort_keys) == 1:
            return f"{sort_keys[0]} > {QueryEngine._cursor_placeholder(sort_keys[0])}"
        columns = ", ".join(sort_keys)
        placeholders = ", ".join(QueryEngine._cursor_placeholder(key) for key in sort_keys)
        return f"({columns}) > ({placeholders})"

    @staticmethod
    def _through_clause(sort_keys: list[str]) -> str:
        if len(sort_keys) == 1:
            return f"{sort_keys[0]} <= {QueryEngine._cursor_placeholder(sort_keys[0])}"
        columns = ", ".join(sort_keys)
        placeholders = ", ".join(QueryEngine._cursor_placeholder(key) for key in sort_keys)
        return f"({columns}) <= ({placeholders})"

    @staticmethod
    def _execute(snapshot: CatalogSnapshot, *, partition_columns: list[str], order_columns: str,
                 sort_keys: list[str], scan_columns: list[str], clauses: list[str], params: list,
                 page_size: int | None, last_key: list[str] | None,
                 bars: bool = False) -> tuple[list[dict], bool, int]:
        if not snapshot.parts:
            return [], False, 0
        projection = ("* replace (cast(bar_ts as timestamptz) as bar_ts, "
                      "cast(ingest_ts as timestamptz) as ingest_ts)") if bars else "*"
        order_by = ", ".join(sort_keys)
        key_clauses = list(clauses)
        key_params = list(params)
        if last_key is not None:
            key_clauses.append(QueryEngine._after_clause(sort_keys))
            key_params.extend(last_key)
        where = "where " + " and ".join(key_clauses) if key_clauses else ""
        limit = f"limit {page_size + 1}" if page_size is not None else ""
        if page_size is None:
            query = f"""
                with source as (
                    select {projection} from read_parquet(?, union_by_name=true, hive_partitioning=false)
                ), ranked as (
                    select *, row_number() over (
                        partition by {', '.join(partition_columns)} order by {order_columns}
                    ) as _rn
                    from source {where}
                )
                select * exclude (_rn), count(*) over () as _matched_count
                from ranked where _rn = 1 order by {order_by} asc
            """
            with duckdb.connect() as connection:
                connection.execute("set timezone='UTC'")
                result = connection.execute(query, [[str(part.path) for part in snapshot.parts], *key_params])
                columns = [column[0] for column in result.description]
                rows = [dict(zip(columns, row)) for row in result.fetchall()]
            matched_count = rows[0].pop("_matched_count") if rows else 0
            for row in rows[1:]:
                row.pop("_matched_count")
            return rows, False, matched_count

        key_names = list(dict.fromkeys([*scan_columns, *partition_columns, *sort_keys]))
        key_projection = ", ".join(
            f"cast({column} as timestamptz) as {column}" if bars and column in {"bar_ts", "ingest_ts"}
            else column
            for column in key_names
        )
        key_query = f"""
            with source as (
                select {key_projection} from read_parquet(?, union_by_name=true, hive_partitioning=false)
            ), ranked as (
                select {key_projection}, row_number() over (
                    partition by {', '.join(partition_columns)} order by {order_columns}
                ) as _rn
                from source {where}
            )
            select {order_by}, count(*) over () as _matched_count
            from ranked where _rn = 1 order by {order_by} asc {limit}
        """
        with duckdb.connect() as connection:
            connection.execute("set timezone='UTC'")
            key_result = connection.execute(
                key_query, [[str(part.path) for part in snapshot.parts], *key_params]
            )
            key_columns = [column[0] for column in key_result.description]
            keys = [dict(zip(key_columns, row)) for row in key_result.fetchall()]
        matched_count = keys[0]["_matched_count"] if keys else 0
        has_more = len(keys) > page_size
        selected_keys = keys[:page_size]
        if not selected_keys:
            return [], False, matched_count
        upper_key = [selected_keys[-1][key] for key in sort_keys]
        row_clauses = list(key_clauses)
        row_params = list(key_params)
        row_clauses.append(QueryEngine._through_clause(sort_keys))
        row_params.extend(upper_key)
        row_where = "where " + " and ".join(row_clauses)
        row_query = f"""
            with source as (
                select {projection} from read_parquet(?, union_by_name=true, hive_partitioning=false)
            ), ranked as (
                select *, row_number() over (
                    partition by {', '.join(partition_columns)} order by {order_columns}
                ) as _rn
                from source {row_where}
            )
            select * exclude (_rn) from ranked where _rn = 1 order by {order_by} asc limit {page_size}
        """
        with duckdb.connect() as connection:
            connection.execute("set timezone='UTC'")
            result = connection.execute(row_query, [[str(part.path) for part in snapshot.parts], *row_params])
            columns = [column[0] for column in result.description]
            rows = [dict(zip(columns, row)) for row in result.fetchall()]
        return rows, has_more, matched_count

    def _query_page(self, *, dataset_id: str, selector: dict[str, str], start: str | None,
                    end: str | None, mode: str, asof_ts: str | None, page_size: int | None,
                    cursor: str | None, sort_keys: list[str], partition_columns: list[str],
                    order_columns: str, scan_columns: list[str], clauses: list[str], params: list,
                    bars: bool = False) -> QueryPage:
        started = time.monotonic()
        explicit = page_size is not None or cursor is not None
        effective_size = self._validate_page_size(page_size, explicit=explicit)
        context = self._cursor_context(dataset_id=dataset_id, selector=selector, start=start, end=end,
                                       mode=mode, asof_ts=asof_ts, sort_keys=sort_keys)
        payload = self._decode_cursor(cursor) if cursor else None
        if payload is not None and payload.get("context") != context:
            raise CursorError("cursor does not match query parameters")
        snapshot = self._resolve_snapshot(dataset_id, selector, payload)
        last_key = payload.get("last_key") if payload else None
        rows, has_more, rows_scanned = self._execute(
            snapshot, partition_columns=partition_columns, order_columns=order_columns,
            sort_keys=sort_keys, scan_columns=scan_columns, clauses=clauses, params=params,
            page_size=effective_size,
            last_key=last_key, bars=bars,
        )
        next_cursor = None
        if has_more:
            next_cursor = self._encode_cursor({
                "context": context,
                "snapshot_id": snapshot.snapshot_id,
                "last_key": [str(rows[-1][key]) for key in sort_keys],
                "expires_at": time.time() + self.cursor_ttl_seconds,
            })
        warning = None
        if not explicit and len(rows) > DEFAULT_PAGE_SIZE:
            warning = {"code": "unbounded_query", "message": "use page_size and cursor for bounded reads"}
        self.metrics.record(duration=time.monotonic() - started, rows_scanned=rows_scanned,
                            rows_returned=len(rows))
        return QueryPage(rows=rows, count=len(rows), schema_versions=list(snapshot.schema_versions),
                         snapshot_id=snapshot.snapshot_id, next_cursor=next_cursor,
                         rows_scanned=rows_scanned, warning=warning)

    def provider_bars_page(self, *, symbol: str, timeframe: str, provider: str,
                           start: datetime | None = None, end: datetime | None = None,
                           page_size: int | None = None, cursor: str | None = None) -> QueryPage:
        clauses, params = [], []
        if start is not None:
            clauses.append("bar_ts >= ?")
            params.append(start)
        if end is not None:
            clauses.append("bar_ts < ?")
            params.append(end)
        return self._query_page(
            dataset_id="provider_bars", selector={"provider": provider, "symbol": symbol, "timeframe": timeframe},
            start=start.isoformat() if start else None, end=end.isoformat() if end else None,
            mode="current", asof_ts=None, page_size=page_size, cursor=cursor,
            sort_keys=["bar_ts"],
            partition_columns=["provider", "symbol", "asset_class", "timeframe", "bar_ts"],
            order_columns="ingest_ts desc", scan_columns=["ingest_ts"], clauses=clauses,
            params=params, bars=True,
        )

    def market_bars_page(self, *, symbol: str, timeframe: str, provider: str, price_basis: str,
                         recipe_id: str, recipe_version: str, start: datetime | None = None,
                         end: datetime | None = None, page_size: int | None = None,
                         cursor: str | None = None) -> QueryPage:
        clauses = ["recipe_id = ?", "recipe_version = ?"]
        params: list = [recipe_id, recipe_version]
        if start is not None:
            clauses.append("bar_ts >= ?")
            params.append(start)
        if end is not None:
            clauses.append("bar_ts < ?")
            params.append(end)
        return self._query_page(
            dataset_id="market_bars",
            selector={"provider": provider, "symbol": symbol, "timeframe": timeframe,
                      "price_basis": price_basis},
            start=start.isoformat() if start else None, end=end.isoformat() if end else None,
            mode=f"recipe:{recipe_id}@{recipe_version}", asof_ts=None,
            page_size=page_size, cursor=cursor, sort_keys=["bar_ts"],
            partition_columns=["provider", "symbol", "asset_class", "timeframe", "bar_ts",
                               "price_basis", "session_profile", "recipe_id", "recipe_version"],
            order_columns="ingest_ts desc", scan_columns=["ingest_ts", "recipe_id", "recipe_version"],
            clauses=clauses, params=params, bars=True,
        )

    def economic_observations_page(self, *, provider: str, series_id: str, start: str | None = None,
                                   end: str | None = None, asof_ts: str | None = None,
                                   mode: str = "current", page_size: int | None = None,
                                   cursor: str | None = None) -> QueryPage:
        if mode not in {"current", "pit"}:
            raise QueryValidationError("economic query mode must be current or pit")
        if mode == "current" and asof_ts is not None:
            mode = "pit"
        if mode == "pit" and not asof_ts:
            raise QueryValidationError("pit query requires asof_ts")
        clauses, params = [], []
        if start is not None:
            clauses.append("observation_date >= ?")
            params.append(start)
        if end is not None:
            clauses.append("observation_date <= ?")
            params.append(end)
        if mode == "pit":
            clauses.append(
                "cast(asof_ts as timestamptz) <= cast(? as timestamptz) and "
                "((availability_policy = 'realtime_vintage' and cast(vintage_start as date) + "
                "coalesce(availability_lag_days, 0) * interval '1 day' <= cast(? as date)) or "
                "(availability_policy = 'release_date_known' and cast(release_ts as timestamptz) + "
                "coalesce(availability_lag_days, 0) * interval '1 day' <= cast(? as timestamptz)))"
            )
            params.extend([asof_ts, asof_ts[:10], asof_ts])
            order = ("case when availability_policy = 'realtime_vintage' then vintage_start "
                     "else release_ts end desc, asof_ts desc")
        else:
            order = ("case when vintage_start is null then 0 else 1 end desc, "
                     "vintage_start desc nulls last, asof_ts desc")
        return self._query_page(
            dataset_id="economic_observations", selector={"provider": provider, "series_id": series_id},
            start=start, end=end, mode=mode, asof_ts=asof_ts, page_size=page_size, cursor=cursor,
            sort_keys=["observation_date"], partition_columns=["observation_date"], order_columns=order,
            scan_columns=["availability_policy", "availability_lag_days", "vintage_start",
                          "release_ts", "asof_ts"],
            clauses=clauses, params=params,
        )


_engines: dict[Path, QueryEngine] = {}


def get_query_engine(root: Path) -> QueryEngine:
    key = Path(root).resolve()
    engine = _engines.get(key)
    if engine is None:
        engine = QueryEngine(key)
        _engines[key] = engine
    return engine


def query_provider_bars(root: Path, *, symbol: str, timeframe: str, provider: str,
                        start: datetime | None = None, end: datetime | None = None) -> list[dict]:
    # Preserve the helper's historical point-query behavior while the HTTP/page contract is half-open.
    inclusive_end = end + timedelta(microseconds=1) if end is not None else None
    return get_query_engine(root).provider_bars_page(provider=provider, symbol=symbol, timeframe=timeframe,
                                                     start=start, end=inclusive_end).rows


def query_market_bars(root: Path, *, symbol: str, timeframe: str, provider: str, price_basis: str,
                      recipe_id: str, recipe_version: str, start: datetime | None = None,
                      end: datetime | None = None) -> list[dict]:
    inclusive_end = end + timedelta(microseconds=1) if end is not None else None
    return get_query_engine(root).market_bars_page(
        provider=provider, symbol=symbol, timeframe=timeframe, price_basis=price_basis,
        recipe_id=recipe_id, recipe_version=recipe_version, start=start, end=inclusive_end,
    ).rows


def query_economic_observations(root: Path, *, provider: str, series_id: str, start: str | None = None,
                                end: str | None = None, asof_ts: str | None = None,
                                mode: str = "current") -> list[dict]:
    return get_query_engine(root).economic_observations_page(provider=provider, series_id=series_id,
                                                             start=start, end=end, asof_ts=asof_ts,
                                                             mode=mode).rows


def provider_bars_coverage(root: Path, *, provider: str, symbol: str, timeframe: str) -> dict:
    rows = query_provider_bars(root, provider=provider, symbol=symbol, timeframe=timeframe)
    return {"dataset_id": "provider_bars", "provider": provider, "symbol": symbol, "timeframe": timeframe,
            "row_count": len(rows), "min_ts": rows[0]["bar_ts"] if rows else None,
            "max_ts": rows[-1]["bar_ts"] if rows else None}


def economic_observations_coverage(root: Path, *, provider: str, series_id: str) -> dict:
    rows = query_economic_observations(root, provider=provider, series_id=series_id)
    return {"dataset_id": "economic_observations", "provider": provider, "series_id": series_id,
            "row_count": len(rows), "min_date": rows[0]["observation_date"] if rows else None,
            "max_date": rows[-1]["observation_date"] if rows else None}
