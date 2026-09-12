# `macro-market-lab` 接入

`macro-market-lab` 应通过 `DataCenterClient` 的 HTTP API 读取数据，不直接访问数据中心的 Parquet 或 SQLite。建议先以显式环境变量启用只读路径：

```python
from data_center.client import DataCenterClient

client = DataCenterClient(
    base_url="http://127.0.0.1:18380",
    api_key=None,
)
bars = client.bars(provider="fixture", symbol="BTCUSDT", timeframe="1d", page_size=1000)
observations = client.economic_observations(series_id="PAYEMS", provider="fred", page_size=1000)
```

迁移到真实 consumer 前，使用同一时间范围比较 output hash、行数、时间范围和 quality status。`economic_observations.v2` 已冻结 PIT 字段；使用 `mode="pit"` 时必须提供 `asof_ts`，未知 release time 的版本不会被伪装成可见数据。

当前迁移状态：provider bars 与 market bars 只读 preview/query 已默认走 Data Center HTTP adapter；设置
`MACRO_MARKET_USE_DATA_CENTER_BARS=0` 可回滚旧 consumer。Dukascopy raw/derived maintenance
已由 systemd service 切换到 Data Center `derived_maintenance_runner`；设置
`MARKETLAB_MARKET_BARS_BACKEND=legacy` 可回滚旧 maintenance。economic consumer
通过 `MACRO_MARKET_USE_DATA_CENTER_ECONOMIC` 显式启用 `DataCenterReadClient` 的 current-state 读取，
取消该变量即可回滚。启用前必须运行 `scripts/economic_parity.py` 并保留 parity receipt；ingest、quality
仍需按 `docs/current-state.md` 的矩阵独立迁移；yfinance macro-daily、economic PIT 和其他未列入本次范围的 consumer
继续保持旧路径并输出 `not_migrated`。
