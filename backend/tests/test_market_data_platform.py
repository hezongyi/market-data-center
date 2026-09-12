import json
from datetime import datetime, timedelta, timezone

import pytest

from data_center.catalog.registry import (
    get_dataset_definition,
    iter_dataset_definitions,
)
from data_center.catalog.snapshot import Catalog
from data_center.control_plane import (
    ControlPlaneRegistry,
    MaintenancePolicy,
    SessionProfile,
    TransformRecipe,
    evaluate_coverage,
    plan_maintenance,
)
from data_center.domain.models import DeriveJob, IngestJob, ProviderBar
from data_center.maintenance_runner import approved_targets
from data_center.platform import build_ingest_plan, coverage_for_rows, execute_ingest
from data_center.storage.parquet import write_provider_bars
from data_center.storage.query import query_market_bars
from data_center.transform import TransformExecutor, recomputation_plan


def _raw_rows(provider: str, symbol: str, start: datetime) -> list[ProviderBar]:
    rows = []
    for index in range(10):
        stamp = start + timedelta(minutes=index)
        rows.append(ProviderBar(
            symbol=symbol, asset_class="crypto", provider=provider, timeframe="1m",
            bar_ts=stamp, open=100 + index, high=102 + index, low=99 + index,
            close=101 + index, volume=10 + index, currency="USD", price_type="raw",
            ingest_ts=start + timedelta(hours=1), source_hash=f"{provider}-{index}",
        ))
    return rows


def _publish_raw(root, rows, run_id):
    from data_center.catalog.manifest import build_manifest, write_manifest

    paths = write_provider_bars(root, rows, part_id=run_id)
    write_manifest(root, build_manifest(
        root, run_id=run_id, dataset_id="provider_bars", schema_version="provider_bars.v1",
        paths=paths, row_count=len(rows), quality_summary={"status": "pass", "finding_count": 0, "findings": []},
    ))


def test_dataset_definitions_are_complete_control_plane_records():
    definitions = {item.dataset_id: item for item in iter_dataset_definitions()}
    assert {"provider_bars", "economic_observations", "market_bars"} <= set(definitions)
    assert definitions["provider_bars"].kind == "raw"
    assert definitions["market_bars"].kind == "derived"
    assert definitions["market_bars"].required_lineage == (
        "recipe_id", "recipe_version", "input_snapshot_id", "source_hash",
    )
    assert get_dataset_definition("provider_bars").retention_policy == {"mode": "immutable"}


def test_maintenance_targets_are_control_plane_approved_and_filterable():
    targets = approved_targets("dukascopy", ["eurusd", "XAUUSD"])
    assert [(item.symbol, item.asset_class) for item in targets] == [
        ("EURUSD", "fx"), ("XAUUSD", "commodity"),
    ]


def test_control_plane_registry_exposes_policy_and_recipe_dependency_graph():
    registry = ControlPlaneRegistry()
    registry.register_maintenance_policy(MaintenancePolicy(policy_id="minute", shard_days=1))
    recipe = TransformRecipe(
        recipe_id="minute-to-hour", version="1", input_dataset="provider_bars",
        output_dataset="market_bars", source_timeframe="1m", target_timeframe="1h",
        allowed_schema_versions=("provider_bars.v1",),
    )
    registry.register_recipe(recipe)
    assert registry.maintenance_policy("minute").shard_days == 1
    assert registry.dependency_graph() == {
        "provider_bars": ({"output_dataset": "market_bars", "recipe_id": "minute-to-hour",
                            "recipe_version": "1"},),
    }


def test_planner_builds_bounded_immutable_execution_plan():
    job = IngestJob(
        job_id="binance-backfill", provider="binance", symbol="BTCUSDT", asset_class="crypto",
        timeframe="1m", start=datetime(2026, 1, 1, tzinfo=timezone.utc),
        end=datetime(2026, 2, 10, tzinfo=timezone.utc), run_kind="backfill", run_scope="maintenance",
    )
    plan = build_ingest_plan(job=job, policy=MaintenancePolicy(max_window_days=40, shard_days=40))
    assert plan["run_kind"] == "backfill" and plan["run_scope"] == "maintenance"
    assert len(plan["windows"]) == 2
    assert all((datetime.fromisoformat(window["end"]) - datetime.fromisoformat(window["start"])).days <= 31
               for window in plan["windows"])
    assert set(plan["config_digests"]) == {
        "dataset_digest", "capability_digest", "instrument_digest", "session_profile_digest",
               "calendar_digest", "quality_profile_digest", "maintenance_policy_digest",
    }


