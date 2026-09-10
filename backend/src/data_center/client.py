from typing import Any

import requests


class DataCenterClient:
    """Small HTTP client for consumers outside the data-center process."""

    def __init__(self, base_url: str = "http://127.0.0.1:18380", api_key: str | None = None, timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    def _request(self, method: str, path: str, **kwargs: Any) -> dict:
        headers = dict(kwargs.pop("headers", {}))
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        response = requests.request(method, f"{self.base_url}/api/v1{path}", headers=headers, timeout=self.timeout, **kwargs)
        response.raise_for_status()
        return response.json()

    def health(self) -> dict:
        return self._request("GET", "/health")

    def datasets(self) -> list[dict]:
        return self._request("GET", "/datasets")["data"]

    def _paged(self, path: str, params: dict, *, page_size: int = 1_000) -> list[dict]:
        rows: list[dict] = []
        cursor = None
        while True:
            page_params = {**params, "page_size": page_size}
            if cursor is not None:
                page_params["cursor"] = cursor
            envelope = self._request("GET", path, params=page_params)
            rows.extend(envelope["data"])
            cursor = envelope["meta"].get("next_cursor")
            if not cursor:
                return rows

    def bars(self, *, provider: str, symbol: str, timeframe: str = "1d", start: str | None = None,
             end: str | None = None, page_size: int = 1_000) -> list[dict]:
        params = {key: value for key, value in {"provider": provider, "symbol": symbol, "timeframe": timeframe, "start": start, "end": end}.items() if value is not None}
        return self._paged("/bars", params, page_size=page_size)

    def economic_observations(self, *, series_id: str, provider: str = "fred", start: str | None = None,
                              end: str | None = None, asof_ts: str | None = None,
                              mode: str = "current", page_size: int = 1_000) -> list[dict]:
        params = {key: value for key, value in {"provider": provider, "series_id": series_id, "start": start,
                                                 "end": end, "asof_ts": asof_ts, "mode": mode}.items() if value is not None}
        return self._paged("/economic/observations", params, page_size=page_size)
