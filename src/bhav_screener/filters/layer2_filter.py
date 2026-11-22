from pathlib import Path

import pandas as pd

from bhav_screener.filters.corporate_actions import adjust_for_corporate_actions
from bhav_screener.utils.config import Settings, load_settings
from bhav_screener.utils.status import write_status
from bhav_screener.utils.universe import NiftyUniverse

OUTPUT_NAME = "layer2_qualified_daily.parquet"
ADJUSTED_NAME = "layer2_adjusted_daily.parquet"
RUPEES_PER_CRORE = 1e7


class Layer2Filter:
    """Layer 2: universe + EQ filter, corporate-action adjustment, indicators, quality gates.

    Reads the daily parquet files written by the backfill and writes the rows that pass every
    gate to <data dir>/results/layer2/layer2_qualified_daily.parquet. All thresholds come from
    config/settings.yaml.
    """

    def __init__(self, settings: Settings | None = None, data_dir: str | Path | None = None) -> None:
        self.settings = settings or load_settings()
        cfg = self.settings.data
        self.parquet_dir = cfg.layer_path("layer1", Path(data_dir) if data_dir is not None else None)
        self.output_path = cfg.layer_path("layer2", Path(data_dir) if data_dir is not None else None) / OUTPUT_NAME
        self.adjusted_path = self.output_path.with_name(ADJUSTED_NAME)
        self.universe = NiftyUniverse(self.settings)

    def run(self) -> pd.DataFrame:
        """Return qualified symbol-days (all days, with indicators) and save them as parquet."""
        try:
            return self._run()
        except Exception as exc:
            write_status("layer2", "failed", self.settings, error=str(exc))
            raise

    def _run(self) -> pd.DataFrame:
        df = self._load()
        if df.empty:
            raise RuntimeError(f"No bhavcopy parquet files found in {self.parquet_dir}; run the backfill first.")

        df = adjust_for_corporate_actions(df, self.settings.universe.corporate_action_threshold)
        df = self._add_indicators(df)
        mask = self._quality_mask(df)
        qualified = df[mask].reset_index(drop=True)

        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        # Layer 3 needs the full adjusted history (breakout highs, RS ranking), not only qualified rows.
        df.to_parquet(self.adjusted_path, engine="pyarrow", compression="zstd", index=False)
        qualified.to_parquet(self.output_path, engine="pyarrow", compression="zstd", index=False)

        last_day = df["trade_date"].max()
        latest = qualified[qualified["trade_date"] == last_day]
        write_status(
            "layer2",
            "ok" if len(latest) else "empty",
            self.settings,
            as_of_date=last_day.date(),
            universe_symbols=int(df["symbol"].nunique()),
            trading_days=int(df["trade_date"].nunique()),
            qualified_symbol_days=len(qualified),
            qualified_on_latest_day=len(latest),
            qualified_symbols=sorted(latest["symbol"]),
            corporate_action_symbols=sorted(df.loc[df["ca_factor"] != 1.0, "symbol"].unique()),
            qualified_file=self.output_path,
            adjusted_file=self.adjusted_path,
        )
        return qualified

    def _load(self) -> pd.DataFrame:
        files = sorted(self.parquet_dir.glob("*.parquet"))
        if not files:
            return pd.DataFrame()
        filters = []
        if self.settings.universe.eq_only:
            filters.append(("series", "==", "EQ"))
        symbols = self.universe.symbols()
        if symbols:
            filters.append(("symbol", "in", sorted(symbols)))
        return pd.concat(
            (pd.read_parquet(f, filters=filters or None) for f in files), ignore_index=True
        )

    def _add_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        sc, un = self.settings.scanner, self.settings.universe
        g = df.groupby("symbol", sort=False)

        for period in sc.sma_periods:
            df[f"sma_{period}"] = g["close"].transform(lambda s, p=period: s.rolling(p).mean())

        df["avg_volume"] = g["volume"].transform(lambda s: s.rolling(un.liquidity_window).mean())
        df["adtv_cr"] = g["turnover_cr"].transform(lambda s: s.rolling(un.liquidity_window).mean())

        true_range = pd.concat(
            [
                df["high"] - df["low"],
                (df["high"] - df["prev_close"]).abs(),
                (df["low"] - df["prev_close"]).abs(),
            ],
            axis=1,
        ).max(axis=1)
        df["atr"] = true_range.groupby(df["symbol"], sort=False).transform(
            lambda s: s.ewm(alpha=1 / sc.atr_period, adjust=False, min_periods=sc.atr_period).mean()
        )
        df["atr_pct"] = df["atr"] / df["close"] * 100
        return df

    def _quality_mask(self, df: pd.DataFrame) -> pd.Series:
        sc, un = self.settings.scanner, self.settings.universe
        mask = pd.Series(True, index=df.index)

        mask &= df["close"] >= un.min_close
        if un.max_close:
            mask &= df["close"] <= un.max_close
        mask &= df["avg_volume"] >= un.min_avg_volume
        mask &= df["adtv_cr"] >= un.min_avg_turnover / RUPEES_PER_CRORE
        mask &= df["atr_pct"].between(sc.min_atr_pct, sc.max_atr_pct)
        for period in sc.require_close_above_smas:
            mask &= df["close"] > df[f"sma_{period}"]
        return mask
