import json
from datetime import datetime, timezone
from pathlib import Path
from threading import Thread

import pytest

from data_center.catalog.manifest import (
    PublicationError,
    file_hash,
    immutable_json,
    manifest_path,
)
from data_center.domain.models import IngestJob
from data_center.ingest.worker import LocalWorker
from data_center.maintenance import cleanup_staging
from data_center.runs.ledger import RunLedger
from data_center.storage.query import query_provider_bars


def setup_run(tmp_path):
    ledger = RunLedger(tmp_path / 'ledger.sqlite')
    worker = LocalWorker(tmp_path / 'lake', ledger)
    run_id = worker.submit(IngestJob(job_id='multi-part', symbol='TEST',
                                    start=datetime(2025, 12, 31, tzinfo=timezone.utc),
                                    end=datetime(2026, 1, 1, tzinfo=timezone.utc)))
    return ledger, worker, run_id


def read(root):
    return query_provider_bars(root, provider='fixture', symbol='TEST', timeframe='1d')


def test_interrupted_multi_part_is_invisible_then_recovers(tmp_path, monkeypatch):
    import data_center.ingest.worker as module
    ledger, worker, run_id = setup_run(tmp_path)
    original_link = module.os.rename
    observations = []

    def interrupt_link(source, target):
        if str(target).endswith('.parquet'):
            reader = Thread(target=lambda: observations.append(len(read(worker.root))))
            reader.start()
            reader.join()
            if 'year=2026' in str(target):
                raise OSError('simulated supervisor interruption')
        return original_link(source, target)

    monkeypatch.setattr(module.os, 'rename', interrupt_link)
    with pytest.raises(OSError):
        worker.run_next()
    assert observations == [0, 0]
    assert read(worker.root) == []
    assert ledger.get(run_id)['status'] == 'running'
    first = next((worker.root / 'provider_bars').rglob('*.parquet'))
    original = file_hash(first)
    monkeypatch.setattr(module.os, 'rename', original_link)
    worker.run_next()
    assert len(read(worker.root)) == 2
    assert ledger.get(run_id)['status'] == 'pass'
    assert file_hash(first) == original
    manifest = manifest_path(worker.root, run_id).read_bytes()
    receipt = ledger.get(run_id)
    directory = worker.root / '.ingest-staging' / run_id / '1'
    staged_receipt = json.loads((directory / 'result.json').read_text())['receipt']
    worker._publish(directory, staged_receipt)
    assert manifest_path(worker.root, run_id).read_bytes() == manifest
    assert ledger.get(run_id) == receipt


def test_corrupt_staged_part_blocks_entire_publication(tmp_path, monkeypatch):
    ledger, worker, run_id = setup_run(tmp_path)
    publish = worker._publish

    def corrupt(directory, receipt):
        Path(receipt['paths'][-1]).write_bytes(b'invalid parquet')
        return publish(directory, receipt)

    monkeypatch.setattr(worker, '_publish', corrupt)
    assert worker.run_next()
    assert ledger.get(run_id)['status'] == 'failed'
    assert ledger.get(run_id)['failure_stage'] == 'publish'
    assert read(worker.root) == []
    assert not list((worker.root / 'provider_bars').rglob('*.parquet'))


def test_manifest_race_never_overwrites(tmp_path):
    target = tmp_path / 'immutable.json'
    results = []

    def publish(value):
        try:
            immutable_json(target, {'value': value})
            results.append(value)
        except PublicationError:
            pass

    threads = [Thread(target=publish, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(results) == 1
    assert json.loads(target.read_text()) == {'value': results[0]}


def test_cleanup_only_terminal_and_retains_reversible_audit(tmp_path):
    ledger, worker, run_id = setup_run(tmp_path)
    worker.run_next()
    before = {str(p): file_hash(p) for p in (worker.root / 'provider_bars').rglob('*.parquet')}
    queued = worker.submit(IngestJob(job_id='pending', symbol='TEST',
                                    start=datetime(2026, 1, 1, tzinfo=timezone.utc),
                                    end=datetime(2026, 1, 2, tzinfo=timezone.utc)))
    pending = worker.root / '.ingest-staging' / queued / '1'
    pending.mkdir(parents=True)
    (pending / 'active').write_text('untouched')
    evidence = tmp_path / 'evidence'
    dry = cleanup_staging(worker.root, ledger, evidence)
    assert dry['directory_count'] == 1
    report = cleanup_staging(worker.root, ledger, evidence, apply=True, operation_id='cleanup-1')
    assert report['directory_count'] == 1 and report['bytes'] > 0
    assert not (worker.root / '.ingest-staging' / run_id).exists()
    assert (pending / 'active').read_text() == 'untouched'
    assert (evidence / 'cleanup/quarantine/cleanup-1' / run_id / '1/result.json').exists()
    assert cleanup_staging(worker.root, ledger, evidence, apply=True, operation_id='cleanup-1') == report
    assert {str(p): file_hash(p) for p in (worker.root / 'provider_bars').rglob('*.parquet')} == before
    assert len(read(worker.root)) == 2
