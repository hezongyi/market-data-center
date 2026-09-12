from datetime import datetime, timedelta, timezone

from data_center.catalog.manifest import build_manifest, write_manifest
from data_center.catalog.snapshot import Catalog
from data_center.control_plane import TransformRecipe
from data_center.derived_maintenance_runner import (
    DerivedTarget,
    plan_derived_maintenance,
    select_recipes,
)
from data_center.domain.models import ProviderBar
from data_center.storage.parquet import write_provider_bars
from data_center.transform import TransformExecutor


def _raw_rows(start: datetime) -> list[ProviderBar]:
    return [ProviderBar(
        symbol="TEST", asset_class="crypto", provider="fixture", timeframe="1m",
        bar_ts=start + timedelta(minutes=index), open=100 + index, high=102 + index,
        low=99 + index, close=101 + index, volume=1, currency="USD", price_type="raw",
        ingest_ts=start + timedelta(hours=1), source_hash=f"raw-{index}",
    ) for index in range(10)]


def _publish_raw(root, rows, run_id):
    paths = write_provider_bars(root, rows, part_id=run_id)
    write_manifest(root, build_manifest(
        root, run_id=run_id, dataset_id="provider_bars", schema_version="provider_bars.v1",
        paths=paths, row_count=len(rows), quality_summary={"status": "pass", "finding_count": 0, "findings": []},
    ))


def test_select_recipes_returns_canonical_layers_in_dependency_order():
    recipes = select_recipes()
    assert recipes
    assert recipes[0].input_dataset == "provider_bars"
    assert any(item.input_dataset == "market_bars" and item.target_timeframe == "1w" for item in recipes)


def test_plan_marks_same_snapshot_materialization_idempotently(tmp_path):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    _publish_raw(tmp_path, _raw_rows(start), "raw")
    snapshot = Catalog(tmp_path).resolve(
        "provider_bars", {"provider": "fixture", "symbol": "TEST", "timeframe": "1m"},
    )
    recipe = TransformRecipe(
        recipe_id="test-5m", version="1", input_dataset="provider_bars", output_dataset="market_bars",
        source_timeframe="1m", target_timeframe="5m", allowed_schema_versions=("provider_bars.v1",),
        allowed_providers=("fixture",),
    )
    TransformExecutor().derive(
        recipe=recipe, input_snapshot=snapshot, selector={"provider": "fixture", "symbol": "TEST"},
        start=start, end=start + timedelta(minutes=10), root=tmp_path, run_id="derived", run_scope="acceptance",
    )
    target = DerivedTarget("fixture", "TEST", "crypto")
    plan = plan_derived_maintenance(
        root=tmp_path, provider="fixture", targets=(target,), recipes=(recipe,),
        start=start, end=start + timedelta(minutes=10),
    )
    assert len(plan) == 1
    assert plan[0]["status"] == "already_materialized"
    assert plan[0]["reason"] == "same_input_snapshot"
