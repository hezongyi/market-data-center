from data_center.dataset_center import DatasetCenter, DatasetMember


def test_dataset_member_inheritance_and_idempotent_request(tmp_path):
    center = DatasetCenter(tmp_path)
    item = center.create(dataset_id="eurusd", name="EURUSD dataset", notes="first")
    center.add_member("eurusd", DatasetMember(symbol="EURUSD"))
    assert item.provider == "dukascopy" and item.base_timeframe == "1m"
    first = center.request("eurusd", symbol="EURUSD", start="2026-01-01T00:00:00Z", end="2026-01-02T00:00:00Z", idempotency_key="k1")
    second = center.request("eurusd", symbol="EURUSD", start="x", end="y", idempotency_key="k1")
    assert first == second and first["status"] == "queued"


def test_paused_allows_manual_request_and_archived_rejects(tmp_path):
    center = DatasetCenter(tmp_path)
    center.create(dataset_id="eurusd", name="EURUSD")
    center.add_member("eurusd", DatasetMember(symbol="EURUSD"))
    center.update("eurusd", status="paused")
    assert center.request("eurusd", symbol="EURUSD", start="a", end="b")["status"] == "queued"
    center.update("eurusd", status="archived")
    try:
        center.request("eurusd", symbol="EURUSD", start="a", end="b")
    except ValueError as exc:
        assert "read-only" in str(exc)
    else:
        raise AssertionError("archived dataset accepted maintenance")
