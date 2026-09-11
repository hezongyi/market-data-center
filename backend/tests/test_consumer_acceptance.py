from pathlib import Path

from data_center.consumer_acceptance import _hash


def test_consumer_rows_hash_is_order_stable():
    assert _hash([{"b": 2, "a": 1}]) == _hash([{"a": 1, "b": 2}])


def test_consumer_acceptance_module_is_present():
    assert Path(__file__).parents[1].joinpath("src/data_center/consumer_acceptance.py").is_file()
