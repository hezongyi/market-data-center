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

## Acceptance evidence

Keep parity, provider acceptance, backup/recovery, cleanup, and cutover reports under the protected evidence root for at least 90 days. Each report must include commit, environment, command, time range, result, and failure reason where applicable.
