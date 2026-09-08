# Storage Contract

canonical root 由配置注入，默认不写入代码仓库。Parquet 是数据事实来源，DuckDB 只读查询 Parquet，SQLite 只保存运行记录、质量 finding 和 receipt 索引。

建议分区：`provider_bars/provider={provider}/asset_class={asset_class}/symbol={symbol}/timeframe={timeframe}/year={year}`。

经济数据分区为 `economic_observations/provider={provider}/series_id={series_id}`。每个 ingest 写入带 `run_id` 的新 Parquet part，不覆盖已存在的数据版本。

写入必须经过 catalog path resolver；应用层不得拼接 canonical 路径。每次写入产生不可变 output hash 和 receipt。
