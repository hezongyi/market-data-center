import hashlib
import hmac
import json
import math
import secrets
import sqlite3
import tempfile
import time
from threading import Lock
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import Cookie, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
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
from data_center.control_plane import timeframe_delta
from data_center.deployment import validated_runtime_identity
from data_center.domain.models import DeriveJob, IngestJob
from data_center.maintenance_tasks import (
    RUN_SCOPES,
    MaintenanceTaskError,
    MaintenanceTaskRequest,
    evaluate_with_capacity,
    platform_capabilities,
    submit_task,
)
from data_center.observability import AlertSink, run_metrics
from data_center.operations_views import (
    capacity_history,
    operations_receipts,
    worker_activity,
)
from data_center.platform import coverage_for_rows, enqueue_ingest_plan
from data_center.platform_registry import REGISTRY
from data_center.run_views import RunCursorError, RunValidationError, RunView
from data_center.runs.ledger import RunLedger
from data_center.settings import Settings
from data_center.snapshot import ReceiptIndex, build_snapshot
from data_center.storage.query import (
    CursorError,
    QueryEngine,
    QueryValidationError,
    economic_observations_coverage,
    market_bars_coverage,
    provider_bars_coverage,
    query_market_bars,
    query_provider_bars,
)

_request_id = ContextVar("request_id", default="")
_session_id = ContextVar("session_id", default=None)
_sessions: dict[str, tuple[str, float]] = {}
_auth_lock = Lock()
_password_hasher = PasswordHasher()

def _password_ok(password: str, encoded: str | None) -> bool:
    if not encoded: return False
    try:
        return _password_hasher.verify(encoded, password)
    except (ValueError, TypeError, VerifyMismatchError): return False

def _password_hash(password: str) -> str:
    return _password_hasher.hash(password)

def _load_auth_state(config: Settings) -> None:
    if config.auth_password_hash or not config.auth_state_path or not config.auth_state_path.exists(): return
    try:
        state = json.loads(config.auth_state_path.read_text())
        config.auth_password_hash = state.get("password_hash")
        _sessions.update({k: (v[0], float(v[1])) for k, v in state.get("sessions", {}).items() if float(v[1]) > time.time()})
    except (OSError, ValueError): return

def _save_auth_state(config: Settings) -> None:
    if not config.auth_state_path or not config.auth_password_hash: return
    config.auth_state_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"password_hash": config.auth_password_hash,
               "sessions": {k: [v[0], v[1]] for k, v in _sessions.items() if v[1] > time.time()}}
    temporary = config.auth_state_path.with_suffix(config.auth_state_path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload), encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(config.auth_state_path)
    config.auth_state_path.chmod(0o600)


def current_request_id():
    return _request_id.get()


def require_api_key(config: Settings, provided: str | None, session: str | None = None) -> None:
    """Apply the service-wide write-authentication policy."""
    session = session or _session_id.get()
    valid_session = bool(session and session in _sessions and _sessions[session][1] > time.time())
    if session and not valid_session: _sessions.pop(session, None)
    valid_key = bool(config.api_key and hmac.compare_digest(provided or "", config.api_key))
    if (config.auth_password_hash or config.api_key) and not valid_key and not valid_session:
        raise HTTPException(status_code=401, detail="invalid api key")


def api_envelope(data, *, meta: dict | None = None) -> dict:
    """Build the versioned success envelope without duplicating its shape.

    Failure paths keep their own handlers: they answer with distinct status
    codes and, for capacity protection, a structured ``data`` payload.
    """
    response_meta = {"request_id": current_request_id(), "schema_version": "v1"}
    if meta:
        response_meta.update(meta)
    return {"data": data, "meta": response_meta, "errors": []}


def operator_identity(request: Request, config: Settings) -> str:
    """Non-reversible actor fingerprint for the write audit trail.

    A declared operator name is used verbatim; otherwise the API key is
    fingerprinted.  The credential itself is never stored or logged.
    """
    declared = (request.headers.get("x-operator") or "").strip()
    if declared:
        return declared[:64]
    session = _session_id.get()
    if session and session in _sessions and _sessions[session][1] > time.time():
        return f"session:{_sessions[session][0]}"
    key = request.headers.get("x-api-key") or ""
    if key:
        return f"api-key:{hashlib.sha256(key.encode()).hexdigest()[:12]}"
    return "anonymous"


