"""Isolated ingest entry point. Only the supervisor publishes canonical parts."""
import json
import sys
from pathlib import Path

from data_center.catalog.snapshot import Catalog
from data_center.domain.models import DeriveJob, IngestJob
from data_center.ingest.economic import run_fred_ingest
from data_center.ingest.service import run_fixture_ingest
from data_center.platform_registry import REGISTRY
from data_center.quality.errors import QualityError
from data_center.transform import TransformExecutor


def safe_failure_result(exc: Exception) -> dict:
    """Convert provider failures to the exact path-safe payload persisted by workers."""
    if isinstance(exc, QualityError):
        return {"error_type": "QualityError", "failure_stage": "quality", "error": "quality checks failed",
                "retryable": False,
                "quality_summary": {"status": "fail", "finding_count": len(exc.findings),
                                    "findings": exc.findings}}
    return {"error_type": type(exc).__name__, "failure_stage": "execute", "error": "ingest failed",
            "retryable": not isinstance(exc, (ValueError, KeyError, ModuleNotFoundError)),
            "quality_summary": {"status": "not_run", "finding_count": 0, "findings": []}}


def main():
    directory = Path(sys.argv[1])
    request = json.loads((directory / "request.json").read_text())
    job = request["payload"]
    try:
        if job.get("run_kind") == "derive":
            derive_job = DeriveJob.model_validate(job)
            recipe = REGISTRY.recipe(derive_job.recipe_id, derive_job.recipe_version)
            snapshot = Catalog(Path(request["canonical_root"])).resolve(
                recipe.input_dataset,
                {"provider": derive_job.provider, "symbol": derive_job.symbol,
                 "timeframe": recipe.source_timeframe},
            )
            if snapshot.snapshot_id != derive_job.input_snapshot_id:
                raise ValueError("derive input snapshot changed after submission")
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
                                      run_kind=job.get("run_kind", "ingest"), run_scope=job.get("run_scope", "production"))
        else:
            receipt = run_fixture_ingest(IngestJob.model_validate(job), directory / "parts", run_id=request["run_id"],
                                         execution_plan=job.get("execution_plan"))
        result = {"receipt": receipt}
    except Exception as exc:  # noqa: BLE001 - child must convert every failure to a safe result
        # Provider exception URLs can contain API keys. Persist only the category.
        result = safe_failure_result(exc)
    temporary = directory / "result.tmp"
    temporary.write_text(json.dumps(result))
    temporary.replace(directory / "result.json")


if __name__ == "__main__":
    main()
