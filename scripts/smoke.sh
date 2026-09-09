#!/usr/bin/env bash
set -euo pipefail

base_url="${DATACENTER_SMOKE_URL:-http://127.0.0.1:18380}"
curl -fsS "$base_url/api/v1/health" >/dev/null
curl -fsS "$base_url/api/v1/datasets" >/dev/null
curl -fsS "$base_url/" >/dev/null
echo "market-data-center smoke: pass"
