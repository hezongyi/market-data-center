#!/usr/bin/env bash
set -euo pipefail
repo_dir="$(cd "$(dirname "$0")/.." && pwd)"
cd "$repo_dir"
task_python="${DATACENTER_PYTHON:-$repo_dir/.venv/bin/python}"
if [[ "$task_python" == */* && "$task_python" != /* ]]; then
  task_python="$repo_dir/$task_python"
fi
(
  cd "$repo_dir/backend"
  "$task_python" -m ruff check src tests
)
"$task_python" -m ruff check scripts/secret_scan.py scripts/compatibility_check.py scripts/economic_parity.py \
  scripts/operations_acceptance.py scripts/query_benchmark.py scripts/query_pagination_acceptance.py
"$task_python" -m pip check
PYTHONPATH="$repo_dir/backend/src" "$task_python" scripts/compatibility_check.py
"$task_python" -m pytest -q backend/tests
"$task_python" scripts/secret_scan.py
PYTHONPATH="$repo_dir/backend/src" "$task_python" scripts/operations_acceptance.py
npm --prefix webui ci
npm --prefix webui run build
"$task_python" scripts/service_acceptance.py
