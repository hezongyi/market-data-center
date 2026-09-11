"""Isolated ingest entry point. Only the supervisor publishes canonical parts."""
import json
import sys
from pathlib import Path

from data_center.domain.models import IngestJob
from data_center.ingest.economic import run_fred_ingest
from data_center.ingest.service import run_fixture_ingest
from data_center.quality.errors import QualityError


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
        if job["dataset_id"] == "economic_observations":
            receipt = run_fred_ingest(series_id=job["series_id"], root=directory / "parts",
                                      start=job.get("start"), end=job.get("end"), run_id=request["run_id"],
                                      schema_version=job.get("schema_version", "economic_observations.v2"))
        else:
            receipt = run_fixture_ingest(IngestJob.model_validate(job), directory / "parts", run_id=request["run_id"])
        result = {"receipt": receipt}
    except Exception as exc:  # noqa: BLE001 - child must convert every failure to a safe result
        # Provider exception URLs can contain API keys. Persist only the category.
        result = safe_failure_result(exc)
    temporary = directory / "result.tmp"
    temporary.write_text(json.dumps(result))
    temporary.replace(directory / "result.json")


if __name__ == "__main__":
    main()
