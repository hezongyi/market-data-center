"""Isolated ingest entry point. Only the supervisor publishes canonical parts."""
import json
import sys
from pathlib import Path

from data_center.domain.models import IngestJob
from data_center.ingest.economic import run_fred_ingest
from data_center.ingest.service import run_fixture_ingest


def main():
    directory = Path(sys.argv[1])
    request = json.loads((directory / "request.json").read_text())
    job = request["payload"]
    try:
        if job["dataset_id"] == "economic_observations":
            receipt = run_fred_ingest(series_id=job["series_id"], root=directory / "parts",
                                      start=job.get("start"), end=job.get("end"), run_id=request["run_id"])
        else:
            receipt = run_fixture_ingest(IngestJob.model_validate(job), directory / "parts", run_id=request["run_id"])
        result = {"receipt": receipt}
    except Exception as exc:
        # Provider exception URLs can contain API keys. Persist only the category.
        result = {"error_type": type(exc).__name__, "error": "ingest failed", "retryable": not isinstance(exc, (ValueError, KeyError, ModuleNotFoundError))}
    temporary = directory / "result.tmp"
    temporary.write_text(json.dumps(result))
    temporary.replace(directory / "result.json")


if __name__ == "__main__":
    main()
