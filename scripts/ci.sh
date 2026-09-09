#!/usr/bin/env bash
set -euo pipefail
repo_dir="$(cd "$(dirname "$0")/.." && pwd)"
cd "$repo_dir"
task_python="${DATACENTER_PYTHON:-$repo_dir/.venv/bin/python}"
"$task_python" -m pytest -q backend/tests
"$task_python" scripts/secret_scan.py
npm --prefix webui ci
npm --prefix webui run build
"$task_python" scripts/service_acceptance.py
