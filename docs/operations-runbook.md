# Operations runbook

## Readiness and queue

```bash
curl -fsS http://127.0.0.1:18380/api/v1/health/ready
curl -fsS http://127.0.0.1:18380/api/v1/metrics
systemctl --user is-active market-data-center-api.service market-data-center-worker.service
```

If readiness is not `ready`, inspect the worker heartbeat and queue metrics first. Restart the API and worker together only after confirming the ledger and canonical root are the same configured paths.

Readiness separates `read_status`, `write_status`, and `capacity_status`. `capacity_status=warning` keeps ordinary ingest available but blocks unattended backfills over 31 days. `capacity_status=critical` returns `507 capacity_protected` for new ingest while reads and restore remain available.

## Capacity response

Default free-space thresholds are warning 15% and critical 10%; production may configure stricter values with `DATACENTER_CAPACITY_WARNING_FREE_RATIO` and `DATACENTER_CAPACITY_CRITICAL_FREE_RATIO`.

1. Run `python -m data_center.operations retention-audit` and retain the capacity receipt.
2. At warning, pause broad backfills and schedule expansion or archival to a separately governed destination.
3. At critical, keep ingest paused; do not delete canonical parts, manifests, terminal receipts, or ledger rows.
4. Expand the filesystem or move approved archives under an explicit maintenance change. Automatic canonical cleanup is forbidden.
5. Run backup verify and an isolated recovery drill, then confirm capacity is above the configured warning threshold before resuming writes.
6. Re-run the monitor; the stable capacity event ID prevents duplicate webhook delivery for the same active condition.

## Economic PIT and parity

Use `mode=current` for current-state reads. PIT reads require an explicit `asof_ts`:

```bash
curl -fsS 'http://127.0.0.1:18380/api/v1/economic/observations?provider=fred&series_id=PAYEMS&mode=pit&asof_ts=2026-09-10T00:00:00Z'
PYTHONPATH=backend/src python scripts/economic_parity.py --provider fred --series-id PAYEMS \
  --start 2026-01-01 --end 2026-08-01 --output /path/to/evidence/parity.json
```

Only set `MACRO_MARKET_USE_DATA_CENTER_ECONOMIC=1` after the parity receipt has `status=pass` and `migration_status=ready`. Remove the flag to roll back the macro consumer to its legacy local reader.

## Backup and recovery

Backups include published canonical files and the ledger, exclude live staging, and verify size/SHA-256 bytes. Production destinations must be outside canonical root and on a distinct mount or equivalent failure domain:

```bash
PYTHONPATH=backend/src python -m data_center.operations backup --destination /independent-mount/backup.tar.gz
PYTHONPATH=backend/src python -m data_center.operations verify --archive /independent-mount/backup.tar.gz
PYTHONPATH=backend/src python -m data_center.operations recovery-drill --destination /independent-mount/recovery-drill
```

Backup v2 writes a same-directory `.partial`, fsyncs, verifies metadata and hashes, then publishes without overwrite. Restore streams through `.restore.partial`, verifies size/hash, and publishes without overwrite. Existing identical bytes are skipped; different bytes fail closed. Temporary artifacts are never valid archives. Backup v1 remains restorable.

`--allow-same-device` is only for isolated development drills and cannot be used as the sole production recovery copy. Never delete or rewrite canonical parts to recover a consumer; disable the feature flag and restore the previous reader.

## Alerts

The monitor writes idempotent events to the local alert outbox. Configure `DATACENTER_ALERT_WEBHOOK_URL` to deliver them. Delivery uses the event ID as `Idempotency-Key`, retries transient failures, and records undelivered events for the next monitor run. Alert delivery failure must not block ingest.

Query operations must monitor `data.query.catalog_snapshot_refresh_total`, cache hit/miss counts,
`duration_seconds`, rows scanned/returned and `rejected_oversized_query_total`. A rising rejected count means
a consumer is sending `page_size > 10000`; sustained unbounded query warnings require migrating that caller to
explicit pagination. Query logs include `request_id`, dataset, selector hash, snapshot ID, mode, page size and
duration, but never include the API key or complete selector.

## Query scalability acceptance

Run the governed benchmark on the acceptance host. It creates an isolated 1,000-part/1,000,000-row publication,
does not modify production canonical data, and writes a receipt with commit, host, dataset size, cold latency,
warm P95, extra peak RSS and output hash:

```bash
PYTHONPATH=backend/src python scripts/query_benchmark.py \
  --output /home/quant/market_lake/evidence/data-center/query-benchmark.json
```

The receipt passes only when a 1,000-row query has warm P95 below one second, cold latency below three seconds,
and additional peak RSS below 512 MiB. Run `scripts/query_pagination_acceptance.py` after deploying the same
commit to prove paged/unpaged and `macro-market-lab` consumer parity on production fixtures.

## Acceptance evidence

Keep CI, dependency refresh, browser acceptance, capacity, parity, provider acceptance, backup/verify/recovery, cleanup, release, and cutover reports under the protected evidence root for at least 90 days. Release receipts and compatibility matrices are retained for the lifetime of the release. Each structured receipt includes commit, environment, action/command, start/completion time, software version, result, failure stage, and safe error category.
