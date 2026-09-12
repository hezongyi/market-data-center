from datetime import datetime, timedelta, timezone

from data_center.control_plane import CoverageResult, MaintenancePolicy
from data_center.maintenance_runner import (
    _recent_gap_windows,
    _tail_recovery_windows,
    run_maintenance,
)
from data_center.settings import Settings


def test_maintenance_symbol_allowlist_normalizes_csv_and_whitespace(monkeypatch):
    monkeypatch.setenv("DATACENTER_MAINTENANCE_SYMBOLS", "eurusd, GBPUSD  xauusd")

    assert Settings().maintenance_symbol_list() == ("EURUSD", "GBPUSD", "XAUUSD")


def test_empty_maintenance_symbol_allowlist_is_unset(monkeypatch):
    monkeypatch.delenv("DATACENTER_MAINTENANCE_SYMBOLS", raising=False)

    assert Settings().maintenance_symbol_list() == ()


def test_scheduled_end_applies_provider_availability_lag():
    from data_center.maintenance_runner import _scheduled_end

    now = datetime(2026, 9, 12, 4, 7, 31, tzinfo=timezone.utc)
    assert _scheduled_end(now, lag_minutes=180) == datetime(2026, 9, 12, 1, 7, tzinfo=timezone.utc)


def test_tail_recovery_starts_after_observed_suffix_and_never_crosses_gap():
    start = datetime(2026, 9, 12, 2, 9, tzinfo=timezone.utc)
    end = datetime(2026, 9, 12, 3, 39, tzinfo=timezone.utc)
    coverage = CoverageResult(
        dataset_id="provider_bars", selector=(("symbol", "BTCUSD"),), row_count=59,
        min_ts=start, max_ts=datetime(2026, 9, 12, 3, 8, tzinfo=timezone.utc), gap_count=1,
        readiness_status="not_ready",
        missing_timestamps=(start + timedelta(minutes=12), *(
            datetime(2026, 9, 12, 3, 9, tzinfo=timezone.utc) + timedelta(minutes=index)
            for index in range(30)
        )),
        timeframe=timedelta(minutes=1),
    )

    windows = _tail_recovery_windows(
        coverage=coverage, start=start, end=end,
        policy=MaintenancePolicy(shard_minutes=60),
    )

    assert [(item["start"], item["end"], item["reason"]) for item in windows] == [
        ("2026-09-12T03:09:00+00:00", "2026-09-12T03:39:00+00:00", "tail"),
    ]


def test_recent_dead_letter_gap_is_suppressed_by_exact_window_and_cooldown():
    now = datetime(2026, 9, 12, 5, 0, tzinfo=timezone.utc)
    runs = [{
        "status": "dead_letter", "provider": "dukascopy", "symbol": "BTCUSD",
        "run_scope": "maintenance", "run_kind": "gap_repair",
        "finished_at": "2026-09-12T04:59:00+00:00",
        "execution_plan": {"windows": [{
            "start": "2026-09-12T02:09:00+00:00", "end": "2026-09-12T02:39:00+00:00",
            "reason": "gap_repair",
        }]},
        "quality_summary": {"findings": [{
            "code": "coverage_not_ready", "coverage": {
                "latest_complete_boundary": "2026-09-12T02:20:00+00:00",
                "timeframe_seconds": 60,
            },
        }]},
    }]

    assert _recent_gap_windows(
        runs=runs, provider="dukascopy", symbol="BTCUSD", run_scope="maintenance",
        now=now, cooldown_minutes=180,
    ) == {
        ("2026-09-12T02:09:00+00:00", "2026-09-12T02:39:00+00:00"),
        ("2026-09-12T02:21:00+00:00", "2026-09-12T02:22:00+00:00"),
    }
    assert not _recent_gap_windows(
        runs=runs, provider="dukascopy", symbol="BTCUSD", run_scope="maintenance",
        now=now, cooldown_minutes=0,
    )


def test_maintenance_continues_tail_after_gap_window_failure(monkeypatch, tmp_path):
    from data_center import maintenance_runner as module

    start = datetime(2026, 9, 12, 2, 9, tzinfo=timezone.utc)
    end = datetime(2026, 9, 12, 3, 39, tzinfo=timezone.utc)
    coverage = CoverageResult(
        dataset_id="provider_bars", selector=(("symbol", "BTCUSD"),), row_count=59,
        min_ts=start, max_ts=datetime(2026, 9, 12, 3, 8, tzinfo=timezone.utc), gap_count=1,
        readiness_status="not_ready", missing_timestamps=(start + timedelta(minutes=12),),
        timeframe=timedelta(minutes=1),
    )
    gap = {"start": "2026-09-12T02:21:00+00:00", "end": "2026-09-12T02:22:00+00:00",
           "reason": "gap_repair", "ordinal": 0, "semantics": "half-open"}
    monkeypatch.setattr(module, "coverage_from_catalog", lambda **_: coverage)
    monkeypatch.setattr(module, "build_ingest_plan", lambda **_: {"windows": [gap]})

    class Session:
        def __init__(self):
            self.headers = {}
            self.trust_env = True

        def close(self):
            pass

    monkeypatch.setattr(module.requests, "Session", Session)
    submitted_windows = []

    def submit(_session, _base_url, method, path, **kwargs):
        if method == "GET" and path == "/runs":
            return []
        assert (method, path) == ("POST", "/ingest/runs")
        submitted_windows.append(kwargs["json"])
        return {"run_id": f"run-{len(submitted_windows)}"}

    monkeypatch.setattr(module, "_request", submit)
    monkeypatch.setattr(module, "_wait_run", lambda _session, _base_url, run_id, _deadline: {
        "status": "failed" if run_id == "run-1" else "pass",
        "error_type": "QualityError" if run_id == "run-1" else None,
        "quality_summary": {"status": "fail", "findings": []},
        "row_count": None if run_id == "run-1" else 30,
    })

    report = run_maintenance(
        base_url="http://127.0.0.1:1", root=tmp_path / "lake", evidence_root=tmp_path / "evidence",
        provider="dukascopy", symbols=["BTCUSD"], start=start, end=end,
    )

    assert report["result"] == "failed"
    target = report["details"]["targets"][0]
    assert target["failed_window_count"] == 1
    assert target["recovery_window_count"] == 1
    assert [item["reason"] for item in target["runs"]] == ["gap_repair", "tail"]
    assert submitted_windows[1]["start"] == "2026-09-12T03:09:00+00:00"
