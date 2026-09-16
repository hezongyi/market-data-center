#!/usr/bin/env bash
set -euo pipefail
node_major="$(node -p 'process.versions.node.split(".")[0]')"
if [[ "$node_major" != 22 ]]; then echo 'Node 22 is required' >&2; exit 1; fi
if ! node -e "require.resolve('playwright', {paths: [process.cwd() + '/webui']})"; then
  echo 'Run bash scripts/prepare-dev.sh web first' >&2; exit 1
fi
if [[ -z "${PLAYWRIGHT_BROWSER_EXECUTABLE:-}" ]]; then
  PLAYWRIGHT_BROWSER_EXECUTABLE="$(find "${HOME}/.cache/ms-playwright" -type f \
    \( -path '*/chrome-linux/chrome' -o -path '*/chrome-linux64/chrome' \
       -o -path '*/chrome-headless-shell-linux64/chrome-headless-shell' \) \
    2>/dev/null | sort -V | tail -1 || true)"
  export PLAYWRIGHT_BROWSER_EXECUTABLE
fi
if [[ -z "${PLAYWRIGHT_BROWSER_EXECUTABLE:-}" || ! -x "$PLAYWRIGHT_BROWSER_EXECUTABLE" ]]; then
  echo 'Run npx --prefix webui playwright install chromium first' >&2; exit 1
fi
export LD_LIBRARY_PATH="${HOME}/.local/share/playwright-deps-jammy/usr/lib/x86_64-linux-gnu${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
exec "$@"
