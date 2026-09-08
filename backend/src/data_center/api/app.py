from uuid import uuid4

from fastapi import FastAPI, Request

from data_center import __version__
from data_center.settings import Settings


def create_app(settings: Settings | None = None) -> FastAPI:
    config = settings or Settings()
    app = FastAPI(title=config.app_name, version=__version__)

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

    return app


app = create_app()

