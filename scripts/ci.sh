#!/usr/bin/env bash
set -euo pipefail
repo_dir="$(cd "$(dirname "$0")/.." && pwd)"
cd "$repo_dir"
task_python="${DATACENTER_PYTHON:-$repo_dir/.venv/bin/python}"
if [[ "${1:-all}" == docs && -z "${DATACENTER_PYTHON:-}" && ! -x "$task_python" ]]; then
  task_python=python3
fi
exec "$task_python" scripts/ci_runner.py "$@"
