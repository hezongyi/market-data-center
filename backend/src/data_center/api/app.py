from uuid import uuid4

from fastapi import FastAPI, Request, Header, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from data_center import __version__
from data_center.settings import Settings
from data_center.domain.models import IngestJob
from data_center.runs.ledger import RunLedger
from data_center.storage.query import query_provider_bars
from data_center.storage.query import query_economic_observations
from data_center.storage.query import economic_observations_coverage, provider_bars_coverage
from data_center.quality.checks import check_provider_bars
from data_center.catalog.registry import DATASETS
from data_center.ingest.worker import LocalWorker
from data_center.ingest.economic import run_fred_ingest


def create_app(settings: Settings | None = None) -> FastAPI:
    config = settings or Settings()
    app = FastAPI(title=config.app_name, version=__version__)
    ledger = RunLedger(config.ledger_path)
    worker = LocalWorker(config.canonical_root, ledger)

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "data": None,
                "meta": {"request_id": request.headers.get("x-request-id", str(uuid4())), "schema_version": "v1"},
                "errors": [{"code": str(exc.status_code), "message": str(exc.detail)}],
            },
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "data": None,
                "meta": {"request_id": request.headers.get("x-request-id", str(uuid4())), "schema_version": "v1"},
                "errors": [{"code": "validation_error", "message": error["msg"]} for error in exc.errors()],
            },
        )

    @app.get(f"{config.api_prefix}/health")
    def health(request: Request) -> dict:
        return {
            "data": {"status": "ok"},
            "meta": {
                "request_id": request.headers.get("x-request-id", str(uuid4())),
                "schema_version": "v1",
            },
            "errors": [],
        }

    @app.get(f"{config.api_prefix}/runs")
    def runs(request: Request) -> dict:
        return {"data": ledger.list(), "meta": {"request_id": str(uuid4()), "schema_version": "v1"}, "errors": []}

    @app.get(f"{config.api_prefix}/datasets")
    def datasets() -> dict:
        return {"data": list(DATASETS.values()), "meta": {"request_id": str(uuid4()), "schema_version": "v1"}, "errors": []}

    @app.get(f"{config.api_prefix}/runs/{{run_id}}")
    def run(run_id: str) -> dict:
        from fastapi import HTTPException
        try:
            payload = ledger.get(run_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="run not found")
        return {"data": payload, "meta": {"request_id": str(uuid4()), "schema_version": "v1"}, "errors": []}

    @app.post(f"{config.api_prefix}/ingest/runs")
    def ingest(job: IngestJob, x_api_key: str | None = Header(default=None)) -> dict:
        if config.api_key and x_api_key != config.api_key:
            raise HTTPException(status_code=401, detail="invalid api key")
        run_id = worker.submit(job)
        payload = {"status": "queued", "job_id": job.job_id, "run_id": run_id}
        return {"data": payload, "meta": {"request_id": str(uuid4()), "schema_version": "v1"}, "errors": []}

    @app.get(f"{config.api_prefix}/bars")
    def bars(symbol: str, provider: str, timeframe: str = "1d", start: str | None = None, end: str | None = None) -> dict:
        from datetime import datetime
        rows = query_provider_bars(config.canonical_root, provider=provider, symbol=symbol, timeframe=timeframe,
                                   start=datetime.fromisoformat(start) if start else None,
                                   end=datetime.fromisoformat(end) if end else None)
        return {"data": rows, "meta": {"request_id": str(uuid4()), "schema_version": "v1", "count": len(rows)}, "errors": []}

    @app.get(f"{config.api_prefix}/provider-bars/coverage")
    def provider_bars_dataset_coverage(provider: str, symbol: str, timeframe: str = "1d") -> dict:
        payload = provider_bars_coverage(config.canonical_root, provider=provider, symbol=symbol, timeframe=timeframe)
        return {"data": payload, "meta": {"request_id": str(uuid4()), "schema_version": "v1"}, "errors": []}

    @app.post(f"{config.api_prefix}/quality/checks")
    def quality_check(job: IngestJob, x_api_key: str | None = Header(default=None)) -> dict:
        if config.api_key and x_api_key != config.api_key:
            raise HTTPException(status_code=401, detail="invalid api key")
        from data_center.connectors.fixture import fetch_bars
        findings = check_provider_bars(fetch_bars(job))
        status = "pass" if not findings else "fail"
        if findings:
            ledger.add_findings([{**finding, "job_id": job.job_id, "dataset_id": job.dataset_id} for finding in findings])
        return {"data": {"status": status, "finding_count": len(findings), "findings": findings}, "meta": {"request_id": str(uuid4()), "schema_version": "v1"}, "errors": []}

    @app.get(f"{config.api_prefix}/quality/findings")
    def quality_findings() -> dict:
        findings = ledger.findings()
        return {"data": findings, "meta": {"request_id": str(uuid4()), "schema_version": "v1", "count": len(findings)}, "errors": []}

    @app.get(f"{config.api_prefix}/economic/observations")
    def economic_observations(series_id: str, provider: str = "fred", start: str | None = None, end: str | None = None) -> dict:
        rows = query_economic_observations(config.canonical_root, provider=provider, series_id=series_id, start=start, end=end)
        return {"data": rows, "meta": {"request_id": str(uuid4()), "schema_version": "v1", "count": len(rows)}, "errors": []}

    @app.get(f"{config.api_prefix}/economic/coverage")
    def economic_dataset_coverage(series_id: str, provider: str = "fred") -> dict:
        payload = economic_observations_coverage(config.canonical_root, provider=provider, series_id=series_id)
        return {"data": payload, "meta": {"request_id": str(uuid4()), "schema_version": "v1"}, "errors": []}

    @app.post(f"{config.api_prefix}/economic/ingest")
    def ingest_economic_observations(series_id: str, start: str | None = None, end: str | None = None, x_api_key: str | None = Header(default=None)) -> dict:
        if config.api_key and x_api_key != config.api_key:
            raise HTTPException(status_code=401, detail="invalid api key")
        payload = run_fred_ingest(series_id=series_id, root=config.canonical_root, start=start, end=end, ledger=ledger)
        return {"data": payload, "meta": {"request_id": str(uuid4()), "schema_version": "v1"}, "errors": []}

    if config.webui_dist is not None and config.webui_dist.is_dir():
        app.mount("/", StaticFiles(directory=config.webui_dist, html=True), name="webui")

    return app


app = create_app()
