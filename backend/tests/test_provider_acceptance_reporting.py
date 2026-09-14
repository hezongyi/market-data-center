"""A red provider acceptance must be triageable from its receipt alone (issue #94).

The scheduled real-provider acceptance kept failing with a bare error_type, so nothing in the receipt
said whether the provider was unreachable, the API refused the request, or the readback was wrong. These
tests pin the failure classification, the credential redaction and the retry policy.
"""
from __future__ import annotations

from datetime import datetime, timezone

import requests

from data_center.acceptance import (
    adapter_version,
    classify_failure,
    redact_summary,
    run_provider_attempts,
)

CHECKED_AT = datetime(2026, 9, 14, tzinfo=timezone.utc)


def http_error(status: int, body: str = "upstream unavailable") -> requests.exceptions.HTTPError:
    response = requests.Response()
    response.status_code = status
    response._content = body.encode()
    response.encoding = "utf-8"
    return requests.exceptions.HTTPError(f"{status} error", response=response)


def test_timeout_and_connection_failures_are_retryable() -> None:
    timeout = classify_failure(requests.exceptions.Timeout("read timed out"))
    assert timeout["category"] == "timeout" and timeout["retryable"] is True
    assert timeout["error_type"] == "Timeout" and timeout["error_message"] == "read timed out"

    network = classify_failure(requests.exceptions.ConnectionError("connection refused"))
    assert network["category"] == "network" and network["retryable"] is True


def test_http_failures_carry_the_status_and_only_5xx_is_retryable() -> None:
    server = classify_failure(http_error(503, '{"errors":[{"code":"unavailable"}]}'))
    assert server["category"] == "http" and server["retryable"] is True
    assert server["http_status"] == 503
    assert "unavailable" in server["response_summary"]

    refusal = classify_failure(http_error(507, '{"errors":[{"code":"capacity_protected"}]}'))
    assert refusal["http_status"] == 507

    not_found = classify_failure(http_error(404))
    assert not_found["category"] == "http" and not_found["retryable"] is False


def test_contract_failures_are_named_and_not_retried() -> None:
    failure = classify_failure(ValueError("API readback incomplete"))

    assert failure["category"] == "contract"
    assert failure["retryable"] is False
    assert failure["error_message"] == "API readback incomplete"


def test_failure_summaries_never_echo_credential_shaped_values() -> None:
    summary = redact_summary('{"detail":"invalid api key","X-API-Key":"super-secret-value","token":"abc123"}')

    assert "super-secret-value" not in summary
    assert "abc123" not in summary
    assert "<redacted>" in summary


def test_provider_attempts_record_every_try_and_stop_on_contract_failures() -> None:
    attempts = {"count": 0}

    def call(method, path, **kwargs):
        attempts["count"] += 1
        raise requests.exceptions.ConnectionError("connection refused")

    result, failure = run_provider_attempts("dukascopy", "EURUSD", "fx", call=call, envelope=call,
                                            acceptance_id="test", checked_at=CHECKED_AT, deadline_seconds=1,
                                            attempts=3, backoff=0)

    assert attempts["count"] == 3, "a retryable failure must be retried up to the attempt budget"
    assert [entry["attempt"] for entry in result["attempts"]] == [1, 2, 3]
    assert failure is not None and failure["category"] == "network"
    assert result["status"] == "failed"
    assert result["adapter_version"].startswith("dukascopy")


def test_contract_failures_are_not_retried() -> None:
    attempts = {"count": 0}

    def call(method, path, **kwargs):
        attempts["count"] += 1
        raise ValueError("non-pass receipt")

    result, failure = run_provider_attempts("binance", "BTCUSDT", "crypto", call=call, envelope=call,
                                            acceptance_id="test", checked_at=CHECKED_AT, deadline_seconds=1,
                                            attempts=3, backoff=0)

    assert attempts["count"] == 1, "a contract failure is a result, not a transient error"
    assert failure is not None and failure["category"] == "contract"
    assert result["attempts"] == [{"attempt": 1, "category": "contract", "retryable": False,
                                   "error_type": "ValueError", "error_message": "non-pass receipt"}]


def test_adapter_version_names_the_adapter_or_says_unknown() -> None:
    assert adapter_version("dukascopy").startswith("dukascopy")
    assert adapter_version("not-a-provider") == "unknown"
