# Operations runbook

## Readiness and queue

```bash
curl -fsS http://127.0.0.1:18380/api/v1/health/ready
curl -fsS http://127.0.0.1:18380/api/v1/metrics
systemctl --user is-active market-data-center-api.service market-data-center-worker.service
```

If readiness is not `ready`, inspect the worker heartbeat and queue metrics first. Restart the API and worker together only after confirming the ledger and canonical root are the same configured paths.

## Economic PIT and parity

Use `mode=current` for current-state reads. PIT reads require an explicit `asof_ts`:

```bash
curl -fsS 'http://127.0.0.1:18380/api/v1/economic/observations?provider=fred&series_id=PAYEMS&mode=pit&asof_ts=2026-09-10T00:00:00Z'
PYTHONPATH=backend/src python scripts/economic_parity.py --provider fred --series-id PAYEMS \
  --start 2026-01-01 --end 2026-08-01 --output /path/to/evidence/parity.json
```

Only set `MACRO_MARKET_USE_DATA_CENTER_ECONOMIC=1` after the parity receipt has `status=pass` and `migration_status=ready`. Remove the flag to roll back the macro consumer to its legacy local reader.

## Backup and recovery

Backups include published canonical files and the ledger, exclude live staging, and verify SHA-256 bytes on restore:

```bash
PYTHONPATH=backend/src python -m data_center.operations backup --destination /path/to/evidence/backup.tar.gz
PYTHONPATH=backend/src python -m data_center.operations recovery-drill --destination /path/to/evidence/recovery-drill
```

Restore fails closed if an existing target differs. Never delete or rewrite canonical parts to recover a consumer; disable the feature flag and restore the previous reader.

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

Keep parity, provider acceptance, backup/recovery, cleanup, and cutover reports under the protected evidence root for at least 90 days. Each report must include commit, environment, command, time range, result, and failure reason where applicable.
