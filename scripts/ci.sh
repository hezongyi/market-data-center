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
  PYTHONPATH="$repo_dir/backend/src" "$task_python" -m pytest -q backend/tests
  PYTHONPATH="$repo_dir/backend/src" "$task_python" scripts/secret_scan.py
  PYTHONPATH="$repo_dir/backend/src" "$task_python" scripts/operations_acceptance.py
  PYTHONPATH="$repo_dir/backend/src" "$task_python" scripts/operational_snapshot_benchmark.py
}

run_web() {
  if ! command -v node >/dev/null 2>&1; then
    echo "Node 22 is required for Web and browser acceptance." >&2
    return 1
  fi
  node_major="$(node -p 'process.versions.node.split(".")[0]')"
  if [[ "$node_major" != "22" ]]; then
    echo "Node 22 is required; found $(node --version)." >&2
    return 1
  fi
  npm --prefix webui ci
  npm --prefix webui run build
  if ! node -e "require.resolve('playwright', {paths: [process.cwd() + '/webui']})"; then
    echo "Locked Playwright package is unavailable. Run: npm --prefix webui ci" >&2
    return 1
  fi
  if [[ -z "${PLAYWRIGHT_BROWSER_EXECUTABLE:-}" ]]; then
    PLAYWRIGHT_BROWSER_EXECUTABLE="$(find "${HOME}/.cache/ms-playwright" -type f -path '*/chrome-linux/chrome' 2>/dev/null | sort -V | tail -1 || true)"
    export PLAYWRIGHT_BROWSER_EXECUTABLE
  fi
  if [[ -n "${PLAYWRIGHT_BROWSER_EXECUTABLE:-}" && -x "$PLAYWRIGHT_BROWSER_EXECUTABLE" ]]; then
    export LD_LIBRARY_PATH="${HOME}/.local/share/playwright-deps-jammy/usr/lib/x86_64-linux-gnu${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
  else
    echo "Playwright Chromium is unavailable. Discover cache with: find \"\$HOME/.cache/ms-playwright\" -type f -path '*/chrome-linux/chrome'" >&2
    echo "Or install it with: npx --prefix webui playwright install chromium" >&2
    return 1
  fi
  PYTHONPATH="$repo_dir/backend/src" DATACENTER_PYTHON="$task_python" npm --prefix webui run test:e2e
  PYTHONPATH="$repo_dir/backend/src" "$task_python" scripts/service_acceptance.py
}

if [[ "$scope" == "all" || "$scope" == "backend" ]]; then run_backend; fi
if [[ "$scope" == "all" || "$scope" == "web" ]]; then run_web; fi