def test_provider_native_higher_period_is_not_a_canonical_maintenance_target():
    job = IngestJob(
        job_id="dukascopy-native-5m", provider="dukascopy", symbol="EURUSD", asset_class="fx",
        timeframe="5m", start=datetime(2026, 1, 1, tzinfo=timezone.utc),
        end=datetime(2026, 1, 1, 0, 5, tzinfo=timezone.utc), run_kind="backfill",
        run_scope="maintenance",
    )
    with pytest.raises(ValueError, match="unsupported maintenance timeframe"):
        build_ingest_plan(job=job)


def test_coverage_separates_physical_session_quality_and_readiness():
    friday = datetime(2026, 1, 2, 23, 59, tzinfo=timezone.utc)
    monday = datetime(2026, 1, 5, 0, 0, tzinfo=timezone.utc)
    coverage = coverage_for_rows(
        dataset_id="provider_bars", selector={"provider": "fixture", "symbol": "FX", "timeframe": "1m"},
        rows=[{"bar_ts": friday}, {"bar_ts": monday}],
        session_profile=SessionProfile(profile_id="weekdays", mode="weekdays"),
        requested_start=friday, requested_end=monday + timedelta(minutes=1),
    )
    assert coverage.physical_coverage == "present"
    assert coverage.session_coverage == "complete"
    assert coverage.quality_status == "pass"
    assert coverage.readiness_status == "ready"
    assert coverage.latest_complete_boundary == monday


def test_gap_repair_uses_coverage_timeframe_and_merges_adjacent_gaps():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    timeframe = timedelta(minutes=5)
    rows = [{"bar_ts": start + offset * timeframe} for offset in (0, 1, 4, 5)]
    coverage = evaluate_coverage(
        dataset_id="provider_bars", selector={"timeframe": "5m"}, rows=rows,
        timeframe=timeframe, requested_start=start, requested_end=start + 6 * timeframe,
    )
    windows = plan_maintenance(
        start=start, end=start + 6 * timeframe, coverage=coverage,
        policy=MaintenancePolicy(shard_days=1),
    )
    assert coverage.missing_timestamps == (start + 2 * timeframe, start + 3 * timeframe)
    assert [(window.start, window.end, window.reason) for window in windows] == [
        (start + 2 * timeframe, start + 4 * timeframe, "gap_repair"),
    ]


def test_planner_supports_intraday_shard_minutes():
    start = datetime(2026, 1, 5, tzinfo=timezone.utc)
    windows = plan_maintenance(
        start=start, end=start + timedelta(minutes=150),
        policy=MaintenancePolicy(shard_days=7, shard_minutes=60),
    )
    assert [(window.start, window.end) for window in windows] == [
        (start, start + timedelta(minutes=60)),
        (start + timedelta(minutes=60), start + timedelta(minutes=120)),
        (start + timedelta(minutes=120), start + timedelta(minutes=150)),
    ]


def test_maintenance_planner_is_idempotent_when_requested_range_is_ready():
    start = datetime(2026, 1, 5, tzinfo=timezone.utc)
    end = start + timedelta(minutes=10)
    coverage = evaluate_coverage(
        dataset_id="provider_bars", selector={"timeframe": "1m"},
        rows=[{"bar_ts": start + timedelta(minutes=index)} for index in range(10)],
        timeframe=timedelta(minutes=1), requested_start=start, requested_end=end,
    )
    assert coverage.readiness_status == "ready"
    assert plan_maintenance(start=start, end=end, coverage=coverage) == []


def test_execute_ingest_records_plan_lineage_and_run_classification(tmp_path):
    class Connector:
        provider = "fixture"
        version = "controlled-v1"

        def fetch_bars(self, job):
            return _raw_rows("fixture", job.symbol, job.start)[:1]

    job = IngestJob(job_id="controlled", provider="fixture", symbol="TEST", asset_class="crypto",
                    timeframe="1m", start=datetime(2026, 1, 1, tzinfo=timezone.utc),
                    end=datetime(2026, 1, 1, 0, 1, tzinfo=timezone.utc), run_scope="acceptance")
    plan = build_ingest_plan(job=job)
    window = plan["windows"][0]
    from data_center.control_plane import IngestWindow

    receipt = execute_ingest(
        window=IngestWindow(datetime.fromisoformat(window["start"]), datetime.fromisoformat(window["end"]),
                            window["reason"], window["ordinal"]),
        job=job, root=tmp_path, connector=Connector(), execution_plan=plan,
    )
    manifest = json.loads((tmp_path / ".manifests" / f"{receipt['run_id']}.json").read_text())
    assert receipt["run_kind"] == manifest["run_kind"] == "ingest"
    assert receipt["run_scope"] == manifest["run_scope"] == "acceptance"
    assert receipt["execution_plan"]["config_digest"]
    assert manifest["lineage"]["input_kind"] == "provider_request"
    assert manifest["lineage"]["source_hash_count"] == 1
    assert manifest["lineage"]["source_hash_digest"]
    assert "source_hashes" not in manifest["lineage"]


