"""Regression tests for an empty input window that owes no bars.

The production defect: ``TransformExecutor.derive`` resolved the session profile
from the first input row, so a window without rows (an FX weekend) raised
``IndexError`` before the empty-input check could classify it.  Each such run
was retried three times and landed in the dead-letter queue, which is how the
legacy derived maintenance unit ended up rate-limited and stopped.

The contract these tests pin down:

* a window the session keeps closed throughout owes no output and is reported
  once as ``SessionClosedError`` (skipped, never retried);
* an open window without input rows is a provider gap, which callers already
  treat as degraded;
* the classification follows the instrument and the session, not the presence
  of rows, and the planner and the executor agree on it.
"""
from datetime import datetime, timedelta, timezone

import pytest

from data_center.catalog.manifest import build_manifest, write_manifest
from data_center.catalog.snapshot import Catalog
from data_center.derived_maintenance_runner import (
    DerivedTarget,
    plan_derived_maintenance,
    run_derived_maintenance,
)
from data_center.domain.errors import (
    PROVIDER_GAP_ERROR_TYPES,
    SESSION_CLOSED_ERROR_TYPES,
    ProviderGapError,
    SessionClosedError,
)
from data_center.domain.models import ProviderBar
from data_center.ingest.process import safe_failure_result
from data_center.platform_registry import REGISTRY
from data_center.run_views import RunView
from data_center.runs.ledger import RunLedger
from data_center.storage.parquet import write_provider_bars
from data_center.transform import TransformExecutor, window_opens_session

# 2026-09-12 is a Saturday and 2026-09-14 a Monday: the FX/metals session
# profile used by the approved dukascopy instruments trades weekdays only.
SATURDAY = datetime(2026, 9, 12, tzinfo=timezone.utc)
SUNDAY = datetime(2026, 9, 13, tzinfo=timezone.utc)
MONDAY = datetime(2026, 9, 14, tzinfo=timezone.utc)
FIRST_HOP_RECIPE = "utc-24x7-1m-to-5m-ohlcv"


def _raw_rows(start: datetime, *, symbol: str = "EURUSD", count: int = 10) -> list[ProviderBar]:
    return [ProviderBar(
        symbol=symbol, asset_class="fx", provider="dukascopy", timeframe="1m",
        bar_ts=start + timedelta(minutes=index), open=1.1, high=1.2, low=1.0, close=1.15,
        volume=10, currency="USD", price_type="bid",
        ingest_ts=start + timedelta(hours=1), source_hash=f"raw-{index}",
    ) for index in range(count)]


def _publish_raw(root, rows, run_id):
    paths = write_provider_bars(root, rows, part_id=run_id)
    write_manifest(root, build_manifest(
        root, run_id=run_id, dataset_id="provider_bars", schema_version="provider_bars.v1",
        paths=paths, row_count=len(rows),
        quality_summary={"status": "pass", "finding_count": 0, "findings": []},
    ))


def _derive(*, root, start: datetime, end: datetime, symbol: str = "EURUSD"):
    snapshot = Catalog(root).resolve(
        "provider_bars", {"provider": "dukascopy", "symbol": symbol, "timeframe": "1m"})
    return TransformExecutor().derive(
        recipe=REGISTRY.recipe(FIRST_HOP_RECIPE, "1"), input_snapshot=snapshot,
        selector={"provider": "dukascopy", "symbol": symbol},
        start=start, end=end, root=root, run_scope="acceptance",
    )


def test_a_weekend_derive_is_classified_instead_of_crashing(tmp_path):
    with pytest.raises(SessionClosedError) as raised:
        _derive(root=tmp_path, start=SATURDAY, end=SUNDAY)

    # The instrument's own profile decided it, not a guess about the weekend.
    assert "dukascopy_fx_weekdays_utc" in str(raised.value)


def test_an_open_window_without_input_rows_is_a_provider_gap(tmp_path):
    with pytest.raises(ProviderGapError) as raised:
        _derive(root=tmp_path, start=MONDAY, end=MONDAY + timedelta(hours=1))

    assert type(raised.value) is ProviderGapError
    assert "dukascopy_fx_weekdays_utc" in str(raised.value)


def test_a_crypto_instrument_is_open_across_the_same_weekend(tmp_path):
    """The same window is a gap for 24x7 crypto, so closure is per instrument."""
    with pytest.raises(ProviderGapError) as raised:
        _derive(root=tmp_path, start=SATURDAY, end=SUNDAY, symbol="BTCUSD")

    assert not isinstance(raised.value, SessionClosedError)
    assert "dukascopy_crypto_24x7" in str(raised.value)


def test_rows_outside_the_session_never_open_a_closed_window(tmp_path):
    """A closed window stays closed even when the input part holds bars for it."""
    _publish_raw(tmp_path, _raw_rows(SATURDAY), "raw-saturday")

    with pytest.raises(SessionClosedError):
        _derive(root=tmp_path, start=SATURDAY, end=SUNDAY)


