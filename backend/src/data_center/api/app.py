from uuid import uuid4

from fastapi import FastAPI, Request

from data_center import __version__
from data_center.settings import Settings
from data_center.domain.models import IngestJob
from data_center.ingest.service import run_fixture_ingest
from data_center.runs.ledger import RunLedger


def create_app(settings: Settings | None = None) -> FastAPI:
    config = settings or Settings()
    app = FastAPI(title=config.app_name, version=__version__)
    ledger = RunLedger(config.ledger_path)

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

    @app.get(f"{config.api_prefix}/runs/{{run_id}}")
    def run(run_id: str) -> dict:
        from fastapi import HTTPException
        try:
            payload = ledger.get(run_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="run not found")
        return {"data": payload, "meta": {"request_id": str(uuid4()), "schema_version": "v1"}, "errors": []}

    @app.post(f"{config.api_prefix}/ingest/runs")
    def ingest(job: IngestJob) -> dict:
        payload = run_fixture_ingest(job, config.canonical_root, ledger)
        return {"data": payload, "meta": {"request_id": str(uuid4()), "schema_version": "v1"}, "errors": []}

    return app


app = create_app()