def test_two_providers_reuse_transform_executor_with_snapshot_lineage(tmp_path):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    recipe = TransformRecipe(
        recipe_id="five-minute-ohlcv", version="1", input_dataset="provider_bars",
        output_dataset="market_bars", source_timeframe="1m", target_timeframe="5m",
        allowed_schema_versions=("provider_bars.v1",), missing_input_policy="fail",
    )
    executor = TransformExecutor()
    receipts = []
    for provider, symbol in (("fixture", "AAAUSD"), ("binance", "BBBUSDT")):
        _publish_raw(tmp_path, _raw_rows(provider, symbol, start), f"raw-{provider}")
        snapshot = Catalog(tmp_path).resolve(
            "provider_bars", {"provider": provider, "symbol": symbol, "timeframe": "1m"},
        )
        receipts.append(executor.derive(
            recipe=recipe, input_snapshot=snapshot,
            selector={"provider": provider, "symbol": symbol}, start=start,
            end=start + timedelta(minutes=10), root=tmp_path, run_id=f"derived-{provider}",
            run_scope="acceptance",
        ))
        rows = query_market_bars(
            tmp_path, provider=provider, symbol=symbol, timeframe="5m", price_basis="raw",
            recipe_id=recipe.recipe_id, recipe_version=recipe.version,
        )
        assert len(rows) == 2
        assert rows[0]["open"] == 100 and rows[0]["close"] == 105
        assert rows[0]["input_snapshot_id"] == snapshot.snapshot_id
    assert {receipt["provider"] for receipt in receipts} == {"fixture", "binance"}
    assert {receipt["lineage"]["aggregation_version"] for receipt in receipts} == {executor.version}
    assert all(set(receipt["config_digests"]) == {
        "recipe_digest", "capability_digest", "instrument_digest", "session_profile_digest",
        "calendar_digest", "quality_profile_digest",
    } for receipt in receipts)
    assert all(receipt["lineage"]["input_hash"] and receipt["lineage"]["output_hash"]
               for receipt in receipts)


def test_transform_filters_wide_snapshot_before_materializing_rows(tmp_path):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    recipe = TransformRecipe(
        recipe_id="wide-snapshot-5m", version="1", input_dataset="provider_bars",
        output_dataset="market_bars", source_timeframe="1m", target_timeframe="5m",
        allowed_schema_versions=("provider_bars.v1",), missing_input_policy="fail",
    )
    _publish_raw(tmp_path, _raw_rows("fixture", "TARGET", start), "target-in-range")
    _publish_raw(tmp_path, _raw_rows("fixture", "OTHER", start), "other-in-range")
    _publish_raw(tmp_path, _raw_rows("fixture", "TARGET", start + timedelta(days=1)), "target-out-of-range")
    snapshot = Catalog(tmp_path).resolve("provider_bars", {"provider": "fixture"})
    receipt = TransformExecutor().derive(
        recipe=recipe, input_snapshot=snapshot, selector={"provider": "fixture", "symbol": "TARGET"},
        start=start, end=start + timedelta(minutes=10), root=tmp_path, run_id="wide-derived",
        run_scope="acceptance",
    )
    assert receipt["input_row_count"] == 10
    assert receipt["row_count"] == 2


def test_transform_applies_named_session_profile_before_aggregation(tmp_path):
    start = datetime(2026, 1, 2, 23, 55, tzinfo=timezone.utc)  # Friday
    recipe = TransformRecipe(
        recipe_id="weekdays-5m", version="1", input_dataset="provider_bars",
        output_dataset="market_bars", source_timeframe="1m", target_timeframe="5m",
        allowed_schema_versions=("provider_bars.v1",), session_profile="weekdays_utc",
        missing_input_policy="fail",
    )
    _publish_raw(tmp_path, _raw_rows("fixture", "WEEKDAYS", start), "weekdays-raw")
    snapshot = Catalog(tmp_path).resolve(
        "provider_bars", {"provider": "fixture", "symbol": "WEEKDAYS", "timeframe": "1m"},
    )
    receipt = TransformExecutor().derive(
        recipe=recipe, input_snapshot=snapshot, selector={"provider": "fixture", "symbol": "WEEKDAYS"},
        start=start, end=start + timedelta(minutes=10), root=tmp_path, run_id="weekdays-derived",
        run_scope="acceptance",
    )
    assert receipt["input_row_count"] == 5
    assert receipt["row_count"] == 1