def record_submission_outcome(ledger: RunLedger, request: MaintenanceTaskRequest, *, actor: str | None,                              request_id: str | None, outcome: str, code: str | None,
                              message: str | None) -> None:
    """Audit every maintenance attempt, including the ones that were refused."""
    ledger.record_write_audit({
        "action": f"maintenance.{request.run_kind}", "actor": actor, "request_id": request_id,
        "task_id": request.task_id, "run_ids": [], "run_kind": request.run_kind,
        "run_scope": request.run_scope, "dataset_id": request.dataset_id,
        "selector": {"provider": request.provider, "symbol": request.symbol, "timeframe": request.timeframe,
                     "series_id": request.series_id},
        "time_range": {"start": request.start.isoformat(), "end": request.end.isoformat()},
        "outcome": outcome, "code": code, "message": message,
    })


def economic_boundary(value: str | None, field: str) -> datetime:
    """Normalize an economic request boundary to an explicit UTC instant.

    The economic dataset is daily, so an unspecified start means "the whole
    series" and an unspecified end means "through now" instead of an implicit
    provider default the console could not show.
    """
    if not value:
        return datetime(1900, 1, 1, tzinfo=timezone.utc) if field == "start" else datetime.now(timezone.utc)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def submit_maintenance(*, request: MaintenanceTaskRequest, ledger: RunLedger, config: Settings,
                       capacity_policy, http_request: Request, request_id: str) -> dict:
    """Shared submission path for every write endpoint.

    Validation and capacity protection are applied identically whether a task
    arrives through the unified maintenance API or a legacy endpoint, and the
    refusal is audited either way.
    """
    actor = operator_identity(http_request, config)
    try:
        return submit_task(request=request, ledger=ledger, root=config.canonical_root,
                           capacity_policy=capacity_policy, request_id=request_id, actor=actor)
    except MaintenanceTaskError as exc:
        record_submission_outcome(ledger, request, actor=actor, request_id=request_id,
                                  outcome="rejected", code=exc.code, message=str(exc))
        raise HTTPException(status_code=422, detail=str(exc))
    except CapacityProtectedError as exc:
        record_submission_outcome(ledger, request, actor=actor, request_id=request_id,
                                  outcome="protected", code="capacity_protected", message=str(exc))
        raise


