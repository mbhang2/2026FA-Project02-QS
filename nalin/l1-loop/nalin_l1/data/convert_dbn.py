"""Convert Databento trades to 1-minute OHLCV bars.

Reads raw Databento ZST trades and resamples them into canonical 1-minute OHLCV
Parquet format.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import databento as db
import pandas as pd


def _data_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def convert_dbn_to_ohlcv(
    input_path: str | Path,
    output_path: str | Path,
    asset_id: str = "ES",
    freq: str = "1min",
) -> None:
    """Convert raw Databento trades to resampled OHLCV bars."""
    input_path = Path(input_path)
    output_path = Path(output_path)
    
    print(f"Reading {input_path}...")
    
    # Read the DBN file directly using databento
    # Note: For trade data, we want to extract price and size
    data = db.DBNStore.from_file(input_path)
    df = data.to_df()
    
    # Check if df is empty
    if df.empty:
        print("Warning: No data found in DBN file.")
        # Create empty OHLCV dataframe with correct schema
        empty_df = pd.DataFrame(columns=["asset_id", "open", "high", "low", "close", "volume"])
        empty_df.index = pd.DatetimeIndex([], name="timestamp")
        empty_df.to_parquet(output_path)
        return

    # Ensure index is tz-aware UTC timestamp
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    else:
        df.index = df.index.tz_convert("UTC")

    # Rename size column to volume if it exists
    if "size" in df.columns and "volume" not in df.columns:
        df = df.rename(columns={"size": "volume"})
        
    # Scale prices if needed (DBN often uses fixed point integers for prices)
    # The databento to_df() method usually scales prices automatically based on the DBN metadata,
    # but let's double check if they look like fixed point.
    if "price" in df.columns:
        if df["price"].max() > 1e6: # Likely unscaled fixed point
             df["price"] = df["price"] / 1e9

    print(f"Resampling {len(df)} trades to {freq} bars...")

    # Resample to OHLCV
    resampled = df.resample(freq).agg(
        open=("price", "first"),
        high=("price", "max"),
        low=("price", "min"),
        close=("price", "last"),
        volume=("volume", "sum")
    )

    # Drop empty bins where no trades occurred
    resampled = resampled.dropna(subset=["open"])

    # Add asset_id
    resampled["asset_id"] = asset_id

    # Make index name consistent
    resampled.index.name = "timestamp"

    # Reorder columns
    resampled = resampled[["asset_id", "open", "high", "low", "close", "volume"]]

    print(f"Saving {len(resampled)} bars to {output_path}...")
    # Ensure directory exists
    output_path.parent.mkdir(parents=True, exist_ok=True)
    resampled.to_parquet(output_path)
    print("Done.")


def main():
    root = _data_root()
    raw_dir = root / "market_data" / "raw"
    
    input_file = raw_dir / "glbx-mdp3-20250703.trades.dbn.zst"
    output_file = raw_dir / "ES_20250703_1m.parquet"
    
    if not input_file.exists():
        print(f"Error: Input file {input_file} not found.")
        return
        
    convert_dbn_to_ohlcv(input_file, output_file)


if __name__ == "__main__":
    main()
