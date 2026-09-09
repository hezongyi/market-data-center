# `macro-market-lab` 接入

`macro-market-lab` 应通过 `DataCenterClient` 的 HTTP API 读取数据，不直接访问数据中心的 Parquet 或 SQLite。建议先以显式环境变量启用只读路径：

```python
from data_center.client import DataCenterClient

client = DataCenterClient(
    base_url="http://127.0.0.1:18380",
    api_key=None,
)
bars = client.bars(provider="fixture", symbol="BTCUSDT", timeframe="1d")
observations = client.economic_observations(series_id="PAYEMS", provider="fred")
```

迁移到真实 consumer 前，使用同一时间范围比较 output hash、行数、时间范围和 quality status。当前 `economic_observations.v1` 尚未覆盖 `macro-market-lab` 所需的完整 PIT 字段，因此 economic consumer 迁移必须等待兼容 schema 和 parity tests。

当前迁移状态：provider bars 只读预览通过 `scripts/adapter_parity.py` 验证，并由
`MACRO_MARKET_USE_DATA_CENTER_BARS` 显式启用；取消该变量即可回滚旧 consumer。economic、ingest、quality
和 maintenance consumer 保持旧路径，输出中标记 `not_migrated`，直到 PIT 历史窗口 parity 通过。