def test_research_only_recipe_cannot_publish_to_canonical_dataset(tmp_path):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    recipe = TransformRecipe(
        recipe_id="research-only", version="1", input_dataset="provider_bars",
        output_dataset="market_bars", source_timeframe="1m", target_timeframe="5m",
        allowed_schema_versions=("provider_bars.v1",), publication_policy="research_only",
    )
    _publish_raw(tmp_path, _raw_rows("fixture", "RESEARCH", start), "research-raw")
    snapshot = Catalog(tmp_path).resolve(
        "provider_bars", {"provider": "fixture", "symbol": "RESEARCH", "timeframe": "1m"},
    )
    with pytest.raises(ValueError, match="publication policy"):
        TransformExecutor().derive(
            recipe=recipe, input_snapshot=snapshot, selector={"provider": "fixture", "symbol": "RESEARCH"},
            start=start, end=start + timedelta(minutes=10), root=tmp_path, run_scope="acceptance",
        )


def test_recomputation_plan_is_explicit_and_not_automatic():
    recipe = TransformRecipe(
        recipe_id="hourly", version="1", input_dataset="provider_bars", output_dataset="market_bars",
        source_timeframe="1m", target_timeframe="1h", allowed_schema_versions=("provider_bars.v1",),
    )
    plan = recomputation_plan(
        recipe=recipe, selectors=[{"provider": "fixture", "symbol": "TEST"}],
        affected_start=datetime(2026, 1, 1, 0, 12, tzinfo=timezone.utc),
        affected_end=datetime(2026, 1, 1, 1, 3, tzinfo=timezone.utc),
    )
    assert plan["affected_start"] == "2026-01-01T00:00:00+00:00"
    assert plan["affected_end"] == "2026-01-01T02:00:00+00:00"
    assert plan["automatic_execution"] is False


def test_transform_rejects_not_ready_canonical_output(tmp_path):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    recipe = TransformRecipe(
        recipe_id="drop-incomplete", version="1", input_dataset="provider_bars",
        output_dataset="market_bars", source_timeframe="1m", target_timeframe="5m",
        allowed_schema_versions=("provider_bars.v1",), missing_input_policy="allow",
        partial_bucket_policy="drop",
    )
    rows = _raw_rows("fixture", "INCOMPLETE", start)
    rows.pop(2)
    _publish_raw(tmp_path, rows, "incomplete-raw")
    snapshot = Catalog(tmp_path).resolve(
        "provider_bars", {"provider": "fixture", "symbol": "INCOMPLETE", "timeframe": "1m"},
    )
    with pytest.raises(ValueError, match="coverage is not ready"):
        TransformExecutor().derive(
            recipe=recipe, input_snapshot=snapshot,
            selector={"provider": "fixture", "symbol": "INCOMPLETE"},
            start=start, end=start + timedelta(minutes=10), root=tmp_path,
            run_scope="acceptance",
        )


def test_worker_expands_planned_windows_into_independent_runs(tmp_path):
    from data_center.ingest.worker import LocalWorker
    from data_center.runs.ledger import RunLedger

    ledger = RunLedger(tmp_path / "ledger.sqlite")
    worker = LocalWorker(tmp_path / "lake", ledger)
    job = IngestJob(
        job_id="windowed", provider="fixture", symbol="WINDOWED", asset_class="crypto", timeframe="1d",
        start=datetime(2026, 1, 1, tzinfo=timezone.utc), end=datetime(2026, 2, 10, tzinfo=timezone.utc),
        run_kind="backfill", run_scope="acceptance",
    )
    run_ids = worker.submit_many(job)
    assert len(run_ids) == 6
    while worker.run_next():
        pass
    assert all(ledger.get(run_id)["status"] == "pass" for run_id in run_ids)


def test_derive_runs_through_worker_staging_and_ledger(tmp_path):
    from data_center.ingest.worker import LocalWorker
    from data_center.runs.ledger import RunLedger

    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    lake = tmp_path / "lake"
    _publish_raw(lake, _raw_rows("fixture", "DERIVE", start), "worker-derived-raw")
    ledger = RunLedger(tmp_path / "ledger.sqlite")
    worker = LocalWorker(lake, ledger)
    run_id = worker.submit_derive(DeriveJob(
        job_id="worker-derived", provider="fixture", symbol="DERIVE",
        recipe_id="utc-24x7-1m-to-5m-ohlcv", recipe_version="1",
        start=start, end=start + timedelta(minutes=10), run_scope="acceptance",
    ))
    assert ledger.get(run_id)["status"] == "queued"
    assert worker.run_next() is True
    receipt = ledger.get(run_id)
    assert receipt["status"] == "pass" and receipt["run_kind"] == "derive"
    assert receipt["input_snapshot_id"] == receipt["lineage"]["input_snapshot_id"]
    assert len(query_market_bars(
        lake, provider="fixture", symbol="DERIVE", timeframe="5m", price_basis="raw",
        recipe_id="utc-24x7-1m-to-5m-ohlcv", recipe_version="1",
    )) == 2
