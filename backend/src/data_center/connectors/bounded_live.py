"""Budget guard around the real connector used by an isolated live preview."""
from __future__ import annotations

import fcntl
import json
from datetime import datetime, timezone
from pathlib import Path

from data_center.instants import parse_instant


class BoundedLiveConnector:
    """Refuse provider access outside the preview's explicit scope and budget."""

    def __init__(self, connector, settings):
        self.connector = connector
        self.settings = settings
        self.provider = connector.provider
        self.version = f"bounded-live:{connector.version}"
        self.end_inclusive = getattr(connector, "end_inclusive", False)

    def _canonical_bytes(self) -> int:
        root = Path(self.settings.canonical_root)
        return sum(path.stat().st_size for path in root.rglob("*") if path.is_file())

    def _claim_request(self) -> None:
        path = self.settings.preview_live_budget_path
        if path is None:
            raise ValueError("live preview request budget is not configured")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            handle.seek(0)
            try:
                state = json.load(handle)
            except (json.JSONDecodeError, ValueError):
                state = {"requests_used": 0}
            now = datetime.now(timezone.utc)
            started_at = parse_instant(state.get("started_at")) if state.get("started_at") else now
            if (now - started_at).total_seconds() >= self.settings.preview_live_runtime_budget_seconds:
                raise ValueError("live preview runtime budget exhausted")
            used = int(state.get("requests_used") or 0)
            if used >= self.settings.preview_live_request_budget:
                raise ValueError("live preview request budget exhausted")
            state["requests_used"] = used + 1
            state["started_at"] = started_at.isoformat()
            handle.seek(0)
            handle.truncate()
            json.dump(state, handle, sort_keys=True)
            handle.flush()

    def fetch_bars(self, job):
        if job.provider != "dukascopy" or not self.settings.preview_symbol_allowed(job.symbol):
            raise ValueError("live preview provider or symbol is outside the approved scope")
        if self.settings.preview_live_start is None or self.settings.preview_live_end is None:
            raise ValueError("live preview bounds are not configured")
        lower = parse_instant(self.settings.preview_live_start)
        upper = parse_instant(self.settings.preview_live_end)
        if job.start < lower or job.end > upper:
            raise ValueError("live preview request is outside the approved UTC window")
        if self._canonical_bytes() >= self.settings.preview_live_byte_budget:
            raise ValueError("live preview disk budget exhausted")
        self._claim_request()
        return self.connector.fetch_bars(job)