def test_the_session_probe_answers_for_partial_and_unaligned_windows():
    session = REGISTRY.session("dukascopy_fx_weekdays_utc")

    assert window_opens_session(session, start=SATURDAY, end=SUNDAY, step=timedelta(minutes=1)) is False
    # A window that only clips the open Monday session still owes bars.
    assert window_opens_session(session, start=SUNDAY + timedelta(hours=23, minutes=59),
                               end=MONDAY + timedelta(minutes=1), step=timedelta(minutes=1)) is True
    # An unaligned start is advanced to the first instant on the source grid.
    assert window_opens_session(session, start=SUNDAY + timedelta(hours=23, minutes=59, seconds=30),
                               end=MONDAY + timedelta(minutes=30), step=timedelta(minutes=1)) is True
    with pytest.raises(ValueError, match="positive step"):
        window_opens_session(session, start=SATURDAY, end=SUNDAY, step=timedelta(0))


def test_the_planner_skips_a_closed_window_and_plans_an_open_one(tmp_path):
    _publish_raw(tmp_path, _raw_rows(MONDAY), "raw-monday")
    target = DerivedTarget("dukascopy", "EURUSD", "fx")
    recipe = REGISTRY.recipe(FIRST_HOP_RECIPE, "1")

    closed = plan_derived_maintenance(
        root=tmp_path, provider="dukascopy", targets=(target,), recipes=(recipe,),
        start=SATURDAY, end=SUNDAY,
    )
    assert [item["status"] for item in closed] == ["skipped"]
    assert closed[0]["reason"] == "session_closed"

    open_window = plan_derived_maintenance(
        root=tmp_path, provider="dukascopy", targets=(target,), recipes=(recipe,),
        start=MONDAY, end=MONDAY + timedelta(hours=1),
    )
    assert [item["status"] for item in open_window] == ["planned"]


@pytest.mark.parametrize(("error_type", "item_status", "result", "counter"), [
    ("SessionClosedError", "skipped", "pass", "skipped_count"),
    ("ProviderGapError", "degraded", "pass", "degraded_count"),
    ("ValueError", "failed", "failed", "failed_count"),
])
def test_a_derive_receipt_is_classified_by_what_it_reports(monkeypatch, tmp_path, error_type,
                                                           item_status, result, counter):
    from data_center import derived_maintenance_runner as module

    root = tmp_path / "lake"
    _publish_raw(root, _raw_rows(MONDAY), "raw-monday")
    monkeypatch.setattr(module, "_request", lambda *_a, **_kw: {"run_id": "run-1"})
    monkeypatch.setattr(module, "_wait_run", lambda *_a, **_kw: {
        "status": "failed", "error_type": error_type, "quality_summary": None, "row_count": None,
    })

    report = run_derived_maintenance(
        base_url="http://127.0.0.1:1", root=root, evidence_root=tmp_path / "evidence",
        provider="dukascopy", symbols=["EURUSD"], start=MONDAY, end=MONDAY + timedelta(minutes=10),
        recipe_specs=[f"{FIRST_HOP_RECIPE}@1"], run_scope="maintenance",
    )

    assert report["result"] == result
    assert report["details"][counter] == 1
    assert [item["status"] for item in report["details"]["plan"]] == [item_status]


def test_a_closed_window_is_reported_once_and_is_not_retryable():
    closed = safe_failure_result(SessionClosedError("window is closed"), {"run_scope": "production"})
    assert closed["error_type"] == "SessionClosedError"
    assert closed["failure_stage"] == "input" and closed["retryable"] is False

    gap = safe_failure_result(ProviderGapError("no bars"), {"run_scope": "production"})
    assert gap["error_type"] == "ProviderGapError" and gap["retryable"] is False


def test_the_two_gap_classes_stay_distinguishable_in_evidence():
    assert SESSION_CLOSED_ERROR_TYPES < PROVIDER_GAP_ERROR_TYPES


def test_a_closed_window_explains_itself_without_blaming_the_provider(tmp_path):
    view = RunView(RunLedger(tmp_path / "audit.sqlite"))

    closed = view.degraded_reasons({"status": "failed", "error_type": "SessionClosedError"}, [])
    assert [reason["code"] for reason in closed] == ["session_closed"]

    gap = view.degraded_reasons({"status": "failed", "error_type": "ProviderGapError"}, [])
    assert [reason["code"] for reason in gap] == ["provider_gap"]

    attempt = view.degraded_reasons(
        {"status": "failed", "attempt_errors": [{"error_type": "SessionClosedError"}]}, [])
    assert [reason["code"] for reason in attempt] == ["session_closed"]
