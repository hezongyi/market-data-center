"""Scheduled production acceptance through the public API; receipts and local alerts."""
import argparse
import fcntl
import gzip
import hashlib
import json
import os
import platform
import re
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import requests

from data_center.deployment import runtime_identity
from data_center.observability import AlertSink
from data_center.settings import Settings


def evidence_context() -> dict:
    repo_root = Path(__file__).resolve().parents[3]
    try:
        commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo_root, text=True,
                                         stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.CalledProcessError):
        commit = os.getenv("GIT_COMMIT", "unknown")
    context = {"commit": commit, "python": platform.python_version(), "host": platform.node()}
    manifest = os.getenv("DATACENTER_DEPLOYMENT_MANIFEST")
    if manifest:
        try:
            context.update(runtime_identity(Path(manifest)))
        except (OSError, RuntimeError, ValueError, json.JSONDecodeError):
            context["deployment_identity"] = "invalid"
    return context


def _rows_hash(rows: list[dict]) -> str:
    return hashlib.sha256(json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


# A red acceptance run has to be triageable without a re-run, so every provider failure is recorded with
# its category, HTTP status, response summary and the adapter that produced it (issue #94).
_ACCEPTANCE_ATTEMPTS = 3
_ACCEPTANCE_BACKOFF_SECONDS = 5.0
_SECRET_PATTERN = re.compile(r"(?i)(api[_-]?key|token|secret|password|authorization)(\"?\s*[:=]\s*\"?)([^\s\",}]+)")


def redact_summary(text: str, limit: int = 240) -> str:
    """Keep a short failure summary that never echoes a credential-shaped value."""
    return _SECRET_PATTERN.sub(r"\1\2<redacted>", text)[:limit]


def classify_failure(exc: BaseException) -> dict:
    """Name what failed and whether an immediate retry can plausibly help."""
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if isinstance(exc, (requests.exceptions.Timeout, TimeoutError)):
        category, retryable = "timeout", True
    elif isinstance(exc, requests.exceptions.ConnectionError):
        category, retryable = "network", True
    elif isinstance(exc, requests.exceptions.HTTPError):
        category, retryable = "http", bool(status and status >= 500)
    elif isinstance(exc, ValueError):
        # The acceptance's own readback assertions: the provider answered, the result was wrong.
        category, retryable = "contract", False
    else:
        category, retryable = "internal", False
    failure = {"category": category, "retryable": retryable, "error_type": type(exc).__name__,
               "error_message": redact_summary(str(exc))}
    if status is not None:
        failure["http_status"] = status
    body = getattr(response, "text", None)
    if body:
        failure["response_summary"] = redact_summary(body)
    return failure


def adapter_version(provider: str) -> str:
    """The adapter that served the provider, so a receipt is tied to the code that produced it."""
    try:
        if provider == "fred":
            from data_center.ingest.economic import default_fred_connector

            return getattr(default_fred_connector(), "version", "unknown")
        from data_center.connectors.registry import get_connector

        return getattr(get_connector(provider), "version", "unknown")
    except (ImportError, ValueError, KeyError):
        return "unknown"


def archive_receipts(root: Path, keep_days: int = 90):
    if keep_days < 90:
        raise ValueError("evidence retention must be at least 90 days")
    cutoff = time.time() - keep_days * 86400
    archived = []
    for path in root.glob("receipt-*.json"):
        if path.stat().st_mtime >= cutoff:
            continue
        target = root / "archive" / (path.name + ".gz")
        target.parent.mkdir(exist_ok=True)
        data = path.read_bytes()
        if target.exists():
            if gzip.decompress(target.read_bytes()) != data:
                raise ValueError("archive differs from receipt")
        else:
            with target.open("xb") as stream:
                stream.write(gzip.compress(data, mtime=0))
        if gzip.decompress(target.read_bytes()) != data:
            raise ValueError("archive verification failed")
        path.unlink()
        archived.append(target.name)
    return archived


def verify_provider(provider, symbol, asset_class, *, call, envelope, acceptance_id, checked_at, deadline_seconds):
    """Run one provider end to end and return its receipt entry.

    Anything that goes wrong is raised: the caller classifies the failure and decides whether a retry can
    help, so an attempt never silently masks what happened.
    """
    now = checked_at
    result = {}
    if provider == "fred":
        query = {"series_id": symbol, "start": (now - timedelta(days=90)).date().replace(day=1).isoformat(),
                 "end": now.date().isoformat(), "run_scope": "acceptance"}
        submitted = call("POST", "/economic/ingest", params=query)
        read_path = "/economic/observations"
    else:
        query = {"provider": provider, "symbol": symbol, "timeframe": "1d",
                 "start": (now - timedelta(days=14)).isoformat(), "end": now.isoformat()}
        result.update({"requested_range": {"start": query["start"], "end": query["end"],
                                            "semantics": "half-open"},
                       "effective_range": {"start": query["start"], "end": query["end"],
                                           "semantics": "half-open"},
                       "closed_bar_cutoff": now.isoformat(),
                       "requested_bytes_estimate": 14 * 4096,
                       "requested_bytes_estimate_method": "14 daily bars at 4 KiB/bar upper bound"})
        submitted = call("POST", "/ingest/runs", json={**query, "job_id": "acceptance-" + acceptance_id,
                                                        "run_scope": "acceptance",
                          "asset_class": asset_class})
        read_path = "/bars"
    result["run_id"] = submitted["run_id"]
    deadline = time.monotonic() + deadline_seconds
    while True:
        receipt = call("GET", "/runs/" + submitted["run_id"])
        if receipt["status"] in {"pass", "failed", "dead_letter"}:
            break
        if time.monotonic() >= deadline:
            raise TimeoutError("run polling deadline")
        time.sleep(1)
    result["receipt"] = receipt
    quality = receipt.get("quality_summary") or {}
    if receipt["status"] != "pass" or not receipt.get("row_count") or quality.get("status") != "pass":
        raise ValueError("non-pass receipt")
    readback = envelope("GET", read_path, params=query)
    rows = readback["data"]
    if len(rows) < receipt["row_count"]:
        raise ValueError("API readback incomplete")
    started = datetime.fromisoformat(receipt["started_at"])
    fresh = [row for row in rows if datetime.fromisoformat(row["ingest_ts"].replace("Z", "+00:00")) >= started]
    if len(fresh) < receipt["row_count"]:
        raise ValueError("API readback does not include newly ingested rows")
    result.update(status="pass", read_count=len(rows), fresh_read_count=len(fresh),
                  snapshot_id=readback["meta"].get("snapshot_id"),
                  unpaged_readback_hash=_rows_hash(rows))
    if provider != "fred":
        if provider == "dukascopy" and {row.get("price_type") for row in fresh} != {"bid"}:
            raise ValueError("Dukascopy readback is not exclusively BID")
        page_rows, cursor, snapshots = [], None, set()
        while True:
            page_params = {**query, "page_size": 5}
            if cursor:
                page_params["cursor"] = cursor
            page = envelope("GET", read_path, params=page_params)
            page_rows.extend(page["data"])
            snapshots.add(page["meta"].get("snapshot_id"))
            cursor = page["meta"].get("next_cursor")
            if not cursor:
                break
        if page_rows != rows or snapshots != {readback["meta"].get("snapshot_id")}:
            raise ValueError("paged and unpaged readback differ")
        manifest = call("GET", "/runs/" + submitted["run_id"] + "/manifest")
        result.update(price_type="bid" if provider == "dukascopy" else None,
                      paged_read_count=len(page_rows), paged_readback_hash=_rows_hash(page_rows),
                      manifest_run_id=manifest.get("run_id"),
                      manifest_parts=manifest.get("parts", []))
    return result



def run_provider_attempts(provider, symbol, asset_class, *, call, envelope, acceptance_id, checked_at,
                          deadline_seconds, attempts=None, backoff=None):
    """Verify one provider, retrying only failures a retry can plausibly fix.

    Returns the receipt entry (with every attempt recorded) and the final failure, if any. A transient
    timeout or connection error is retried with linear backoff; a contract or data failure is not,
    because repeating it only adds noise to the receipt.
    """
    attempts = _ACCEPTANCE_ATTEMPTS if attempts is None else attempts
    backoff = _ACCEPTANCE_BACKOFF_SECONDS if backoff is None else backoff
    result = {"provider": provider, "symbol": symbol, "adapter_version": adapter_version(provider)}
    recorded = []
    for attempt in range(1, attempts + 1):
        try:
            result.update(verify_provider(provider, symbol, asset_class, call=call, envelope=envelope,
                                          acceptance_id=acceptance_id, checked_at=checked_at,
                                          deadline_seconds=deadline_seconds))
            recorded.append({"attempt": attempt, "status": "pass"})
            result["attempts"] = recorded
            return result, None
        except Exception as exc:  # noqa: BLE001 - acceptance must record every provider failure
            failure = classify_failure(exc)
            recorded.append({"attempt": attempt, **failure})
            if failure["retryable"] and attempt < attempts:
                time.sleep(backoff * attempt)
                continue
            result.update(status="failed", **failure)
            result["attempts"] = recorded
            return result, failure
    raise AssertionError("unreachable: the retry loop always returns")


def run_acceptance(base_url, root, interval_seconds=3600, spacing_seconds=5, deadline_seconds=480):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(root, 0o700)
    except OSError:
        pass
    with (root / ".lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"status": "skipped", "reason": "acceptance_busy"}
        last = root / "last-attempt"
        if last.exists() and time.time() - float(last.read_text()) < interval_seconds:
            return {"status": "skipped", "reason": "minimum_interval"}
        last.write_text(str(time.time()))
        settings = Settings()
        capacity_before = settings.capacity_policy().inspect(settings.canonical_root).as_dict()
        session = requests.Session()
        session.trust_env = False
        if os.getenv("DATACENTER_API_KEY"):
            session.headers["X-API-Key"] = os.environ["DATACENTER_API_KEY"]

        def envelope(method, path, **kwargs):
            response = session.request(method, base_url.rstrip("/") + "/api/v1" + path, timeout=10, **kwargs)
            response.raise_for_status()
            return response.json()

        def call(method, path, **kwargs):
            return envelope(method, path, **kwargs)["data"]

        now = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        report = {"acceptance_id": uuid4().hex, "checked_at": datetime.now(timezone.utc).isoformat(),
                  "validation_command": "python -m data_center.acceptance", "base_url": base_url,
                  "environment": evidence_context(),
                  "capacity_before": capacity_before,
                  "status": "pass", "providers": []}
        providers = (("binance", "BTCUSDT", "crypto"), ("yfinance", "SPY", "etf"),
                     ("dukascopy", "EURUSD", "fx"), ("fred", "PAYEMS", None))
        for index, (provider, symbol, asset_class) in enumerate(providers):
            if index:
                time.sleep(spacing_seconds)
            result, failure = run_provider_attempts(
                provider, symbol, asset_class, call=call, envelope=envelope,
                acceptance_id=report["acceptance_id"], checked_at=now, deadline_seconds=deadline_seconds)
            if failure is not None:
                report["status"] = "failed"
            report["providers"].append(result)
        report["capacity_after"] = settings.capacity_policy().inspect(settings.canonical_root).as_dict()
        path = root / ("receipt-" + report["acceptance_id"] + ".json")
        path.write_text(json.dumps(report, indent=2))
        if report["status"] != "pass":
            settings = Settings()
            AlertSink(settings.evidence_root / "alerts", settings.alerts_enabled).emit(
                "provider_acceptance_failed", identity=report["acceptance_id"],
                fields={"receipt": str(path), "status": "failed",
                        "providers": [r["provider"] for r in report["providers"] if r["status"] != "pass"]})
            with (root / "alerts.jsonl").open("a") as stream:
                stream.write(json.dumps({"event": "provider_acceptance_failed", "at": report["checked_at"],
                                         "receipt": path.name, "providers": [r["provider"] for r in report["providers"] if r["status"] != "pass"]}) + "\n")
        report["archived_receipts"] = archive_receipts(root)
        session.close()
        return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:18380")
    parser.add_argument("--output", type=Path, default=Settings().evidence_root / "acceptance")
    parser.add_argument("--interval-seconds", type=float, default=3600)
    args = parser.parse_args()
    report = run_acceptance(args.base_url, args.output, args.interval_seconds)
    print(json.dumps({key: value for key, value in report.items() if key != "providers"}), flush=True)
    raise SystemExit(1 if report["status"] == "failed" else 0)


if __name__ == "__main__":
    main()
