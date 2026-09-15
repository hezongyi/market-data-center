"""Isolated ingest entry point. Only the supervisor publishes canonical parts."""
import json
import sqlite3
import sys
from pathlib import Path

from data_center.catalog.snapshot import (
    Catalog,
    CatalogSnapshot,
    snapshot_from_reference,
)
from data_center.domain.errors import (
    InputUnavailableError,
    ProviderGapError,
    SessionClosedError,
)
from data_center.domain.models import DeriveJob, IngestJob
from data_center.ingest.economic import run_fred_ingest
from data_center.ingest.service import run_fixture_ingest
from data_center.platform_registry import REGISTRY
from data_center.quality.errors import QualityError
from data_center.quality.verification import (
    run_parity_verification,
    run_quality_verification,
)
from data_center.transform import TransformExecutor


def _fixed_input(request: dict, job: dict, derive_job: DeriveJob, recipe) -> CatalogSnapshot:
    """Rebuild the accepted input, never the current one.

    A run submitted before fixed inputs existed has no reference and keeps the
    old comparison so its failure mode is unchanged; every new run carries the
    parts it was accepted against and is rebuilt from them, so publishing more
    raw data cannot turn a queued derivation into a permanent failure.
    """
    root = Path(request["canonical_root"])
    reference = _stored_input(request, job)
    if reference is not None:
        return snapshot_from_reference(root, reference)
    snapshot = Catalog(root).resolve(
        recipe.input_dataset,
        {"provider": derive_job.provider, "symbol": derive_job.symbol,
         "timeframe": recipe.source_timeframe},
    )
    if snapshot.snapshot_id != derive_job.input_snapshot_id:
        raise InputUnavailableError("derive input snapshot changed after submission")
    return snapshot


def _stored_input(request: dict, job: dict) -> dict | None:
    """Read the run's fixed input reference straight from the ledger, read-only.

    The child never migrates or writes the ledger: it opens the file in
    read-only mode for one lookup, so reconstructing an input cannot contend
    with the supervisor's writes.
    """
    input_id = job.get("input_id")
    if not input_id:
        return None
    path = request.get("ledger_path")
    if not path:
        raise InputUnavailableError("the run carries a fixed input but no ledger to read it from")
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
            row = connection.execute("select payload from production_inputs where input_id=?",
                                     (str(input_id),)).fetchone()
    except sqlite3.Error as exc:
        raise InputUnavailableError("the fixed input store is unreadable") from exc
    if row is None:
        raise InputUnavailableError(f"fixed input reference is not stored: {input_id}")
    return json.loads(row[0])


def safe_failure_result(exc: Exception, job: dict | None = None) -> dict:
    """Convert provider failures to the exact path-safe payload persisted by workers."""
    if isinstance(exc, QualityError):
        # A provider can omit bars from an otherwise valid response.  Production
        # and maintenance plans persist that exact gap and retry it on the
        # governed cooldown; spending the worker's short retry budget only
        # creates three identical requests and a misleading dead letter.
        # Structural/schema quality failures remain terminal too.
        return {"error_type": "QualityError", "failure_stage": "quality", "error": "quality checks failed",
                "retryable": False,
                "quality_summary": {"status": "fail", "finding_count": len(exc.findings),
                                    "findings": exc.findings}}
    if isinstance(exc, SessionClosedError):
        # A window the session profile keeps closed owes no output, so retrying
        # the same window cannot change the answer.  Report it once, as an
        # expected condition, instead of spending its attempts on the
        # dead-letter queue.
        return {"error_type": "SessionClosedError", "failure_stage": "input",
                "error": "the session profile keeps the requested window closed", "retryable": False,
                "quality_summary": {"status": "not_run", "finding_count": 0, "findings": []}}
    if isinstance(exc, ProviderGapError):
        # A production plan owns the slower, durable gap cooldown. Other scopes
        # retain the worker's transient retry behavior because they have no plan
        # progress in which to persist the debt.
        governed = (job or {}).get("run_scope") in {"maintenance", "production"}
        return {"error_type": "ProviderGapError", "failure_stage": "execute",
                "error": "ingest failed", "retryable": not governed,
                "quality_summary": {"status": "not_run", "finding_count": 0, "findings": []}}
    if isinstance(exc, InputUnavailableError):
        # Stopped and reported, never retried into a different computation.
        return {"error_type": "InputUnavailableError", "failure_stage": "input",
                "error": "input unavailable", "retryable": False,
                "quality_summary": {"status": "not_run", "finding_count": 0, "findings": []}}
    return {"error_type": type(exc).__name__, "failure_stage": "execute", "error": "ingest failed",
            "retryable": isinstance(exc, ProviderGapError) or not isinstance(exc, (ValueError, KeyError, ModuleNotFoundError)),
            "quality_summary": {"status": "not_run", "finding_count": 0, "findings": []}}


def main():
    directory = Path(sys.argv[1])
    request = json.loads((directory / "request.json").read_text())
    job = request["payload"]
    result: dict = {}
    try:
        if job.get("run_kind") in {"quality", "parity"}:
            # Verification runs report findings and publish no canonical part,
            # so the supervisor must not open the publication path for them.
            verifier = run_parity_verification if job.get("run_kind") == "parity" else run_quality_verification
            result = {"verification": verifier(job=job, root=Path(request["canonical_root"]),
                                               run_id=request["run_id"])}
        else:
            if job.get("run_kind") == "derive":
                derive_job = DeriveJob.model_validate(job)
                recipe = REGISTRY.recipe(derive_job.recipe_id, derive_job.recipe_version)
                snapshot = _fixed_input(request, job, derive_job, recipe)
                receipt = TransformExecutor().derive(
                    recipe=recipe, input_snapshot=snapshot,
                    selector={"provider": derive_job.provider, "symbol": derive_job.symbol},
                    start=derive_job.start, end=derive_job.end, root=directory / "parts",
                    run_id=request["run_id"], run_scope=derive_job.run_scope,
                )
                receipt["job_id"] = derive_job.job_id
            elif job["dataset_id"] == "economic_observations":
                receipt = run_fred_ingest(series_id=job["series_id"], root=directory / "parts",
                                          start=job.get("start"), end=job.get("end"), run_id=request["run_id"],
                                          schema_version=job.get("schema_version", "economic_observations.v2"),
                                          run_kind=job.get("run_kind", "ingest"),
                                          run_scope=job.get("run_scope", "production"))
            else:
                receipt = run_fixture_ingest(IngestJob.model_validate(job), directory / "parts",
                                             run_id=request["run_id"],
                                             execution_plan=job.get("execution_plan"))
            result = {"receipt": receipt}
    except Exception as exc:  # noqa: BLE001 - child must convert every failure to a safe result
        # Provider exception URLs can contain API keys. Persist only the category.
        result = safe_failure_result(exc, job)
    temporary = directory / "result.tmp"
    temporary.write_text(json.dumps(result))
    temporary.replace(directory / "result.json")


if __name__ == "__main__":
    main()