def create_app(settings: Settings | None = None) -> FastAPI:
    config = settings or Settings()
    if config.auth_state_path is None:
        config.auth_state_path = config.ledger_path.with_name("auth-state.json")
    _load_auth_state(config)
    app = FastAPI(title=config.app_name, version=__version__)
    ledger = RunLedger(config.ledger_path)
    query_engine = QueryEngine(config.canonical_root)
    capacity_policy = config.capacity_policy()
    run_view = RunView(ledger, canonical_root=config.canonical_root,
                       cursor_secret=config.api_key or str(config.canonical_root))
    alert_sink = AlertSink(config.evidence_root / "alerts", config.alerts_enabled)
    receipt_index = ReceiptIndex(config.evidence_root)
    identity = validated_runtime_identity(
        config.deployment_manifest, config.evidence_root, component="api", webui_dist=config.webui_dist,
    ) if config.deployment_manifest else {
        "deployment_id": "development", "software_version": __version__, "source_commit": "unknown"
    }
    print(json.dumps({"event": "api_started", "request_id": None,
                      **{key: identity[key] for key in ("deployment_id", "software_version", "source_commit")}}),
          flush=True)

    @app.post(f"{config.api_prefix}/auth/login")
    def auth_login(payload: dict, response: Response):
        username = str(payload.get("username", "")); password = str(payload.get("password", ""))
        if username != config.auth_username or not _password_ok(password, config.auth_password_hash):
            raise HTTPException(status_code=401, detail="invalid credentials")
        token = secrets.token_urlsafe(32); _sessions[token] = (username, time.time() + config.auth_session_ttl_seconds); _save_auth_state(config)
        response.set_cookie("mdc_session", token, httponly=True, samesite="lax", secure=config.auth_cookie_secure, max_age=config.auth_session_ttl_seconds)
        return api_envelope({"username": username})

    @app.get(f"{config.api_prefix}/auth/status")
    def auth_status():
        """Expose only whether first-time authentication setup is required."""
        return api_envelope({"initialized": bool(config.auth_password_hash),
                              "username": config.auth_username})

    @app.post(f"{config.api_prefix}/auth/initialize")
    def auth_initialize(payload: dict, request: Request, x_api_key: str | None = Header(default=None, alias="X-API-Key")):
        """Set the first operator password exactly once.

        A configured API key is required to bootstrap a password.  Loopback
        development instances may bootstrap without one; non-loopback
        deployments are already required to configure an API key by Settings.
        """
        with _auth_lock:
            if config.auth_password_hash:
                raise HTTPException(status_code=409, detail="authentication is already initialized")
        origin = request.headers.get("origin")
        if origin and origin.rstrip("/") != f"{request.url.scheme}://{request.url.netloc}".rstrip("/"):
            raise HTTPException(status_code=403, detail="origin not allowed")
        if config.api_key and not hmac.compare_digest(x_api_key or "", config.api_key):
            raise HTTPException(status_code=401, detail="invalid api key")
        username = str(payload.get("username") or config.auth_username).strip()
        password = str(payload.get("password") or "")
        if username != config.auth_username or len(password) < 12:
            raise HTTPException(status_code=422, detail="username or password does not meet requirements")
        with _auth_lock:
            if config.auth_password_hash:
                raise HTTPException(status_code=409, detail="authentication is already initialized")
            config.auth_password_hash = _password_hash(password)
            _save_auth_state(config)
        return api_envelope({"initialized": True, "username": username})

    @app.post(f"{config.api_prefix}/auth/logout")
    def auth_logout(session: str | None = Cookie(default=None, alias="mdc_session")):
        if session: _sessions.pop(session, None); _save_auth_state(config)
        result = {"data": {"logged_out": True}, "meta": {"schema_version": "v1"}, "errors": []}
        response = Response(content=json.dumps(result), media_type="application/json"); response.delete_cookie("mdc_session"); return response

    @app.get(f"{config.api_prefix}/auth/me")
    def auth_me(session: str | None = Cookie(default=None, alias="mdc_session")):
        valid = bool(session and session in _sessions and _sessions[session][1] > time.time())
        if not valid: raise HTTPException(status_code=401, detail="not authenticated")
        return api_envelope({"username": _sessions[session][0], "expires_at": _sessions[session][1]})

    @app.post(f"{config.api_prefix}/auth/change-password")
    def auth_change_password(payload: dict, session: str | None = Cookie(default=None, alias="mdc_session"),
                             x_api_key: str | None = Header(default=None, alias="X-API-Key")):
        # Keep the protected-route contract's stable API-key failure response,
        # then require an active browser session before rotating credentials.
        require_api_key(config, x_api_key, session)
        if not session or session not in _sessions or _sessions[session][1] <= time.time():
            raise HTTPException(status_code=401, detail="not authenticated")
        current, replacement = str(payload.get("current_password", "")), str(payload.get("new_password", ""))
        if not _password_ok(current, config.auth_password_hash) or len(replacement) < 12:
            raise HTTPException(status_code=422, detail="invalid password change")
        config.auth_password_hash = _password_hash(replacement)
        _save_auth_state(config)
        _sessions.clear()
        return api_envelope({"changed": True})

    @app.middleware("http")
    async def audit_request(request: Request, call_next):
        request_id = request.headers.get("x-request-id", str(uuid4()))[:128]
        token = _request_id.set(request_id); session_token = _session_id.set(request.cookies.get("mdc_session"))
        started = time.monotonic()
        try:
            origin = request.headers.get("origin")
            if request.method in {"POST", "PUT", "PATCH", "DELETE"} and request.cookies.get("mdc_session") and origin:
                expected = f"{request.url.scheme}://{request.url.netloc}"
                if origin.rstrip("/") != expected.rstrip("/"):
                    raise HTTPException(status_code=403, detail="origin not allowed")
            response = await call_next(request)
        finally:
            _request_id.reset(token); _session_id.reset(session_token)
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
        # Write failures carry a stable, safe semantic code so a console can
        # distinguish permission, protection, conflict and validation without
        # parsing prose; the message stays human-readable.
        code = {
            401: "unauthorized", 403: "forbidden", 404: "not_found", 409: "conflict",
            422: "invalid_request", 507: "capacity_protected",
        }.get(exc.status_code, "internal_error" if exc.status_code >= 500 else str(exc.status_code))
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "data": None,
                "meta": {"request_id": current_request_id(), "schema_version": "v1"},
                "errors": [{"code": code, "message": str(exc.detail)}],
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

    @app.exception_handler(RunValidationError)
    async def run_validation_error_handler(request: Request, exc: RunValidationError) -> JSONResponse:
        code = "cursor_error" if isinstance(exc, RunCursorError) else "run_validation_error"
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
        return api_envelope({"status": "ok"})

    @app.get(f"{config.api_prefix}/health/live")
    def health_live(request: Request) -> dict:
        return api_envelope({"status": "ok"})

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
        return JSONResponse(status_code=status_code, content=api_envelope({
            "status": status, "read_status": "available" if storage_ready else "unavailable",
            "write_status": "protected" if capacity and capacity["status"] == "critical" else "available",
            "capacity_status": capacity["status"] if capacity else "unknown",
            "operational_snapshot_status": snapshot.status if snapshot else "unknown",
            "worker_heartbeat_age_seconds": age,
            "software_version": identity["software_version"], "source_commit": identity["source_commit"],
            "deployment_id": identity["deployment_id"],
        }))

    @app.get(f"{config.api_prefix}/metrics")
    def metrics() -> dict:
        payload = run_metrics(
            ledger, canonical_root=config.canonical_root, evidence_root=config.evidence_root,
            backup_root=config.backup_root, capacity_policy=capacity_policy,
            receipt_index=receipt_index, restore_staging_root=config.restore_staging_root,
        )
        payload.update({key: identity[key] for key in ("deployment_id", "software_version", "source_commit")})
        payload["query"] = query_engine.metrics.snapshot(query_engine.catalog)
        return api_envelope(payload)

    @app.get(f"{config.api_prefix}/runs")
    def runs(status: str | None = None, dataset_id: str | None = None, run_kind: str | None = None,
             run_scope: str | None = None, provider: str | None = None, symbol: str | None = None,
             created_from: str | None = None, created_to: str | None = None,
             page_size: int | None = None, cursor: str | None = None) -> dict:
        page = run_view.list_runs(status=status, dataset_id=dataset_id, run_kind=run_kind,
                                  run_scope=run_scope, provider=provider, symbol=symbol,
                                  created_from=created_from, created_to=created_to,
                                  page_size=page_size, cursor=cursor)
        meta = {"count": page["page"]["count"], "page": page["page"], "filters": page["filters"]}
        if page["warnings"]:
            meta["warnings"] = page["warnings"]
        return api_envelope(page["runs"], meta=meta)

    @app.get(f"{config.api_prefix}/runs/{{run_id}}/detail")
    def run_detail(run_id: str) -> dict:
        """Workbench projection of one run: stage, windows, lineage and findings.

        The stored receipt stays available at ``/runs/{run_id}`` unchanged; this
        view adds derived relations without ever rewriting a terminal run.
        """
        projection = run_view.get_run(run_id)
        if projection is None:
            raise HTTPException(status_code=404, detail="run not found")
        return api_envelope(projection)

    @app.post(f"{config.api_prefix}/maintenance/plans")
    def maintenance_plan(request: MaintenanceTaskRequest) -> dict:
        """Validate a maintenance request and describe exactly what it would queue."""
        preview = evaluate_with_capacity(request=request, root=config.canonical_root,
                                         capacity_policy=capacity_policy)
        return api_envelope(preview)

    @app.get(f"{config.api_prefix}/maintenance/tasks")
    def maintenance_tasks() -> dict:
        return api_envelope(ledger.list_maintenance_tasks())

    @app.post(f"{config.api_prefix}/maintenance/tasks", status_code=202)
    def maintenance_task(request: MaintenanceTaskRequest, http_request: Request,
                         x_api_key: str | None = Header(default=None)) -> dict:
        require_api_key(config, x_api_key)
        envelope = submit_maintenance(request=request, ledger=ledger, config=config,
                                      capacity_policy=capacity_policy, http_request=http_request,
                                      request_id=current_request_id())
        return api_envelope(envelope)

    @app.get(f"{config.api_prefix}/capabilities")
    def capabilities() -> dict:
        """Read model for form validation: what the platform can actually do."""
        return api_envelope(platform_capabilities(capacity_policy, config, ledger))

    @app.get(f"{config.api_prefix}/operations/queue")
    def operations_queue() -> dict:
        payload = ledger.job_queue_state()
        payload["runs_by_status"] = {}
        for run in ledger.list():
            status = run.get("status", "unknown")
            payload["runs_by_status"][status] = payload["runs_by_status"].get(status, 0) + 1
        return api_envelope(payload)

    @app.get(f"{config.api_prefix}/operations/audit")
    def operations_audit(limit: int = 50) -> dict:
        entries = ledger.write_audit_entries(limit=max(1, min(limit, 500)))
        return api_envelope(entries, meta={"count": len(entries)})

    @app.get(f"{config.api_prefix}/operations/capacity-history")
    def operations_capacity_history(limit: int = 50) -> dict:
        live = capacity_policy.inspect(config.canonical_root).as_dict()
        live["fixed_measurement"] = config.capacity_fixed_free_ratio is not None
        payload = capacity_history(alert_sink, live, limit=max(1, min(limit, 500)))
        return api_envelope(payload)

    @app.get(f"{config.api_prefix}/operations/worker")
    def operations_worker() -> dict:
        payload = worker_activity(ledger)
        payload["worker_heartbeat_age_seconds"] = payload["heartbeat_age_seconds"]
        return api_envelope(payload)

    @app.get(f"{config.api_prefix}/operations/receipts")
    def operations_receipts_view(limit: int = 5) -> dict:
        payload = operations_receipts(receipt_index, limit_per_action=max(1, min(limit, 50)))
        return api_envelope(payload)

    @app.post(f"{config.api_prefix}/runs/{{run_id}}/retry", status_code=202)
    def retry(run_id: str, http_request: Request, x_api_key: str | None = Header(default=None)) -> dict:
        require_api_key(config, x_api_key)
        actor = operator_identity(http_request, config)
        try:
            capacity_policy.require_ingest_capacity(config.canonical_root)
            new_id = ledger.retry_run(run_id)
        except (KeyError, ValueError, CapacityProtectedError) as exc:
            code = "not_found" if isinstance(exc, KeyError) else (
                "conflict" if isinstance(exc, ValueError) else "capacity_protected")
            ledger.record_write_audit({
                "action": "runs.retry", "actor": actor, "request_id": current_request_id(),
                "run_ids": [run_id], "selector": {"run_id": run_id}, "outcome": "rejected",
                "code": code, "message": str(exc),
            })
            if isinstance(exc, KeyError):
                raise HTTPException(status_code=404, detail="run not found")
            if isinstance(exc, CapacityProtectedError):
                raise
            raise HTTPException(status_code=409, detail=str(exc))
        ledger.record_write_audit({
            "action": "runs.retry", "actor": actor, "request_id": current_request_id(),
            "run_ids": [run_id, new_id], "selector": {"run_id": run_id}, "outcome": "queued",
            "code": None, "message": f"retry queued as {new_id}",
        })
        return api_envelope(ledger.get(new_id))

    @app.post(f"{config.api_prefix}/runs/{{run_id}}/acknowledge")
    def acknowledge_dead_letter(run_id: str, http_request: Request,
                                x_api_key: str | None = Header(default=None)) -> dict:
        require_api_key(config, x_api_key)
        actor = operator_identity(http_request, config)
        try:
            payload = ledger.acknowledge_dead_letter(run_id)
        except KeyError:
            ledger.record_write_audit({
                "action": "runs.acknowledge", "actor": actor, "request_id": current_request_id(),
                "run_ids": [run_id], "selector": {"run_id": run_id}, "outcome": "rejected",
                "code": "not_found", "message": "run not found",
            })
            raise HTTPException(status_code=404, detail="run not found")
        except ValueError as exc:
            ledger.record_write_audit({
                "action": "runs.acknowledge", "actor": actor, "request_id": current_request_id(),
                "run_ids": [run_id], "selector": {"run_id": run_id}, "outcome": "rejected",
                "code": "conflict", "message": str(exc),
            })
            raise HTTPException(status_code=409, detail=str(exc))
        ledger.record_write_audit({
            "action": "runs.acknowledge", "actor": actor, "request_id": current_request_id(),
            "run_ids": [run_id], "selector": {"run_id": run_id}, "outcome": "acknowledged",
            "code": None, "message": "dead letter acknowledged",
        })
        return api_envelope(payload)

    @app.get(f"{config.api_prefix}/datasets")
    def datasets() -> dict:
        return api_envelope([definition.as_dict() for definition in iter_dataset_definitions()])

    @app.get(f"{config.api_prefix}/runs/{{run_id}}")
    def run(run_id: str) -> dict:
        from fastapi import HTTPException
        try:
            payload = ledger.get(run_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="run not found")
        return api_envelope(payload)

    @app.get(f"{config.api_prefix}/runs/{{run_id}}/manifest")
    def run_manifest(run_id: str) -> dict:
        try:
            payload = json.loads(manifest_path(config.canonical_root, run_id).read_text())
            validate_manifest(config.canonical_root, payload)
        except (OSError, json.JSONDecodeError, PublicationError):
            raise HTTPException(status_code=404, detail="manifest not found")
        return api_envelope(payload)

    @app.post(f"{config.api_prefix}/ingest/runs")
    def ingest(job: IngestJob, http_request: Request, x_api_key: str | None = Header(default=None)) -> dict:
        require_api_key(config, x_api_key)
        actor = operator_identity(http_request, config)
        requested_days = max(0, math.ceil((job.end - job.start).total_seconds() / 86_400))
        if job.run_kind == "backfill":
            capacity_policy.require_backfill_capacity(config.canonical_root, requested_days=requested_days)
        else:
            capacity_policy.require_ingest_capacity(config.canonical_root)
        run_ids = enqueue_ingest_plan(ledger=ledger, job=job, request_id=current_request_id())
        ledger.record_write_audit({
            "action": "maintenance.ingest", "actor": actor, "request_id": current_request_id(),
            "task_id": job.job_id, "run_ids": run_ids, "run_kind": job.run_kind, "run_scope": job.run_scope,
            "dataset_id": job.dataset_id,
            "selector": {"provider": job.provider, "symbol": job.symbol, "timeframe": job.timeframe},
            "time_range": {"start": job.start.isoformat(), "end": job.end.isoformat()},
            "outcome": "queued", "code": None, "message": f"{len(run_ids)} run(s) queued",
        })
        payload = {"status": "queued", "job_id": job.job_id, "run_id": run_ids[0],
                   "run_ids": run_ids, "window_count": len(run_ids)}
        return api_envelope(payload)

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
        meta = {"count": page.count, "schema_versions": page.schema_versions,
                "snapshot_id": page.snapshot_id, "next_cursor": page.next_cursor}
        if page.warning:
            meta["warnings"] = [page.warning]
        return api_envelope(page.rows, meta=meta)

    @app.get(f"{config.api_prefix}/provider-bars/coverage")
    def provider_bars_dataset_coverage(provider: str, symbol: str, timeframe: str = "1d",
                                       start: str | None = None, end: str | None = None) -> dict:
        payload = provider_bars_coverage(config.canonical_root, provider=provider, symbol=symbol, timeframe=timeframe)
        # For the governed 1m rollout expose session-aware coverage as an
        # additive response.  This makes the distinction between a globally
        # degraded history and its individually safe ready intervals visible
        # to consumers without changing the legacy summary fields.
        if provider == "dukascopy" and timeframe == "1m" and start is not None and end is not None:
            rows = query_provider_bars(
                config.canonical_root, provider=provider, symbol=symbol, timeframe=timeframe,
            )
            if rows:
                try:
                    instrument = REGISTRY.instrument(provider, symbol)
                    session = REGISTRY.session(instrument.session_profile)
                except ValueError:
                    session = REGISTRY.session("utc_24x7")
                payload = coverage_for_rows(
                    dataset_id="provider_bars",
                    selector={"provider": provider, "symbol": symbol, "timeframe": timeframe},
                    rows=rows,
                    session_profile=session,
                    requested_start=datetime.fromisoformat(start),
                    requested_end=datetime.fromisoformat(end),
                    timeframe=timedelta(minutes=1),
                ).as_dict()
        elif provider == "dukascopy" and timeframe == "1m":
            # A min/max summary must not infer gaps across periods that were
            # never requested/observed (for example sparse historical imports).
            payload = {**payload, "coverage_scope": "summary",
                       "readiness_status": "unknown", "ready_interval_count": 0,
                       "ready_intervals": [], "gap_count": None,
                       "missing_timestamp_count": None}
        return api_envelope(payload)

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
        meta = {"count": page.count, "schema_versions": page.schema_versions,
                "snapshot_id": page.snapshot_id, "next_cursor": page.next_cursor}
        if page.warning:
            meta["warnings"] = [page.warning]
        return api_envelope(page.rows, meta=meta)

    @app.post(f"{config.api_prefix}/derive/runs", status_code=202)
    def derive(job: DeriveJob, http_request: Request, x_api_key: str | None = Header(default=None)) -> dict:
        require_api_key(config, x_api_key)
        try:
            source_timeframe = REGISTRY.recipe(job.recipe_id, job.recipe_version).source_timeframe
        except ValueError:
            source_timeframe = "1d"
        envelope = submit_maintenance(
            request=MaintenanceTaskRequest(
                run_kind="derive", run_scope=job.run_scope, dataset_id="market_bars",
                provider=job.provider, symbol=job.symbol, timeframe=source_timeframe,
                recipe_id=job.recipe_id, recipe_version=job.recipe_version,
                start=job.start, end=job.end, task_id=job.job_id,
            ),
            ledger=ledger, config=config, capacity_policy=capacity_policy,
            http_request=http_request, request_id=current_request_id(),
        )
        return api_envelope(envelope)

    @app.post(f"{config.api_prefix}/quality/checks", status_code=202)
    def quality_check(job: IngestJob, http_request: Request, x_api_key: str | None = Header(default=None)) -> dict:
        require_api_key(config, x_api_key)
        envelope = submit_maintenance(
            request=MaintenanceTaskRequest(
                run_kind="quality", run_scope=job.run_scope, dataset_id=job.dataset_id,
                provider=job.provider, symbol=job.symbol, asset_class=job.asset_class,
                timeframe=job.timeframe, start=job.start, end=job.end, task_id=job.job_id,
            ),
            ledger=ledger, config=config, capacity_policy=capacity_policy,
            http_request=http_request, request_id=current_request_id(),
        )
        return api_envelope(envelope)

    @app.get(f"{config.api_prefix}/quality/findings")
    def quality_findings(severity: str | None = None, code: str | None = None,
                         dataset_id: str | None = None, run_id: str | None = None,
                         state: str | None = None, series_id: str | None = None,
                         observed_from: str | None = None, observed_to: str | None = None,
                         page_size: int | None = None, cursor: str | None = None) -> dict:
        page = run_view.list_findings(severity=severity, code=code, dataset_id=dataset_id, run_id=run_id,
                                      state=state, series_id=series_id, observed_from=observed_from,
                                      observed_to=observed_to, page_size=page_size, cursor=cursor)
        return api_envelope(page["findings"], meta={
            "count": page["page"]["count"], "page": page["page"], "filters": page["filters"],
            "state_counts": page["state_counts"],
        })

    @app.post(f"{config.api_prefix}/quality/findings/{{finding_id}}/state")
    def quality_finding_state(finding_id: str, payload: dict, http_request: Request,
                              x_api_key: str | None = Header(default=None)) -> dict:
        require_api_key(config, x_api_key)
        state = str(payload.get("state") or "")
        if state not in {"open", "acknowledged", "resolved"}:
            raise HTTPException(status_code=422, detail="state must be open, acknowledged or resolved")
        try:
            record = ledger.set_finding_state(finding_id, state, note=payload.get("note"),
                                              resolved_by_run_id=payload.get("resolved_by_run_id"))
        except KeyError:
            raise HTTPException(status_code=404, detail="finding not found")
        ledger.record_write_audit({
            "action": "quality.finding_state", "actor": operator_identity(http_request, config),
            "request_id": current_request_id(), "task_id": None, "run_ids": [],
            "run_kind": None, "run_scope": None, "dataset_id": payload.get("dataset_id"),
            "selector": {"finding_id": finding_id},
            "time_range": {}, "outcome": state, "code": None,
            "message": payload.get("note") or f"finding marked {state}",
        })
        return api_envelope(record)

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
        meta = {"economic_schema_version": economic_schema_version, "query_mode": effective_mode,
                "count": page.count, "schema_versions": page.schema_versions,
                "snapshot_id": page.snapshot_id, "next_cursor": page.next_cursor}
        if page.warning:
            meta["warnings"] = [page.warning]
        return api_envelope(page.rows, meta=meta)

    @app.get(f"{config.api_prefix}/economic/coverage")
    def economic_dataset_coverage(series_id: str, provider: str = "fred") -> dict:
        payload = economic_observations_coverage(config.canonical_root, provider=provider, series_id=series_id)
        return api_envelope(payload)

    @app.post(f"{config.api_prefix}/economic/ingest", status_code=202)
    def ingest_economic_observations(series_id: str, http_request: Request, start: str | None = None,
                                     end: str | None = None, run_scope: str = "production",
                                     x_api_key: str | None = Header(default=None)) -> dict:
        require_api_key(config, x_api_key)
        if run_scope not in RUN_SCOPES:
            raise HTTPException(status_code=422, detail="invalid run_scope")
        envelope = submit_maintenance(
            request=MaintenanceTaskRequest(
                run_kind="ingest", run_scope=run_scope, dataset_id="economic_observations",
                provider="fred", series_id=series_id, task_id=f"fred-{series_id}",
                start=economic_boundary(start, "start"), end=economic_boundary(end, "end"),
            ),
            ledger=ledger, config=config, capacity_policy=capacity_policy,
            http_request=http_request, request_id=current_request_id(),
        )
        # Legacy convenience keys stay present: consumers already read
        # ``run_id``/``status`` and the unified envelope keeps them.
        envelope["series_id"] = series_id
        return api_envelope(envelope)

    @app.get(f"{config.api_prefix}/market-bars/coverage")
    def market_bars_dataset_coverage(symbol: str, provider: str, timeframe: str, price_basis: str,
                                     recipe_id: str, recipe_version: str, start: str | None = None,
                                     end: str | None = None) -> dict:
        payload = market_bars_coverage(
            config.canonical_root, provider=provider, symbol=symbol, timeframe=timeframe,
            price_basis=price_basis, recipe_id=recipe_id, recipe_version=recipe_version,
        )
        payload["recipe"] = {"recipe_id": recipe_id, "recipe_version": recipe_version}
        try:
            recipe = REGISTRY.recipe(recipe_id, recipe_version)
        except ValueError:
            payload["recipe_status"] = "not_registered"
        else:
            payload["recipe_status"] = "registered"
            payload["recipe"].update({"source_timeframe": recipe.source_timeframe,
                                      "target_timeframe": recipe.target_timeframe,
                                      "session_profile": recipe.session_profile,
                                      "materialization": recipe.materialization,
                                      "partial_bucket_policy": recipe.partial_bucket_policy,
                                      "missing_input_policy": recipe.missing_input_policy})
            if start is not None and end is not None:
                rows = query_market_bars(
                    config.canonical_root, provider=provider, symbol=symbol, timeframe=timeframe,
                    price_basis=price_basis, recipe_id=recipe_id, recipe_version=recipe_version,
                )
                if rows:
                    try:
                        session_id = REGISTRY.instrument(provider, symbol).session_profile
                    except ValueError:
                        session_id = recipe.session_profile
                    payload = coverage_for_rows(
                        dataset_id="market_bars",
                        selector={"provider": provider, "symbol": symbol, "timeframe": timeframe,
                                  "price_basis": price_basis, "recipe_id": recipe_id,
                                  "recipe_version": recipe_version},
                        rows=rows, session_profile=REGISTRY.session(session_id),
                        requested_start=datetime.fromisoformat(start),
                        requested_end=datetime.fromisoformat(end),
                        timeframe=timeframe_delta(timeframe),
                    ).as_dict() | {"recipe": payload["recipe"], "recipe_status": "registered",
                                   "price_basis": price_basis}
        return api_envelope(payload)

    if config.webui_dist is not None and config.webui_dist.is_dir():
        app.mount("/", StaticFiles(directory=config.webui_dist, html=True), name="webui")

    return app


app = create_app()
