import fcntl
import hashlib
import hmac
import json
import math
import os
import sqlite3
import subprocess
import tempfile
import time
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from uuid import uuid4

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import Cookie, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from data_center import __version__
from data_center.auth import AuthError, AuthStore
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
    governance_units,
    operations_receipts,
    worker_activity,
)
from data_center.platform import coverage_for_rows, enqueue_ingest_plan
from data_center.platform_registry import REGISTRY
from data_center.production_tasks import (
    DefinitionError,
    ProductionConflict,
    ProductionTasks,
)
from data_center.run_views import RunCursorError, RunValidationError, RunView
from data_center.runs.ledger import IdempotencyConflict, RunLedger
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
_auth_store = ContextVar("auth_store", default=None)
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

def _refresh_sessions(config: Settings) -> None:
    """Refresh session records so multiple API workers observe revocations/logins."""
    if not config.auth_state_path or not config.auth_state_path.exists():
        return
    try:
        with config.auth_state_path.open("r", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
            state = json.load(handle)
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        if state.get("password_hash"):
            config.auth_password_hash = state.get("password_hash")
        _sessions.clear()
        _sessions.update({k: (v[0], float(v[1])) for k, v in state.get("sessions", {}).items() if float(v[1]) > time.time()})
    except (OSError, ValueError, TypeError, KeyError):
        return

def _save_auth_state(config: Settings, mutation=None) -> None:
    """Persist auth state with the read/modify/write inside one file lock."""
    if not config.auth_state_path: return
    config.auth_state_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = config.auth_state_path.with_suffix(config.auth_state_path.suffix + ".lock")
    temporary = config.auth_state_path.with_suffix(config.auth_state_path.suffix + ".tmp")
    with lock_path.open("a+", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        state = {}
        if config.auth_state_path.exists():
            try:
                state = json.loads(config.auth_state_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                state = {}
        if state.get("password_hash"):
            config.auth_password_hash = state["password_hash"]
        _sessions.clear()
        _sessions.update({k: (v[0], float(v[1])) for k, v in state.get("sessions", {}).items()
                          if isinstance(v, list) and len(v) == 2 and float(v[1]) > time.time()})
        if mutation:
            mutation()
        if not config.auth_password_hash: return
        payload = {"password_hash": config.auth_password_hash,
                   "sessions": {k: [v[0], v[1]] for k, v in _sessions.items() if v[1] > time.time()}}
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o600)
        temporary.replace(config.auth_state_path)
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
    config.auth_state_path.chmod(0o600)


def current_request_id():
    return _request_id.get()


def require_api_key(config: Settings, provided: str | None, session: str | None = None) -> None:
    """Apply the service-wide write-authentication policy."""
    session = session or _session_id.get()
    store = _auth_store.get()
    valid_session = bool(store and store.session(session))
    valid_key = bool(config.api_key and hmac.compare_digest(provided or "", config.api_key))
    if (config.api_key or (store and store.initialized())) and not valid_key and not valid_session:
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
    store = _auth_store.get()
    current = store.session(session) if store else None
    if current:
        return f"session:{current['username']}"
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
    auth = AuthStore(config.ledger_path.with_name("auth.sqlite3"), username=config.auth_username,
                     seed_hash=config.auth_password_hash, legacy_path=config.auth_state_path,
                     ttl_seconds=config.auth_session_ttl_seconds)
    config.auth_password_hash = None
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
        try: token = auth.login(username, password)
        except AuthError as exc: raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
        response.set_cookie("mdc_session", token, httponly=True, samesite="lax", secure=config.auth_cookie_secure, max_age=config.auth_session_ttl_seconds)
        return api_envelope({"username": username})

    @app.get(f"{config.api_prefix}/auth/status")
    def auth_status():
        """Expose only whether first-time authentication setup is required."""
        return api_envelope({"initialized": auth.initialized(),
                              "username": config.auth_username})

    @app.post(f"{config.api_prefix}/auth/initialize")
    def auth_initialize(payload: dict, request: Request, x_api_key: str | None = Header(default=None, alias="X-API-Key")):
        """Set the first operator password exactly once.

        A configured API key is required to bootstrap a password.  Loopback
        development instances may bootstrap without one; non-loopback
        deployments are already required to configure an API key by Settings.
        """
        origin = request.headers.get("origin")
        if origin and origin.rstrip("/") != f"{request.url.scheme}://{request.url.netloc}".rstrip("/"):
            raise HTTPException(status_code=403, detail="origin not allowed")
        if config.api_key and not hmac.compare_digest(x_api_key or "", config.api_key):
            raise HTTPException(status_code=401, detail="invalid api key")
        username = str(payload.get("username") or config.auth_username).strip()
        password = str(payload.get("password") or "")
        if username != config.auth_username or len(password) < 12:
            raise HTTPException(status_code=422, detail="username or password does not meet requirements")
        try: auth.initialize(username, password)
        except AuthError as exc: raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
        return api_envelope({"initialized": True, "username": username})

    @app.post(f"{config.api_prefix}/auth/logout")
    def auth_logout(session: str | None = Cookie(default=None, alias="mdc_session")):
        auth.logout(session)
        result = {"data": {"logged_out": True}, "meta": {"schema_version": "v1"}, "errors": []}
        response = Response(content=json.dumps(result), media_type="application/json"); response.delete_cookie("mdc_session"); return response

    @app.get(f"{config.api_prefix}/auth/me")
    def auth_me(session: str | None = Cookie(default=None, alias="mdc_session")):
        current = auth.session(session)
        if not current: raise HTTPException(status_code=401, detail="not authenticated")
        return api_envelope(current)

    @app.post(f"{config.api_prefix}/auth/change-password")
    def auth_change_password(payload: dict, session: str | None = Cookie(default=None, alias="mdc_session"),
                             x_api_key: str | None = Header(default=None, alias="X-API-Key")):
        # Keep the protected-route contract's stable API-key failure response,
        # then require an active browser session before rotating credentials.
        require_api_key(config, x_api_key, session)
        current, replacement = str(payload.get("current_password", "")), str(payload.get("new_password", ""))
        try: auth.change_password(session, current, replacement)
        except AuthError as exc: raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
        return api_envelope({"changed": True})

    @app.middleware("http")
    async def audit_request(request: Request, call_next):
        request_id = request.headers.get("x-request-id", str(uuid4()))[:128]
        token = _request_id.set(request_id); session_token = _session_id.set(request.cookies.get("mdc_session")); auth_token = _auth_store.set(auth)
        started = time.monotonic()
        try:
            origin = request.headers.get("origin")
            if request.method in {"POST", "PUT", "PATCH", "DELETE"} and request.cookies.get("mdc_session") and origin:
                expected = f"{request.url.scheme}://{request.url.netloc}"
                if origin.rstrip("/") != expected.rstrip("/"):
                    raise HTTPException(status_code=403, detail="origin not allowed")
            response = await call_next(request)
        finally:
            _request_id.reset(token); _session_id.reset(session_token); _auth_store.reset(auth_token)
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
        detail = exc.detail
        errors = None
        if isinstance(detail, dict) and detail.get("code"):
            # A route that knows *why* it refused keeps its stable code instead
            # of being flattened into the generic status code (spec 8).
            code = str(detail["code"])
            message = str(detail.get("message") or code)
            errors = [{"code": code, "message": message}]
            errors.extend({**item, "code": item.get("code", code)} for item in detail.get("errors") or [])
        else:
            code = {
                401: "unauthorized", 403: "forbidden", 404: "not_found", 409: "conflict",
                422: "invalid_request", 507: "capacity_protected",
            }.get(exc.status_code, "internal_error" if exc.status_code >= 500 else str(exc.status_code))
            message = str(detail)
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "data": None,
                "meta": {"request_id": current_request_id(), "schema_version": "v1"},
                "errors": errors or [{"code": code, "message": message}],
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
            "capacity_free_ratio": capacity.get("free_ratio") if capacity else None,
            "capacity_measurement_source": capacity.get("measurement_source") if capacity else None,
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

    def installed_governance_units() -> list[str] | None:
        """Ask the host which Data Center timers exist; ``None`` when unanswerable.

        The repository's unit files are the declaration, the host is the reality,
        and an unreadable host is reported as unknown rather than as empty.
        """
        try:
            probe = subprocess.run(
                ["systemctl", "--user", "list-unit-files", "market-data-center-*.timer", "--no-legend"],
                capture_output=True, text=True, timeout=5.0, check=False)
        except (OSError, subprocess.SubprocessError):
            return None
        if probe.returncode != 0:
            return None
        return [line.split()[0] for line in probe.stdout.splitlines() if line.split()]

    production_tasks_service = ProductionTasks(
        ledger, cursor_secret=config.api_key or str(config.canonical_root),
        canonical_root=config.canonical_root, capacity_policy=capacity_policy)

    def production_conflict_status(code: str) -> int:
        """Refusals that are bad requests stay 422; genuine state conflicts are 409."""
        return 422 if code in {"expected_version_required", "unsupported_command", "cursor_error",
                               "page_size_error", "filter_error"} else 409

    @app.get(f"{config.api_prefix}/production/tasks")
    def production_tasks(provider: str | None = None, symbol: str | None = None,
                         desired_state: str | None = None, health: str | None = None,
                         phase: str | None = None, include_deleted: bool = False,
                         page_size: int | None = None, cursor: str | None = None) -> dict:
        """Plan list with SQL-side filtering and a cursor bound to those filters."""
        if desired_state is not None and desired_state not in {"enabled", "paused", "archived"}:
            raise HTTPException(status_code=422, detail="desired_state must be enabled, paused or archived")
        try:
            page = production_tasks_service.list(
                provider=provider, symbol=symbol, desired_state=desired_state, health=health,
                phase=phase, include_deleted=include_deleted, page_size=page_size, cursor=cursor)
        except ProductionConflict as exc:
            raise HTTPException(status_code=production_conflict_status(exc.code),
                                detail={"code": exc.code, "message": str(exc)}) from exc
        return api_envelope(page["tasks"], meta={"page": page["page"]})

    @app.post(f"{config.api_prefix}/production/tasks", status_code=201)
    def production_task_create(payload: dict, request: Request,
                               x_api_key: str | None = Header(default=None)) -> dict:
        require_api_key(config, x_api_key)
        actor = operator_identity(request, config)
        try:
            task = production_tasks_service.create(
                definition=payload.get("definition") or {}, name=payload.get("name") or "",
                task_id=payload.get("task_id"), alias=payload.get("alias"),
                desired_state=payload.get("desired_state", "paused"), actor=actor,
                request_id=current_request_id(),
                idempotency_key=request.headers.get("Idempotency-Key"))
        except DefinitionError as exc:
            raise HTTPException(status_code=422, detail={"code": "invalid_definition",
                                                         "message": str(exc),
                                                         "errors": exc.errors}) from exc
        except IdempotencyConflict as exc:
            raise HTTPException(status_code=409, detail={"code": "idempotency_conflict",
                                                         "message": str(exc)}) from exc
        except ProductionConflict as exc:
            raise HTTPException(status_code=production_conflict_status(exc.code),
                                detail={"code": exc.code, "message": str(exc)}) from exc
        return api_envelope(task)

    @app.post(f"{config.api_prefix}/production/plans")
    def production_plan_preview(payload: dict) -> dict:
        """Side-effect-free preview of a plan definition; it writes nothing (spec 8)."""
        return api_envelope(production_tasks_service.preview(payload.get("definition") or payload))

    @app.get(f"{config.api_prefix}/production/tasks/{{task_id}}")
    def production_task_detail(task_id: str) -> dict:
        task = production_tasks_service.read(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="production task not found")
        return api_envelope(task)

    @app.patch(f"{config.api_prefix}/production/tasks/{{task_id}}")
    def production_task_change(task_id: str, payload: dict, request: Request,
                               x_api_key: str | None = Header(default=None)) -> dict:
        require_api_key(config, x_api_key)
        actor = operator_identity(request, config)
        if "desired_state" in payload and "definition" not in payload:
            command = {"paused": "pause", "enabled": "resume", "archived": "archive"}.get(
                str(payload["desired_state"]))
            if command is None:
                raise HTTPException(status_code=422, detail="desired_state must be enabled, paused or archived")
        else:
            command = "update"
        try:
            task = production_tasks_service.change(
                task_id, command, definition=payload.get("definition"),
                expected_version=payload.get("expected_version"), actor=actor,
                request_id=current_request_id(), name=payload.get("name"),
                alias=payload.get("alias"),
                idempotency_key=request.headers.get("Idempotency-Key"))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="production task not found") from exc
        except DefinitionError as exc:
            raise HTTPException(status_code=422, detail={"code": "invalid_definition",
                                                         "message": str(exc),
                                                         "errors": exc.errors}) from exc
        except IdempotencyConflict as exc:
            raise HTTPException(status_code=409, detail={"code": "idempotency_conflict",
                                                         "message": str(exc)}) from exc
        except ProductionConflict as exc:
            raise HTTPException(status_code=production_conflict_status(exc.code),
                                detail={"code": exc.code, "message": str(exc)}) from exc
        return api_envelope(task)

    @app.post(f"{config.api_prefix}/production/tasks/{{task_id}}/actions")
    def production_task_action(task_id: str, payload: dict, request: Request,
                               x_api_key: str | None = Header(default=None)) -> dict:
        require_api_key(config, x_api_key)
        command = str(payload.get("command") or "")
        actor = operator_identity(request, config)
        try:
            result = production_tasks_service.change(
                task_id, command, definition=payload.get("definition"),
                expected_version=payload.get("expected_version"), actor=actor,
                request_id=current_request_id(), name=payload.get("name"),
                alias=payload.get("alias"),
                idempotency_key=request.headers.get("Idempotency-Key"))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="production task not found") from exc
        except DefinitionError as exc:
            raise HTTPException(status_code=422, detail={"code": "invalid_definition",
                                                         "message": str(exc),
                                                         "errors": exc.errors}) from exc
        except IdempotencyConflict as exc:
            raise HTTPException(status_code=409, detail={"code": "idempotency_conflict",
                                                         "message": str(exc)}) from exc
        except ProductionConflict as exc:
            raise HTTPException(status_code=production_conflict_status(exc.code),
                                detail={"code": exc.code, "message": str(exc)}) from exc
        return api_envelope(result)

    @app.get(f"{config.api_prefix}/production/tasks/{{task_id}}/executions")
    def production_task_executions(task_id: str, page_size: int | None = None,
                                   cursor: str | None = None) -> dict:
        task = production_tasks_service.read(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="production task not found")
        try:
            page = production_tasks_service.executions(
                task["task_id"], page_size=page_size, cursor=cursor)
        except ProductionConflict as exc:
            raise HTTPException(status_code=production_conflict_status(exc.code),
                                detail={"code": exc.code, "message": str(exc)}) from exc
        return api_envelope(page["executions"], meta={"page": page["page"]})

    @app.post(f"{config.api_prefix}/production/executions/{{execution_id}}/retry", status_code=202)
    def production_execution_retry(execution_id: str, request: Request,
                                   x_api_key: str | None = Header(default=None)) -> dict:
        """Re-plan the unfinished needs of a terminal round as a linked follow-up."""
        require_api_key(config, x_api_key)
        try:
            result = production_tasks_service.retry(
                execution_id=execution_id, actor=operator_identity(request, config),
                request_id=current_request_id(),
                idempotency_key=request.headers.get("Idempotency-Key"))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="production execution not found") from exc
        except IdempotencyConflict as exc:
            raise HTTPException(status_code=409, detail={"code": "idempotency_conflict",
                                                         "message": str(exc)}) from exc
        except ProductionConflict as exc:
            raise HTTPException(status_code=production_conflict_status(exc.code),
                                detail={"code": exc.code, "message": str(exc)}) from exc
        return api_envelope(result)

    @app.get(f"{config.api_prefix}/production/executions/{{execution_id}}")
    def production_execution_detail(execution_id: str) -> dict:
        execution = ledger.get_production_execution(execution_id)
        if execution is None:
            raise HTTPException(status_code=404, detail="production execution not found")
        return api_envelope(execution)

    @app.get(f"{config.api_prefix}/production/executions/{{execution_id}}/steps")
    def production_execution_steps(execution_id: str, limit: int = 100) -> dict:
        if ledger.get_production_execution(execution_id) is None:
            raise HTTPException(status_code=404, detail="production execution not found")
        return api_envelope(ledger.list_production_steps(execution_id, limit=limit))

    @app.patch(f"{config.api_prefix}/maintenance/tasks/{{task_id}}")
    def maintenance_task_status(task_id: str, payload: dict, request: Request,
                                x_api_key: str | None = Header(default=None, alias="X-API-Key"),
                                session: str | None = Cookie(default=None, alias="mdc_session")) -> dict:
        require_api_key(config, x_api_key, session)
        status = str(payload.get("status", ""))
        if status not in {"paused", "enabled"}:
            raise HTTPException(status_code=422, detail="status must be paused or enabled")
        task = ledger.update_maintenance_task_status(task_id, status)
        if task is None: raise HTTPException(status_code=404, detail="maintenance task not found")
        ledger.record_write_audit({"action": f"maintenance.task.{status}", "actor": operator_identity(request, config), "request_id": current_request_id(), "task_id": task_id, "run_ids": task.get("run_ids", []), "run_kind": task.get("run_kind"), "run_scope": task.get("run_scope"), "dataset_id": task.get("dataset_id"), "outcome": "updated", "code": None, "message": status})
        return api_envelope(task)

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
        # `fixed_measurement` stays for consumers that already read it; `measurement_source` is the
        # unified field and it also travels inside every capacity receipt.
        live["fixed_measurement"] = live["measurement_source"] == "fixed_acceptance"
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

    @app.get(f"{config.api_prefix}/production/catalog-matrix")
    def production_catalog_matrix() -> dict:
        """Registered x planned matrix; a read model that never widens scope."""
        return api_envelope(production_tasks_service.catalog_matrix())

    @app.get(f"{config.api_prefix}/operations/units")
    def operations_units() -> dict:
        """Governance timers: owner, cadence, latest receipt, declaration differences."""
        return api_envelope(governance_units(
            declared_root=Path(__file__).resolve().parents[4] / "deploy" / "systemd",
            installed=installed_governance_units(),
            receipt_index=receipt_index))

    @app.get(f"{config.api_prefix}/operations/scheduler")
    def operations_scheduler() -> dict:
        """Scheduler heartbeat, dispatch switch, due backlog and plan counts.

        The projection is observational: it reports what the ledger recorded and
        never infers a healthy state the scheduler did not write (spec 3.2, 7.3).
        """
        moment = datetime.now(timezone.utc)
        state = ledger.scheduler_state()
        due = ledger.list_due_production_tasks(now=moment.isoformat(), limit=50)
        counts: dict[str, int] = {}
        blocked: list[dict] = []
        for task in ledger.list_production_tasks():
            counts[task["desired_state"]] = counts.get(task["desired_state"], 0) + 1
            document = production_tasks_service.read(task["task_id"])
            if document is not None and document["block_reason"] is not None:
                blocked.append({"task_id": document["task_id"], "reason": document["block_reason"],
                                "health": document["health"]})
        gate = production_tasks_service.dispatch_gate(now=moment)
        return api_envelope({
            "scheduler": state,
            "dispatch_enabled": state["dispatch_enabled"],
            "due_now": len(due),
            "due_task_ids": [task["task_id"] for task in due],
            "plans_by_state": counts,
            "oldest_due_at": min([task["next_run_at"] for task in due], default=None),
            "queue": ledger.job_queue_state(),
            # A critical capacity state refuses new publishing work; warning is
            # decided per plan against its unattended catch-up span (spec 5.6).
            "capacity": gate["capacity"],
            "publishing_allowed": gate["allowed"],
            "provider_backoff": ledger.provider_backoff_state(now=moment),
            "blocked": blocked,
        })

    @app.post(f"{config.api_prefix}/operations/scheduler/actions")
    def operations_scheduler_action(payload: dict, request: Request,
                                    x_api_key: str | None = Header(default=None)) -> dict:
        require_api_key(config, x_api_key)
        command = str(payload.get("command") or "")
        if command not in {"pause_dispatch", "resume_dispatch"}:
            raise HTTPException(status_code=422, detail={
                "code": "unsupported_command",
                "message": "command must be pause_dispatch or resume_dispatch"})
        result = ledger.set_global_dispatch(command == "resume_dispatch",
                                            actor=operator_identity(request, config),
                                            request_id=current_request_id())
        return api_envelope({"command": command, **result})

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
        # Detailed coverage is computed for one selector shape today. Every other shape says so in a
        # machine-readable reason, so a console never has to render a bare "not published" that reads
        # the same as "checked and healthy".
        detail_unavailable = ("detailed coverage requires start and end, because readiness is only "
                              "evaluated over the requested window")
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
                detail_unavailable = None
            else:
                detail_unavailable = ("no canonical rows are published for this selector, so readiness "
                                      "cannot be evaluated")
        elif provider == "dukascopy" and timeframe == "1m":
            # A min/max summary must not infer gaps across periods that were
            # never requested/observed (for example sparse historical imports).
            payload = {**payload, "coverage_scope": "summary",
                       "readiness_status": "unknown", "ready_interval_count": 0,
                       "ready_intervals": [], "gap_count": None,
                       "missing_timestamp_count": None}
        else:
            detail_unavailable = (f"detailed coverage is computed for provider=dukascopy timeframe=1m with "
                                  f"start and end; {provider} {timeframe} answers with the summary only")
        return api_envelope({**payload, "coverage_detail_unavailable": detail_unavailable})

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
