"""Read models for the operations workbench.

Runs, findings and queue state are projected here so the presentation contract
(stage, degraded reasons, retry chain, opaque cursors) lives in one place
instead of being re-derived by every route.  Nothing in this module writes to
the ledger: a terminal run stays exactly as published.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from data_center.catalog.manifest import manifest_path

from .instants import parse_instant

TERMINAL_STATUSES = ("pass", "failed", "dead_letter")
DEFAULT_RUN_PAGE_SIZE = 50
MAX_RUN_PAGE_SIZE = 500
DEFAULT_FINDING_PAGE_SIZE = 100
MAX_FINDING_PAGE_SIZE = 1000
UNBOUNDED_WARNING_ROWS = 1000
# Half-open boundary for a run's planned window, expressed as a time range so
# every consumer reads the same semantics the control plane planned.
WINDOW_SEMANTICS = "half-open"


class RunValidationError(ValueError):
    """Raised when a run/finding filter cannot be served as requested."""


class RunCursorError(RunValidationError):
    """Raised when an opaque run cursor cannot be trusted or has expired."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _timestamp(value: str | None) -> str:
    """Normalize a timestamp for comparison and display; unknown values sort last."""
    if not value:
        return ""
    return str(value)


def _parse_boundary(value: str | None, field: str) -> str:
    if value is None:
        return ""
    try:
        parsed = parse_instant(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RunValidationError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


class RunView:
    """Projection of ledger runs, findings and audit state for the console."""

    def __init__(self, ledger, *, canonical_root: Path | None = None, cursor_secret: str | None = None,
                 cursor_ttl_seconds: float = 3600.0):
        self.ledger = ledger
        self.canonical_root = Path(canonical_root) if canonical_root is not None else None
        secret = cursor_secret or f"market-data-center-runs:{ledger.path}"
        self._cursor_secret = hashlib.sha256(secret.encode()).digest()
        self.cursor_ttl_seconds = cursor_ttl_seconds

    # -- cursors ---------------------------------------------------------
    def _encode_cursor(self, payload: dict) -> str:
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        signature = hmac.new(self._cursor_secret, raw, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(raw + signature).decode().rstrip("=")

    def _decode_cursor(self, cursor: str) -> dict:
        try:
            raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
            if base64.urlsafe_b64encode(raw).decode().rstrip("=") != cursor:
                raise RunCursorError("cursor is invalid or has been tampered with")
            message, signature = raw[:-32], raw[-32:]
            expected = hmac.new(self._cursor_secret, message, hashlib.sha256).digest()
            if len(signature) != 32 or not hmac.compare_digest(signature, expected):
                raise RunCursorError("cursor is invalid or has been tampered with")
            payload = json.loads(message)
        except RunCursorError:
            raise
        except Exception as exc:  # any malformed cursor is a client error
            raise RunCursorError("cursor is invalid or has been tampered with") from exc
        if payload.get("expires_at", 0) < time.time():
            raise RunCursorError("cursor has expired")
        return payload

    @staticmethod
    def _page_size(page_size: int | None, *, default: int, maximum: int) -> int:
        effective = default if page_size is None else page_size
        if effective < 1 or effective > maximum:
            raise RunValidationError(f"page_size must be between 1 and {maximum}")
        return effective

    # -- runs ------------------------------------------------------------
    @staticmethod
    def _run_filters(*, status: str | None, dataset_id: str | None, run_kind: str | None,
                     run_scope: str | None, provider: str | None, symbol: str | None,
                     created_from: str | None, created_to: str | None) -> dict:
        return {
            "status": status or None, "dataset_id": dataset_id or None, "run_kind": run_kind or None,
            "run_scope": run_scope or None, "provider": provider or None, "symbol": symbol or None,
            "created_from": _parse_boundary(created_from, "created_from") or None,
            "created_to": _parse_boundary(created_to, "created_to") or None,
        }

    @staticmethod
    def _matches(run: dict, filters: dict) -> bool:
        for field in ("status", "dataset_id", "run_kind", "run_scope", "provider", "symbol"):
            expected = filters.get(field)
            if expected and run.get(field) != expected:
                return False
        created = _timestamp(run.get("created_at"))
        if filters.get("created_from") and created < filters["created_from"]:
            return False
        return not (filters.get("created_to") and created > filters["created_to"])

    def list_runs(self, *, status: str | None = None, dataset_id: str | None = None,
                  run_kind: str | None = None, run_scope: str | None = None,
                  provider: str | None = None, symbol: str | None = None,
                  created_from: str | None = None, created_to: str | None = None,
                  page_size: int | None = None, cursor: str | None = None) -> dict:
        """Return one page of runs ordered newest first.

        Without pagination parameters the legacy complete-result behavior is
        preserved (with an ``unbounded_query`` warning once the result is large).
        When a page size or cursor is supplied the cursor binds the exact filter
        set and the last row's sort keys, so a consumer never needs to know an
        offset and a changed filter cannot silently continue an unrelated page.
        """
        filters = self._run_filters(status=status, dataset_id=dataset_id, run_kind=run_kind,
                                    run_scope=run_scope, provider=provider, symbol=symbol,
                                    created_from=created_from, created_to=created_to)
        explicit = page_size is not None or cursor is not None
        limit = self._page_size(page_size, default=DEFAULT_RUN_PAGE_SIZE, maximum=MAX_RUN_PAGE_SIZE) if explicit else None
        cursor_payload = self._decode_cursor(cursor) if cursor else None
        if cursor_payload is not None and cursor_payload.get("filters") != filters:
            raise RunCursorError("cursor does not match the requested filters")
        universe = self.ledger.list()
        rows = [run for run in universe if self._matches(run, filters)]
        rows.sort(key=lambda run: (_timestamp(run.get("created_at")), str(run.get("run_id"))), reverse=True)
        if cursor_payload is not None:
            anchor = (cursor_payload.get("created_at", ""), cursor_payload.get("run_id", ""))
            rows = [run for run in rows
                    if (_timestamp(run.get("created_at")), str(run.get("run_id"))) < anchor]
        page = rows if limit is None else rows[:limit]
        # ``rows`` is the filtered universe, so retry chains stay correct even
        # when the ancestor is older than the page being displayed.
        findings_by_run = self.findings_index()
        next_cursor = None
        if limit is not None and len(rows) > limit and page:
            last = page[-1]
            next_cursor = self._encode_cursor({
                "filters": filters, "created_at": _timestamp(last.get("created_at")),
                "run_id": str(last.get("run_id")), "expires_at": time.time() + self.cursor_ttl_seconds,
            })
        projections = [self.project(run, all_runs=universe, findings_by_run=findings_by_run) for run in page]
        warnings: list[str] = []
        if limit is None and len(projections) > UNBOUNDED_WARNING_ROWS:
            warnings.append("unbounded_query")
        return {"runs": projections, "page": {"count": len(projections), "next_cursor": next_cursor,
                                              "page_size": limit, "paginated": limit is not None,
                                              "has_more": next_cursor is not None,
                                              "order": "created_at desc, run_id desc"},
                "filters": filters, "warnings": warnings, "generated_at": _utc_now()}

    def get_run(self, run_id: str, *, all_runs: list[dict] | None = None) -> dict | None:
        try:
            run = self.ledger.get(run_id)
        except KeyError:
            return None
        runs = all_runs if all_runs is not None else self.ledger.list()
        return self.project(run, all_runs=runs, include_findings=True)

    def retry_chain(self, run_id: str, *, all_runs: list[dict] | None = None) -> list[dict]:
        """Return the oldest ancestor followed by every retry that descends from it."""
        runs = all_runs if all_runs is not None else self.ledger.list()
        by_id = {run.get("run_id"): run for run in runs}
        ancestor = run_id
        seen: set[str] = set()
        while ancestor and ancestor in by_id and ancestor not in seen:
            seen.add(ancestor)
            previous = by_id[ancestor].get("retry_of")
            if not previous or previous not in by_id:
                break
            ancestor = previous
        chain: list[dict] = []
        queue = [ancestor] if ancestor and ancestor in by_id else [run_id]
        visited: set[str] = set()
        while queue:
            current = queue.pop(0)
            if current in visited or current not in by_id:
                continue
            visited.add(current)
            run = by_id[current]
            chain.append({"run_id": current, "status": run.get("status"), "created_at": run.get("created_at"),
                          "relation": "origin" if current == ancestor else "retry",
                          "stage": self.stage(run), "retry_of": run.get("retry_of")})
            children = sorted((item for item in runs if item.get("retry_of") == current),
                              key=lambda item: _timestamp(item.get("created_at")))
            queue.extend(str(child.get("run_id")) for child in children)
        return chain

    # -- projections -----------------------------------------------------
    @staticmethod
    def stage(run: dict) -> str:
        status = run.get("status")
        if status == "queued":
            return "queued"
        if status == "running":
            return "running"
        if status == "pass":
            return "published" if run.get("manifest") else "verified"
        failure_stage = run.get("failure_stage")
        return failure_stage or "execute"

    def degraded_reasons(self, run: dict, findings: list[dict]) -> list[dict]:
        """Explain every non-fatal degradation without inventing provider state."""
        reasons: list[dict] = []
        coverage = run.get("coverage") if isinstance(run.get("coverage"), dict) else None
        if coverage and coverage.get("readiness_status") in {"degraded", "not_ready"}:
            reasons.append({
                "code": "coverage_degraded" if coverage.get("readiness_status") == "degraded" else "coverage_not_ready",
                "message": (f"coverage {coverage.get('readiness_status')}: "
                            f"{coverage.get('gap_count', 0)} missing timestamps in the requested range"),
                "source": "coverage",
            })
        summary = run.get("quality_summary") if isinstance(run.get("quality_summary"), dict) else {}
        for finding in (summary.get("findings") or [])[:5]:
            code = finding.get("code")
            if code in {"coverage_not_ready", "provider_gap"}:
                reasons.append({"code": code, "message": finding.get("message") or code, "source": "quality"})
        if run.get("error_type") == "ProviderGapError" or any(
                item.get("error_type") == "ProviderGapError" for item in run.get("attempt_errors") or []):
            reasons.append({"code": "provider_gap", "message": "the provider did not return the requested interval",
                            "source": "provider"})
        for error in (run.get("attempt_errors") or [])[-2:]:
            if error.get("retryable") and run.get("status") == "queued":
                reasons.append({"code": "retrying", "message": error.get("error") or "retry scheduled",
                                "source": "worker"})
        if findings and run.get("status") == "pass":
            reasons.append({"code": "findings_recorded", "message": f"{len(findings)} quality finding(s) recorded",
                            "source": "quality"})
        deduped: list[dict] = []
        seen: set[tuple[str, str]] = set()
        for reason in reasons:
            key = (reason["code"], reason["message"])
            if key not in seen:
                seen.add(key)
                deduped.append(reason)
        return deduped

    @staticmethod
    def outcome(run: dict, reasons: list[dict]) -> str:
        status = run.get("status")
        if status in {"queued", "running", "failed", "dead_letter"}:
            return status
        if status == "pass":
            return "degraded" if reasons else "pass"
        return status or "unknown"

    def manifest_status(self, run: dict) -> str:
        run_kind = run.get("run_kind")
        if run_kind in {"quality", "parity"}:
            return "not_applicable"
        if self.canonical_root is None:
            return "unknown"
        return "published" if manifest_path(self.canonical_root, str(run.get("run_id"))).exists() else "missing"

    @staticmethod
    def window_count(run: dict) -> int:
        plan = run.get("execution_plan") if isinstance(run.get("execution_plan"), dict) else None
        if plan and isinstance(plan.get("windows"), list) and plan["windows"]:
            return len(plan["windows"])
        if isinstance(run.get("windows"), list) and run["windows"]:
            return len(run["windows"])
        return 1

    def selector(self, run: dict) -> dict:
        selector = {key: run.get(key) for key in ("provider", "symbol", "timeframe", "price_basis",
                                                  "recipe_id", "recipe_version", "series_id")}
        return {key: value for key, value in selector.items() if value}

    def findings_index(self) -> dict[str, list[dict]]:
        """Group findings by run once so a page of runs costs one read."""
        index: dict[str, list[dict]] = {}
        for item in self.ledger.findings():
            index.setdefault(str(item.get("run_id")), []).append(item)
        return index

    def project(self, run: dict, *, all_runs: list[dict] | None = None,
                findings_by_run: dict[str, list[dict]] | None = None,
                include_findings: bool = False) -> dict:
        runs = self.ledger.list() if all_runs is None else all_runs
        index = self.findings_index() if findings_by_run is None else findings_by_run
        findings = index.get(str(run.get("run_id")), [])
        reasons = self.degraded_reasons(run, findings)
        summary = run.get("quality_summary") if isinstance(run.get("quality_summary"), dict) else {}
        finding_count = max(len(findings), int(summary.get("finding_count") or 0))
        plan = run.get("execution_plan") if isinstance(run.get("execution_plan"), dict) else None
        time_range = None
        if isinstance(run.get("windows"), list) and run["windows"]:
            time_range = {"start": run["windows"][0].get("start"), "end": run["windows"][-1].get("end"),
                          "semantics": WINDOW_SEMANTICS}
        elif plan and isinstance(plan.get("windows"), list) and plan["windows"]:
            time_range = {"start": plan["windows"][0].get("start"), "end": plan["windows"][-1].get("end"),
                          "semantics": WINDOW_SEMANTICS}
        elif run.get("start") or run.get("end"):
            time_range = {"start": run.get("start"), "end": run.get("end"), "semantics": WINDOW_SEMANTICS}
        projection = {
            **run,
            "stage": self.stage(run),
            "outcome": self.outcome(run, reasons),
            "terminal": run.get("status") in TERMINAL_STATUSES,
            "degraded_reasons": reasons,
            "selector": self.selector(run),
            "time_range": time_range,
            "window_count": self.window_count(run),
            "input_snapshot_id": run.get("input_snapshot_id"),
            "manifest_status": self.manifest_status(run),
            "finding_count": finding_count,
            "retry_chain": self.retry_chain(str(run.get("run_id")), all_runs=runs),
            "dead_letter_state": run.get("dead_letter_state"),
            "attempt_count": run.get("attempt_count"),
            "retry_count": run.get("retry_count", 0),
        }
        if include_findings:
            projection["findings"] = findings
        return projection

    # -- findings --------------------------------------------------------
    def list_findings(self, *, severity: str | None = None, code: str | None = None,
                      dataset_id: str | None = None, run_id: str | None = None,
                      state: str | None = None, series_id: str | None = None,
                      observed_from: str | None = None, observed_to: str | None = None,
                      page_size: int | None = None, cursor: str | None = None) -> dict:
        limit = self._page_size(page_size, default=DEFAULT_FINDING_PAGE_SIZE, maximum=MAX_FINDING_PAGE_SIZE)
        filters = {"severity": severity or None, "code": code or None, "dataset_id": dataset_id or None,
                   "run_id": run_id or None, "state": state or None, "series_id": series_id or None,
                   "observed_from": _parse_boundary(observed_from, "observed_from") or None,
                   "observed_to": _parse_boundary(observed_to, "observed_to") or None}
        cursor_payload = self._decode_cursor(cursor) if cursor else None
        if cursor_payload is not None and cursor_payload.get("filters") != filters:
            raise RunCursorError("cursor does not match the requested filters")

        def observed_at(item: dict) -> str:
            return _timestamp(item.get("bar_ts") or item.get("observation_date") or item.get("observed_at"))

        rows = []
        for item in self.ledger.findings():
            if filters["severity"] and item.get("severity") != filters["severity"]:
                continue
            if filters["code"] and item.get("code") != filters["code"]:
                continue
            if filters["dataset_id"] and item.get("dataset_id") != filters["dataset_id"]:
                continue
            if filters["run_id"] and item.get("run_id") != filters["run_id"]:
                continue
            if filters["series_id"] and item.get("series_id") != filters["series_id"]:
                continue
            if filters["state"] and item.get("state", "open") != filters["state"]:
                continue
            stamp = observed_at(item)
            if filters["observed_from"] and stamp[:10] < filters["observed_from"][:10]:
                continue
            if filters["observed_to"] and stamp[:10] > filters["observed_to"][:10]:
                continue
            rows.append(item)
        rows.sort(key=lambda item: (observed_at(item), str(item.get("finding_id"))), reverse=True)
        if cursor_payload is not None:
            anchor = (cursor_payload.get("observed_at", ""), cursor_payload.get("finding_id", ""))
            rows = [item for item in rows if (observed_at(item), str(item.get("finding_id"))) < anchor]
        page = rows[:limit]
        next_cursor = None
        if len(rows) > limit and page:
            last = page[-1]
            next_cursor = self._encode_cursor({
                "filters": filters, "observed_at": observed_at(last), "finding_id": str(last.get("finding_id")),
                "expires_at": time.time() + self.cursor_ttl_seconds,
            })
        states = self.ledger.finding_states()
        counts: dict[str, int] = {}
        for item in self.ledger.findings():
            key = (states.get(item.get("finding_id")) or {}).get("state", "open")
            counts[key] = counts.get(key, 0) + 1
        return {"findings": page, "page": {"count": len(page), "next_cursor": next_cursor, "page_size": limit,
                                           "has_more": next_cursor is not None,
                                           "order": "observed_at desc, finding_id desc"},
                "filters": filters, "state_counts": counts, "generated_at": _utc_now()}
