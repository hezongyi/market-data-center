import hmac
import json
import math
import sqlite3
import tempfile
import time
from contextvars import ContextVar
from datetime import timedelta
from uuid import uuid4

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from data_center import __version__
from data_center.capacity import CapacityProtectedError
from data_center.catalog.manifest import (
    PublicationError,
    manifest_path,
    validate_manifest,
)
from data_center.catalog.registry import iter_dataset_definitions
from data_center.catalog.snapshot import selector_hash
from data_center.deployment import validated_runtime_identity
from data_center.domain.models import DeriveJob, IngestJob
from data_center.ingest.worker import LocalWorker
from data_center.observability import run_metrics
from data_center.platform import coverage_for_rows, enqueue_ingest_plan
from data_center.platform_registry import REGISTRY
from data_center.quality.checks import check_provider_bars
from data_center.runs.ledger import RunLedger
from data_center.settings import Settings
from data_center.snapshot import ReceiptIndex, build_snapshot
from data_center.storage.query import (
    CursorError,
    QueryEngine,
    QueryValidationError,
    economic_observations_coverage,
    provider_bars_coverage,
    query_provider_bars,
)

_request_id = ContextVar("request_id", default="")


def current_request_id():
    return _request_id.get()


def create_app(settings: Settings | None = None) -> FastAPI:
    config = settings or Settings()
    app = FastAPI(title=config.app_name, version=__version__)
    ledger = RunLedger(config.ledger_path)
    query_engine = QueryEngine(config.canonical_root)
    capacity_policy = config.capacity_policy()
    receipt_index = ReceiptIndex(config.evidence_root)
    identity = validated_runtime_identity(
        config.deployment_manifest, config.evidence_root, component="api", webui_dist=config.webui_dist,
    ) if config.deployment_manifest else {
        "deployment_id": "development", "software_version": __version__, "source_commit": "unknown"
    }
    print(json.dumps({"event": "api_started", "request_id": None,
                      **{key: identity[key] for key in ("deployment_id", "software_version", "source_commit")}}),
          flush=True)

    @app.middleware("http")
    async def audit_request(request: Request, call_next):
        request_id = request.headers.get("x-request-id", str(uuid4()))[:128]
        token = _request_id.set(request_id)
        started = time.monotonic()
        try:
            response = await call_next(request)
        finally:
            _request_id.reset(token)
        response.headers["X-Request-ID"] = request_id
        print(json.dumps({"event": "http_request", "request_id": request_id, "method": request.method,
                          "path": request.url.path, "status": response.status_code,
                          "duration_seconds": round(time.monotonic() - started, 4)}), flush=True)
        return response

    @app.exception_handler(PublicationError)
    async def publication_error_handler(request: Request, exc: PublicationError):
        return JSONResponse(status_code=503, content={"data": None,
            "meta": {"request_id": current_request_id(), "schema_version": "v1"},
            "errors": [{"code": "integrity_error", "message": "published data integrity check failed"}]})

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "data": None,
                "meta": {"request_id": current_request_id(), "schema_version": "v1"},
                "errors": [{"code": str(exc.status_code), "message": str(exc.detail)}],
            },
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "data": None,
                "meta": {"request_id": current_request_id(), "schema_version": "v1"},
                "errors": [{"code": "validation_error", "message": "invalid request field"} for error in exc.errors()],
            },
        )

    @app.exception_handler(QueryValidationError)
    async def query_validation_error_handler(request: Request, exc: QueryValidationError) -> JSONResponse:
        code = "cursor_error" if isinstance(exc, CursorError) else "query_validation_error"
        return JSONResponse(
            status_code=422,
            content={
                "data": None,
                "meta": {"request_id": current_request_id(), "schema_version": "v1"},
                "errors": [{"code": code, "message": str(exc)}],
            },
        )

    @app.exception_handler(ValueError)
    async def value_error_handler(request: Request, exc: ValueError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={"data": None,
                     "meta": {"request_id": current_request_id(), "schema_version": "v1"},
                     "errors": [{"code": "invalid_request", "message": str(exc)}]},
        )

    @app.exception_handler(CapacityProtectedError)
    async def capacity_protected_handler(request: Request, exc: CapacityProtectedError) -> JSONResponse:
        return JSONResponse(
            status_code=507,
            content={
                "data": {"write_status": "protected", "capacity": exc.snapshot.as_dict()},
                "meta": {"request_id": current_request_id(), "schema_version": "v1"},
                "errors": [{"code": "capacity_protected", "message": str(exc)}],
            },
        )

    @app.get(f"{config.api_prefix}/health")
    def health(request: Request) -> dict:
        return {
            "data": {"status": "ok"},
            "meta": {
                "request_id": current_request_id(),
                "schema_version": "v1",
            },
            "errors": [],
        }

    @app.get(f"{config.api_prefix}/health/live")
    def health_live(request: Request) -> dict:
        return {"data": {"status": "ok"}, "meta": {"request_id": current_request_id(), "schema_version": "v1"}, "errors": []}

    @app.get(f"{config.api_prefix}/health/ready")
    def health_ready(request: Request) -> JSONResponse:
        storage_ready = False
        try:
            config.canonical_root.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryFile(dir=config.canonical_root) as probe:
                probe.write(b"ready")
                probe.flush()
            with sqlite3.connect(config.ledger_path, timeout=1) as conn:
                conn.execute("begin immediate")
                conn.execute("update worker_heartbeat set heartbeat=heartbeat where id=1")
                conn.rollback()
            storage_ready = True
            snapshot = build_snapshot(
                ledger, capacity_policy=capacity_policy, canonical_root=config.canonical_root,
                receipt_index=receipt_index, backup_root=config.backup_root,
                restore_staging_root=config.restore_staging_root, evidence_root=config.evidence_root,
            )
            age = snapshot.metrics["worker_heartbeat_age_seconds"]
            ready = age is not None and age < 60 and snapshot.status == "fresh"
            capacity = snapshot.capacity
            status_code = 200 if ready else 503
            status = "ready" if ready else "not_ready"
        except (OSError, sqlite3.Error):
            status_code, status, age, capacity, ready, snapshot = 503, "not_ready", None, None, False, None
        return JSONResponse(status_code=status_code, content={"data": {
            "status": status, "read_status": "available" if storage_ready else "unavailable",
            "write_status": "protected" if capacity and capacity["status"] == "critical" else "available",
            "capacity_status": capacity["status"] if capacity else "unknown",
            "operational_snapshot_status": snapshot.status if snapshot else "unknown",
            "worker_heartbeat_age_seconds": age,
            "software_version": identity["software_version"], "source_commit": identity["source_commit"],
            "deployment_id": identity["deployment_id"],
        }, "meta": {"request_id": current_request_id(), "schema_version": "v1"}, "errors": []})

    @app.get(f"{config.api_prefix}/metrics")
    def metrics() -> dict:
        payload = run_metrics(
            ledger, canonical_root=config.canonical_root, evidence_root=config.evidence_root,
            backup_root=config.backup_root, capacity_policy=capacity_policy,
            receipt_index=receipt_index, restore_staging_root=config.restore_staging_root,
        )
        payload.update({key: identity[key] for key in ("deployment_id", "software_version", "source_commit")})
        payload["query"] = query_engine.metrics.snapshot(query_engine.catalog)
        return {"data": payload, "meta": {"request_id": current_request_id(), "schema_version": "v1"}, "errors": []}

    @app.get(f"{config.api_prefix}/runs")
    def runs(request: Request, status: str | None = None) -> dict:
        rows = ledger.list()
        return {"data": [row for row in rows if status is None or row.get("status") == status], "meta": {"request_id": current_request_id(), "schema_version": "v1"}, "errors": []}

    @app.post(f"{config.api_prefix}/runs/{{run_id}}/retry", status_code=202)
    def retry(run_id: str, x_api_key: str | None = Header(default=None)) -> dict:
        if config.api_key and not hmac.compare_digest(x_api_key or "", config.api_key):
            raise HTTPException(status_code=401, detail="invalid api key")
        capacity_policy.require_ingest_capacity(config.canonical_root)
        try:
            new_id = ledger.retry_run(run_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="run not found")
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        return {"data": ledger.get(new_id), "meta": {"request_id": current_request_id(), "schema_version": "v1"}, "errors": []}

    @app.post(f"{config.api_prefix}/runs/{{run_id}}/acknowledge")
    def acknowledge_dead_letter(run_id: str, x_api_key: str | None = Header(default=None)) -> dict:
        if config.api_key and not hmac.compare_digest(x_api_key or "", config.api_key):
            raise HTTPException(status_code=401, detail="invalid api key")
        try:
            payload = ledger.acknowledge_dead_letter(run_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="run not found")
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        return {"data": payload, "meta": {"request_id": current_request_id(), "schema_version": "v1"}, "errors": []}

    @app.get(f"{config.api_prefix}/datasets")
    def datasets() -> dict:
        return {"data": [definition.as_dict() for definition in iter_dataset_definitions()],
                "meta": {"request_id": current_request_id(), "schema_version": "v1"}, "errors": []}

    @app.get(f"{config.api_prefix}/runs/{{run_id}}")
    def run(run_id: str) -> dict:
        from fastapi import HTTPException
        try:
            payload = ledger.get(run_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="run not found")
        return {"data": payload, "meta": {"request_id": current_request_id(), "schema_version": "v1"}, "errors": []}

    @app.get(f"{config.api_prefix}/runs/{{run_id}}/manifest")
    def run_manifest(run_id: str) -> dict:
        try:
            payload = json.loads(manifest_path(config.canonical_root, run_id).read_text())
            validate_manifest(config.canonical_root, payload)
        except (OSError, json.JSONDecodeError, PublicationError):
            raise HTTPException(status_code=404, detail="manifest not found")
        return {"data": payload, "meta": {"request_id": current_request_id(), "schema_version": "v1"}, "errors": []}

    @app.post(f"{config.api_prefix}/ingest/runs")
    def ingest(job: IngestJob, x_api_key: str | None = Header(default=None)) -> dict:
        if config.api_key and not hmac.compare_digest(x_api_key or "", config.api_key):
            raise HTTPException(status_code=401, detail="invalid api key")
        requested_days = max(0, math.ceil((job.end - job.start).total_seconds() / 86_400))
        if job.run_kind == "backfill":
            capacity_policy.require_backfill_capacity(config.canonical_root, requested_days=requested_days)
        else:
            capacity_policy.require_ingest_capacity(config.canonical_root)
        run_ids = enqueue_ingest_plan(ledger=ledger, job=job, request_id=current_request_id())
        payload = {"status": "queued", "job_id": job.job_id, "run_id": run_ids[0],
                   "run_ids": run_ids, "window_count": len(run_ids)}
        return {"data": payload, "meta": {"request_id": current_request_id(), "schema_version": "v1"}, "errors": []}

    @app.get(f"{config.api_prefix}/bars")
    def bars(symbol: str, provider: str, timeframe: str = "1d", start: str | None = None,
             end: str | None = None, page_size: int | None = None, cursor: str | None = None) -> dict:
        from datetime import datetime
        started = time.monotonic()
        page = query_engine.provider_bars_page(
            provider=provider, symbol=symbol, timeframe=timeframe,
            start=datetime.fromisoformat(start) if start else None,
            end=datetime.fromisoformat(end) if end else None,
            page_size=page_size, cursor=cursor,
        )
        print(json.dumps({"event": "data_query", "request_id": current_request_id(),
                          "dataset": "provider_bars",
                          "selector_hash": selector_hash({"provider": provider, "symbol": symbol,
                                                          "timeframe": timeframe}),
                          "snapshot_id": page.snapshot_id, "query_mode": "current",
                          "page_size": page_size, "duration_seconds": round(time.monotonic() - started, 4)}),
              flush=True)
        meta = {"request_id": current_request_id(), "schema_version": "v1", "count": page.count,
                "schema_versions": page.schema_versions, "snapshot_id": page.snapshot_id,
                "next_cursor": page.next_cursor}
        if page.warning:
            meta["warnings"] = [page.warning]
        return {"data": page.rows, "meta": meta, "errors": []}

    @app.get(f"{config.api_prefix}/provider-bars/coverage")
    def provider_bars_dataset_coverage(provider: str, symbol: str, timeframe: str = "1d") -> dict:
        payload = provider_bars_coverage(config.canonical_root, provider=provider, symbol=symbol, timeframe=timeframe)
        # For the governed 1m rollout expose session-aware coverage as an
        # additive response.  This makes the distinction between a globally
        # degraded history and its individually safe ready intervals visible
        # to consumers without changing the legacy summary fields.
        if provider == "dukascopy" and timeframe == "1m":
            rows = query_provider_bars(
                config.canonical_root, provider=provider, symbol=symbol, timeframe=timeframe,
            )
            if rows:
                try:
                    instrument = REGISTRY.instrument(provider, symbol)
                    session = REGISTRY.session(instrument.session_profile)
                except ValueError:
                    session = REGISTRY.session("utc_24x7")
                timestamps = [row["bar_ts"] for row in rows if row.get("bar_ts") is not None]
                first, last = min(timestamps), max(timestamps)
                payload = coverage_for_rows(
                    dataset_id="provider_bars",
                    selector={"provider": provider, "symbol": symbol, "timeframe": timeframe},
                    rows=rows,
                    session_profile=session,
                    requested_start=first,
                    requested_end=last + timedelta(minutes=1),
                    timeframe=timedelta(minutes=1),
                ).as_dict()
        return {"data": payload, "meta": {"request_id": current_request_id(), "schema_version": "v1"}, "errors": []}

    @app.get(f"{config.api_prefix}/market-bars")
    def market_bars(symbol: str, provider: str, timeframe: str, price_basis: str,
                    recipe_id: str, recipe_version: str, start: str | None = None,
                    end: str | None = None, page_size: int | None = None,
                    cursor: str | None = None) -> dict:
        from datetime import datetime

        started = time.monotonic()
        page = query_engine.market_bars_page(
            provider=provider, symbol=symbol, timeframe=timeframe, price_basis=price_basis,
            recipe_id=recipe_id, recipe_version=recipe_version,
            start=datetime.fromisoformat(start) if start else None,
            end=datetime.fromisoformat(end) if end else None,
            page_size=page_size, cursor=cursor,
        )
        print(json.dumps({"event": "data_query", "request_id": current_request_id(),
                          "dataset": "market_bars",
                          "selector_hash": selector_hash({"provider": provider, "symbol": symbol,
                                                          "timeframe": timeframe, "price_basis": price_basis,
                                                          "recipe_id": recipe_id, "recipe_version": recipe_version}),
                          "snapshot_id": page.snapshot_id,
                          "query_mode": f"recipe:{recipe_id}@{recipe_version}",
                          "page_size": page_size,
                          "duration_seconds": round(time.monotonic() - started, 4)}),
              flush=True)
        meta = {"request_id": current_request_id(), "schema_version": "v1", "count": page.count,
                "schema_versions": page.schema_versions, "snapshot_id": page.snapshot_id,
                "next_cursor": page.next_cursor}
        if page.warning:
            meta["warnings"] = [page.warning]
        return {"data": page.rows, "meta": meta, "errors": []}

    @app.post(f"{config.api_prefix}/derive/runs", status_code=202)
    def derive(job: DeriveJob, x_api_key: str | None = Header(default=None)) -> dict:
        if config.api_key and not hmac.compare_digest(x_api_key or "", config.api_key):
            raise HTTPException(status_code=401, detail="invalid api key")
        capacity_policy.require_ingest_capacity(config.canonical_root)
        run_id = LocalWorker(config.canonical_root, ledger).submit_derive(job)
        return {"data": {"status": "queued", "job_id": job.job_id, "run_id": run_id,
                         "input_snapshot_id": ledger.get(run_id).get("input_snapshot_id")},
                "meta": {"request_id": current_request_id(), "schema_version": "v1"}, "errors": []}

    @app.post(f"{config.api_prefix}/quality/checks")
    def quality_check(job: IngestJob, x_api_key: str | None = Header(default=None)) -> dict:
        if config.api_key and not hmac.compare_digest(x_api_key or "", config.api_key):
            raise HTTPException(status_code=401, detail="invalid api key")
        from data_center.connectors.fixture import fetch_bars
        findings = check_provider_bars(fetch_bars(job))
        status = "pass" if not findings else "fail"
        if findings:
            ledger.add_findings([{**finding, "job_id": job.job_id, "dataset_id": job.dataset_id} for finding in findings])
        return {"data": {"status": status, "finding_count": len(findings), "findings": findings}, "meta": {"request_id": current_request_id(), "schema_version": "v1"}, "errors": []}

    @app.get(f"{config.api_prefix}/quality/findings")
    def quality_findings() -> dict:
        findings = ledger.findings()
        return {"data": findings, "meta": {"request_id": current_request_id(), "schema_version": "v1", "count": len(findings)}, "errors": []}

    @app.get(f"{config.api_prefix}/economic/observations")
    def economic_observations(series_id: str, provider: str = "fred", start: str | None = None,
                              end: str | None = None, asof_ts: str | None = None,
                              mode: str = "current", page_size: int | None = None,
                              cursor: str | None = None) -> dict:
        if mode not in {"current", "pit"}:
            raise HTTPException(status_code=422, detail="mode must be current or pit")
        if mode == "pit" and not asof_ts:
            raise HTTPException(status_code=422, detail="pit mode requires asof_ts")
        effective_mode = "pit" if mode == "current" and asof_ts is not None else mode
        started = time.monotonic()
        page = query_engine.economic_observations_page(
            provider=provider, series_id=series_id, start=start, end=end, asof_ts=asof_ts,
            mode=mode, page_size=page_size, cursor=cursor,
        )
        short_versions = {version.rsplit(".", 1)[-1] for version in page.schema_versions}
        economic_schema_version = (next(iter(short_versions)) if len(short_versions) == 1
                                   else "mixed" if short_versions else "unknown")
        print(json.dumps({"event": "data_query", "request_id": current_request_id(),
                          "dataset": "economic_observations",
                          "selector_hash": selector_hash({"provider": provider, "series_id": series_id}),
                          "snapshot_id": page.snapshot_id, "query_mode": effective_mode,
                          "page_size": page_size, "duration_seconds": round(time.monotonic() - started, 4)}),
              flush=True)
        meta = {"request_id": current_request_id(), "schema_version": "v1",
                "economic_schema_version": economic_schema_version, "query_mode": effective_mode,
                "count": page.count, "schema_versions": page.schema_versions,
                "snapshot_id": page.snapshot_id, "next_cursor": page.next_cursor}
        if page.warning:
            meta["warnings"] = [page.warning]
        return {"data": page.rows, "meta": meta, "errors": []}

    @app.get(f"{config.api_prefix}/economic/coverage")
    def economic_dataset_coverage(series_id: str, provider: str = "fred") -> dict:
        payload = economic_observations_coverage(config.canonical_root, provider=provider, series_id=series_id)
        return {"data": payload, "meta": {"request_id": current_request_id(), "schema_version": "v1"}, "errors": []}

    @app.post(f"{config.api_prefix}/economic/ingest")
    def ingest_economic_observations(series_id: str, start: str | None = None, end: str | None = None,
                                     run_scope: str = "production",
                                     x_api_key: str | None = Header(default=None)) -> dict:
        if config.api_key and not hmac.compare_digest(x_api_key or "", config.api_key):
            raise HTTPException(status_code=401, detail="invalid api key")
        capacity_policy.require_ingest_capacity(config.canonical_root)
        if run_scope not in {"production", "acceptance", "migration", "maintenance"}:
            raise HTTPException(status_code=422, detail="invalid run_scope")
        run_id = ledger.enqueue_job({"job_id": f"fred-{series_id}", "dataset_id": "economic_observations",
                                    "provider": "fred", "series_id": series_id, "start": start, "end": end,
                                    "run_scope": run_scope,
                                    "schema_version": "economic_observations.v2",
                                    "request_id": current_request_id()})
        payload = {"run_id": run_id, "dataset_id": "economic_observations", "series_id": series_id, "status": "queued"}
        return {"data": payload, "meta": {"request_id": current_request_id(), "schema_version": "v1"}, "errors": []}

    if config.webui_dist is not None and config.webui_dist.is_dir():
        app.mount("/", StaticFiles(directory=config.webui_dist, html=True), name="webui")

    return app


app = create_app()
