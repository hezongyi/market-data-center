# Version compatibility matrix

| Contract | Supported versions | Compatibility rule |
| --- | --- | --- |
| API envelope | `v1` | `/api/v1` keeps `data`, `meta`, `errors`; additive metadata is allowed. |
| `provider_bars` | `provider_bars.v1` | Immutable parts and manifests remain readable. |
| `economic_observations` | `economic_observations.v1`, `economic_observations.v2` | v1 remains read-only for historical parts; new governed FRED ingest uses v2. |
| Manifest | `provider_bars.v1`, `economic_observations.v1/v2` | Published manifests are immutable and validated before query visibility. |
| Receipt | `schema_version` follows dataset | A retry creates a new run; terminal receipts are never rewritten. |

The compatibility check in `scripts/compatibility_check.py` runs in the shared CI entry point. A contract change must add a new version, update this matrix, and retain readers for existing published versions until an explicit migration and evidence report removes them.

The production dependency baseline is Python 3.10+, FastAPI 0.115+, Pydantic 2+, Polars 1+, DuckDB 1+, `yfinance==1.7.0`, and `curl_cffi==0.16.3`. Exact pins are checked where declared; `pip check` runs before tests.
