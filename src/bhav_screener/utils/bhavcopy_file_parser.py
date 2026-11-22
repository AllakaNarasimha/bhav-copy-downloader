from datetime import date
from io import BytesIO, StringIO
from pathlib import Path
from typing import Iterable, Optional
import zipfile
import pandas as pd


class BhavcopyFileParser:
  """Utility class for reading and normalizing NSE Bhavcopy files (.csv or .zip)."""

  UDIFF_RENAME_MAP = {
      "TckrSymb": "symbol",
      "SctySrs": "series",
      "OpnPric": "open",
      "HghPric": "high",
      "LwPric": "low",
      "ClsPric": "close",
      "PrvsClsgPric": "prev_close",
      "TtlTradgVol": "volume",
  }

  UDIFF_NUMERIC_COLS = [
      "OpnPric",
      "HghPric",
      "LwPric",
      "ClsPric",
      "PrvsClsgPric",
      "TtlTradgVol",
      "TtlTrfVal",
  ]

  @staticmethod
  def read_raw_dataframe(file_path: str | Path) -> pd.DataFrame:
    """Reads a Bhavcopy .csv or .zip file into a cleaned string DataFrame."""
    path = Path(file_path)
    if not path.exists():
      return pd.DataFrame()

    if path.suffix.lower() == ".zip":
      with zipfile.ZipFile(path, "r") as zf:
        csv_names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        if not csv_names:
          return pd.DataFrame()
        content = zf.read(csv_names[0]).decode("utf-8", errors="replace")
      df = pd.read_csv(StringIO(content), dtype=str)
    else:
      df = pd.read_csv(path, dtype=str)

    if df.empty:
      return pd.DataFrame()

    df.columns = [c.strip() for c in df.columns]
    for col in df.select_dtypes(include="object").columns:
      df[col] = df[col].str.strip()
    return df

  @staticmethod
  def validate_trade_date(df: pd.DataFrame, expected_date: date) -> bool:
    """Checks whether TradDt inside a UDiFF DataFrame matches expected_date."""
    if df.empty or "TradDt" not in df.columns:
      return False
    parsed = pd.to_datetime(df["TradDt"], errors="coerce").dropna()
    if parsed.empty:
      return False
    return bool(parsed.iloc[0].date() == expected_date)

  @classmethod
  def parse_udiff_cm(
      cls,
      file_path: str | Path,
      expected_date: Optional[date] = None,
      universe: Optional[Iterable[str]] = None,
      eq_only: bool = True,
  ) -> pd.DataFrame:
    """Parses a UDiFF CM Bhavcopy file into standardized screener columns."""
    df = cls.read_raw_dataframe(file_path)
    if df.empty:
      return pd.DataFrame()

    if expected_date is not None and not cls.validate_trade_date(
        df, expected_date
    ):
      return pd.DataFrame()

    if eq_only and "SctySrs" in df.columns:
      df = df[df["SctySrs"] == "EQ"]

    if universe is not None and "TckrSymb" in df.columns:
      allowed = {s.strip().upper() for s in universe}
      df = df[df["TckrSymb"].isin(allowed)]

    if df.empty:
      return pd.DataFrame()

    for col in cls.UDIFF_NUMERIC_COLS:
      if col in df.columns:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    if "TradDt" in df.columns:
      parsed_dt = pd.to_datetime(df["TradDt"], errors="coerce")
      df["trade_date"] = parsed_dt
    elif expected_date is not None:
      df["trade_date"] = pd.to_datetime(expected_date)

    if "TtlTrfVal" in df.columns:
      df["turnover_cr"] = df["TtlTrfVal"] / 1e7

    df = df.rename(
        columns={
            k: v for k, v in cls.UDIFF_RENAME_MAP.items() if k in df.columns
        }
    )
    return df.reset_index(drop=True)