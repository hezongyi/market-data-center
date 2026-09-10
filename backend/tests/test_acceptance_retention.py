import gzip
import os
import time

import pytest

from data_center.acceptance import archive_receipts, run_acceptance
from data_center.operations import (
    create_backup,
    daily_chunks,
    recovery_drill,
    restore_backup,
    retention_audit,
)


def test_old_receipts_are_losslessly_archived(tmp_path):
    old = tmp_path / 'receipt-old.json'
    old.write_text('{"status":"failed"}')
    data = old.read_bytes()
    os.utime(old, (time.time() - 91 * 86400,) * 2)
    fresh = tmp_path / 'receipt-new.json'
    fresh.write_text('{}')
    other = tmp_path / 'ledger.sqlite'
    other.write_text('do not touch')
    assert archive_receipts(tmp_path) == ['receipt-old.json.gz']
    assert gzip.decompress((tmp_path / 'archive/receipt-old.json.gz').read_bytes()) == data
    assert not old.exists()
    assert fresh.exists() and other.exists()


def test_acceptance_minimum_interval_skips_network(tmp_path):
    (tmp_path / 'last-attempt').write_text(str(time.time()))
    assert run_acceptance('http://127.0.0.1:1', tmp_path)['status'] == 'skipped'


def test_acceptance_unreachable_service_persists_alert(tmp_path):
    import json
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        report = run_acceptance(f"http://127.0.0.1:{sock.getsockname()[1]}", tmp_path, spacing_seconds=0)
    assert report["status"] == "failed"
    alert = json.loads((tmp_path / "alerts.jsonl").read_text())
    assert set(alert["providers"]) == {"binance", "yfinance", "fred"}
    assert json.loads((tmp_path / alert["receipt"]).read_text())["status"] == "failed"


def test_retention_audit_does_not_change_data(tmp_path):
    part = tmp_path / "part.parquet"
    part.write_bytes(b"unchanged")
    os.utime(part, (time.time() - 40 * 86400,) * 2)
    report = retention_audit(tmp_path)
    assert report["canonical"]["old_files"] == 1
    assert report["capacity"]["total_bytes"] >= report["capacity"]["used_bytes"]
    assert part.read_bytes() == b"unchanged"


def test_daily_backfill_chunks_are_bounded_and_contiguous():
    from datetime import date
    from itertools import pairwise

    import pytest

    chunks = list(daily_chunks(date(2024, 1, 1), date(2026, 1, 1)))
    assert chunks[0][0] == date(2024, 1, 1) and chunks[-1][1] == date(2026, 1, 1)
    assert all(0 < (end - start).days <= 365 for start, end in chunks)
    assert all(left[1] == right[0] for left, right in pairwise(chunks))
    with pytest.raises(ValueError):
        list(daily_chunks(date(2026, 1, 1), date(2024, 1, 1)))


def test_completed_backfill_receipt_is_idempotent_without_network(tmp_path):
    import json
    from datetime import date

    from data_center.operations import backfill

    output = tmp_path / "backfill.json"
    output.write_text(json.dumps({"status": "pass", "provider": "fixture", "symbol": "TEST", "runs": []}))
    assert backfill("http://127.0.0.1:1", "fixture", "TEST", "crypto",
                     date(2026, 1, 1), date(2026, 1, 2), output)["status"] == "pass"


def test_backfill_resume_rejects_different_request(tmp_path):
    import json
    from datetime import date

    import pytest

    from data_center.operations import backfill

    output = tmp_path / "backfill.json"
    output.write_text(json.dumps({"status": "failed", "provider": "fixture", "symbol": "OTHER", "runs": []}))
    with pytest.raises(ValueError, match="different request"):
        backfill("http://127.0.0.1:1", "fixture", "TEST", "crypto",
                 date(2026, 1, 1), date(2026, 1, 2), output)


def test_backup_restore_is_verified_and_never_overwrites_conflicts(tmp_path):
    root = tmp_path / "canonical"
    ledger = root / "audit" / "ledger.sqlite"
    part = root / "provider_bars" / "part.parquet"
    part.parent.mkdir(parents=True)
    ledger.parent.mkdir(parents=True, exist_ok=True)
    part.write_bytes(b"canonical-bytes")
    ledger.write_bytes(b"ledger-bytes")
    (root / ".ingest-staging" / "running" / "part.parquet").parent.mkdir(parents=True)
    (root / ".ingest-staging" / "running" / "part.parquet").write_bytes(b"live")
    archive = tmp_path / "backup.tar.gz"
    report = create_backup(root, ledger, archive)
    assert report["status"] == "pass" and report["file_count"] == 2
    restored = tmp_path / "restored"
    restored_ledger = restored / "audit" / "data_center.sqlite"
    assert restore_backup(archive, restored, restored_ledger)["restored"] == 2
    assert (restored / "provider_bars/part.parquet").read_bytes() == b"canonical-bytes"
    assert (restored / "audit/ledger.sqlite").read_bytes() == b"ledger-bytes"
    with pytest.raises(ValueError, match="differs"):
        (restored / "provider_bars/part.parquet").write_bytes(b"changed")
        restore_backup(archive, restored, restored_ledger)
    drill = recovery_drill(root, ledger, tmp_path / "drill")
    assert drill["status"] == "pass" and drill["file_count"] == 2


def test_backup_rejects_ledger_outside_canonical_root(tmp_path):
    with pytest.raises(ValueError, match="inside"):
        create_backup(tmp_path / "canonical", tmp_path / "outside.sqlite", tmp_path / "backup.tar.gz")
