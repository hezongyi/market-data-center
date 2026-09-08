from pathlib import Path


def provider_bars_path(root: Path, *, provider: str, asset_class: str, symbol: str, timeframe: str, year: int) -> Path:
    return root / "provider_bars" / f"provider={provider}" / f"asset_class={asset_class}" / f"symbol={symbol}" / f"timeframe={timeframe}" / f"year={year}"

