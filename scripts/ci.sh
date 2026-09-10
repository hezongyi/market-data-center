#!/usr/bin/env bash
set -euo pipefail
repo_dir="$(cd "$(dirname "$0")/.." && pwd)"
cd "$repo_dir"
scope="${1:-all}"
if [[ "$scope" != "all" && "$scope" != "backend" && "$scope" != "web" ]]; then
  echo "usage: scripts/ci.sh [all|backend|web]" >&2
  exit 2
fi
task_python="${DATACENTER_PYTHON:-$repo_dir/.venv/bin/python}"
if [[ "$task_python" == */* && "$task_python" != /* ]]; then
  task_python="$repo_dir/$task_python"
fi
started_at="$("$task_python" -c 'from datetime import datetime, timezone; print(datetime.now(timezone.utc).isoformat())')"
receipt="${DATACENTER_CI_RECEIPT:-$repo_dir/acceptance-receipts/ci/$scope.json}"
receipt_action="${DATACENTER_CI_ACTION:-ci}"
write_ci_receipt() {
  status=$?
  trap - EXIT
  result=pass
  if [[ $status -ne 0 ]]; then result=failed; fi
  "$task_python" scripts/ci_receipt.py --result "$result" --scope "$scope" --action "$receipt_action" --started-at "$started_at" --output "$receipt" || true
  exit "$status"
}
trap write_ci_receipt EXIT

run_backend() {
  (
    cd "$repo_dir/backend"
    "$task_python" -m ruff check src tests
  )
  "$task_python" -m ruff check scripts
  "$task_python" -m pip check
  PYTHONPATH="$repo_dir/backend/src" "$task_python" scripts/compatibility_check.py
  "$task_python" scripts/dependency_lock_check.py
  "$task_python" -m pytest -q backend/tests
  "$task_python" scripts/secret_scan.py
  PYTHONPATH="$repo_dir/backend/src" "$task_python" scripts/operations_acceptance.py
}

run_web() {
  npm --prefix webui ci
  npm --prefix webui run build
  PYTHONPATH="$repo_dir/backend/src" DATACENTER_PYTHON="$task_python" npm --prefix webui run test:e2e
  PYTHONPATH="$repo_dir/backend/src" "$task_python" scripts/service_acceptance.py
}

if [[ "$scope" == "all" || "$scope" == "backend" ]]; then run_backend; fi
if [[ "$scope" == "all" || "$scope" == "web" ]]; then run_web; fi
