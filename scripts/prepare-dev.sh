#!/usr/bin/env bash
# Explicit dependency preparation; checks never reinstall dependencies.
set -euo pipefail
repo_dir="$(cd "$(dirname "$0")/.." && pwd)"
cd "$repo_dir"
scope="${1:-all}"
case "$scope" in backend|web|all) ;; *) echo 'usage: scripts/prepare-dev.sh [backend|web|all]' >&2; exit 2 ;; esac
if [[ "$scope" == backend || "$scope" == all ]]; then
  task_python="${DATACENTER_PYTHON:-$repo_dir/.venv/bin/python}"
  if [[ ! -x "$task_python" ]]; then
    if [[ -n "${DATACENTER_PYTHON:-}" ]]; then
      echo 'DATACENTER_PYTHON must name an existing Python environment' >&2
      exit 2
    fi
    python3.11 -m venv "$repo_dir/.venv"
  fi
  suffix="$("$task_python" -c 'import sys; print(f"{sys.version_info.major}{sys.version_info.minor}")')"
  case "$suffix" in 310|311|312) ;; *) echo 'Python 3.10, 3.11 or 3.12 is required' >&2; exit 2 ;; esac
  "$task_python" -m pip install -c "backend/constraints/py$suffix.txt" -e './backend[dev]'
fi
if [[ "$scope" == web || "$scope" == all ]]; then
  test "$(node -p 'process.versions.node.split(".")[0]')" = 22 || {
    echo 'Node 22 is required' >&2; exit 2;
  }
  npm --prefix webui ci --include=dev
fi
