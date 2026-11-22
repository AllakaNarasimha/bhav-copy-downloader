from pathlib import Path

import numpy as np
import pandas as pd
from ta.momentum import RSIIndicator
from ta.volatility import DonchianChannel

from bhav_screener.filters.layer2_filter import ADJUSTED_NAME, OUTPUT_NAME
from bhav_screener.utils.config import PROJECT_ROOT, Settings, load_settings
from bhav_screener.utils.status import write_status

CANDIDATES_NAME = "layer3_candidates.parquet"
CANDIDATE_COLUMNS = [
    "trade_date", "symbol", "setup", "entry", "stop", "risk_per_share", "risk_pct", "target",
    "rs_score", "rsi", "volume_ratio", "atr_pct", "pct_below_52w_high", "adtv_cr",
]


def _ta_indicators(grp: pd.DataFrame, sc) -> pd.DataFrame:
    """Indicators for one symbol (sorted by date), computed with the `ta` library."""
    def channel(window: int, fillna: bool = False) -> DonchianChannel:
        return DonchianChannel(grp["high"], grp["low"], grp["close"], window=window, fillna=fillna)

    return pd.DataFrame(
        {
            "rsi": RSIIndicator(grp["close"], window=sc.rsi_period).rsi(),
            # Highest high of the PREVIOUS N days, so today's breakout bar is excluded.
            "breakout_high": channel(sc.breakout_lookback).donchian_channel_hband().shift(1),
            "recent_high": channel(sc.pullback_lookback).donchian_channel_hband(),
            "swing_low_breakout": channel(sc.stop_lookback_breakout).donchian_channel_lband(),
            "swing_low_pullback": channel(sc.stop_lookback_pullback).donchian_channel_lband(),
            # fillna=True lets the 52-week window use the history available so far.
            "high_52w": channel(sc.high_52w_days, fillna=True).donchian_channel_hband(),
        },
        index=grp.index,
    )


class SignalScreener:
    """Layer 3: breakout / pullback signals on the symbols that passed Layer 2.

    Reads data/results/layer2/layer2_*.parquet, evaluates the latest trading day, and for each triggered
    setup computes entry (close), a structural stop, risk per share, target and an RS score.
    Results go to data/results/layer3/ and data/status/layer3_status.json.
    Delivery percentage is not part of the UDiFF bhavcopy, so only the volume check is applied.
    """

    def __init__(self, settings: Settings | None = None, data_dir: str | Path | None = None) -> None:
        self.settings = settings or load_settings()
        cfg = self.settings.data
        base = Path(data_dir) if data_dir is not None else None
        layer2 = cfg.layer_path("layer2", base)
        layer3 = cfg.layer_path("layer3", base)
        self.adjusted_path = layer2 / ADJUSTED_NAME
        self.qualified_path = layer2 / OUTPUT_NAME
        self.candidates_path = layer3 / CANDIDATES_NAME
        self.results_dir = layer3

    def run(self) -> pd.DataFrame:
        """Return today's triggered candidates, ranked by RS score (best first)."""
        try:
            return self._run()
        except Exception as exc:
            write_status("layer3", "failed", self.settings, error=str(exc))
            raise

    def _run(self) -> pd.DataFrame:
        for path in (self.adjusted_path, self.qualified_path):
            if not path.exists():
                raise RuntimeError(f"{path.name} not found; run Layer2Filter first.")

        history = pd.read_parquet(self.adjusted_path)
        qualified = pd.read_parquet(self.qualified_path, columns=["trade_date", "symbol"])

        as_of = history["trade_date"].max()
        self._check_fresh(as_of)
        eligible = set(qualified.loc[qualified["trade_date"] == as_of, "symbol"])

        features = self._add_features(history)
        today = features[(features["trade_date"] == as_of) & features["symbol"].isin(eligible)]
        candidates = self._detect(today)

        csv_path = self._save_csv(candidates, as_of)
        self.candidates_path.parent.mkdir(parents=True, exist_ok=True)
        candidates.to_parquet(self.candidates_path, engine="pyarrow", compression="zstd", index=False)
        write_status(
            "layer3",
            "ok" if len(candidates) else "empty",
            self.settings,
            as_of_date=as_of.date(),
            eligible_symbols=len(eligible),
            candidates_found=len(candidates),
            setups_enabled=self.settings.scanner.setups,
            results_file=csv_path,
            candidates_file=self.candidates_path,
            delivery_check="not available in UDiFF bhavcopy; volume check only",
            candidates=candidates.assign(trade_date=candidates["trade_date"].dt.strftime("%Y-%m-%d"))
            .to_dict("records"),
        )
        return candidates

    def _check_fresh(self, as_of: pd.Timestamp) -> None:
        max_age = self.settings.data.max_data_age_days
        age = (pd.Timestamp.today().normalize() - as_of).days
        if max_age and age > max_age:
            raise RuntimeError(
                f"Newest stored day is {as_of:%Y-%m-%d} ({age} days old, limit {max_age}); "
                "re-run the download step or raise data.max_data_age_days."
            )

    def _add_features(self, df: pd.DataFrame) -> pd.DataFrame:
        sc = self.settings.scanner
        df = df.sort_values(["symbol", "trade_date"]).reset_index(drop=True)
        ta_cols = df.groupby("symbol", sort=False, group_keys=False).apply(
            lambda grp: _ta_indicators(grp, sc), include_groups=False
        )
        df = df.join(ta_cols)
        g = df.groupby("symbol", sort=False)

        df["prior_high"] = df["breakout_high"]
        df["prior_avg_volume"] = g["volume"].transform(
            lambda s: s.shift(1).rolling(self.settings.universe.liquidity_window).mean()
        )
        df["volume_ratio"] = df["volume"] / df["prior_avg_volume"]
        df["pct_below_52w_high"] = (df["high_52w"] - df["close"]) / df["high_52w"] * 100
        df["prev_day_close"] = g["close"].shift(1)

        returns = df["close"] / g["close"].shift(sc.rs_lookback) - 1
        df["rs_score"] = returns.groupby(df["trade_date"]).rank(pct=True) * 100
        return df

    def _detect(self, d: pd.DataFrame) -> pd.DataFrame:
        sc = self.settings.scanner
        d = d.copy()
        d["setup"] = self._classify_setups(d)
        common = (d["pct_below_52w_high"] <= sc.max_pct_below_52w_high) & (d["rs_score"] >= sc.min_rs_score)
        d = self._apply_stops_and_targets(d[(d["setup"] != "") & common])
        result = d[CANDIDATE_COLUMNS].sort_values("rs_score", ascending=False).reset_index(drop=True)
        return result.round({c: 2 for c in result.select_dtypes("number").columns})

    def _classify_setups(self, d: pd.DataFrame) -> np.ndarray:
        """Label each row "breakout", "pullback" or "" (breakout wins when both match)."""
        sc = self.settings.scanner
        breakout = (
            (d["close"] > d["prior_high"])
            & (d["volume_ratio"] >= sc.volume_multiple)
            & d["rsi"].between(sc.rsi_min, sc.rsi_max)
        )
        sma_pb = d[f"sma_{sc.pullback_ma}"]
        depth_pct = (d["recent_high"] / d["close"] - 1) * 100
        pullback = (
            (depth_pct >= sc.pullback_min_depth_pct)
            & (d["low"] <= sma_pb * (1 + sc.pullback_tolerance_pct / 100))
            & (d["close"] >= sma_pb)
            & (d["close"] > d["prev_day_close"])
        )

        return np.select(
            [breakout & ("breakout" in sc.setups), pullback & ("pullback" in sc.setups)],
            ["breakout", "pullback"],
            default="",
        )

    def _apply_stops_and_targets(self, d: pd.DataFrame) -> pd.DataFrame:
        """Add entry, stop, risk and target, and drop setups whose stop or target is unusable."""
        sc = self.settings.scanner
        d = d.copy()
        d["entry"] = d["close"]
        ia = self.settings.intraday
        if ia.enabled:
            # Same-session trade: ATR stop and target, and the target must fit inside one day's range.
            d["stop"] = d["entry"] - ia.stop_atr * d["atr"]
            reward_risk, max_stop_pct = ia.reward_risk, ia.max_stop_pct
        else:
            # Chart stop first: swing low of the setup minus an ATR buffer, never tightened to fit a budget.
            swing_low = np.where(d["setup"] == "breakout", d["swing_low_breakout"], d["swing_low_pullback"])
            d["stop"] = swing_low - sc.stop_atr_buffer * d["atr"]
            # A stop closer than min_stop_atr x ATR would sit inside daily noise, so move it out.
            d["stop"] = np.minimum(d["stop"], d["entry"] - sc.min_stop_atr * d["atr"])
            reward_risk, max_stop_pct = self.settings.risk.reward_risk, sc.max_stop_pct
        d["risk_per_share"] = d["entry"] - d["stop"]
        d["risk_pct"] = d["risk_per_share"] / d["entry"] * 100
        d["target"] = d["entry"] + reward_risk * d["risk_per_share"]
        # Too-wide stops are skipped rather than forced into a bad level.
        keep = (d["risk_per_share"] > 0) & (d["risk_pct"] <= max_stop_pct)
        if ia.enabled:
            keep &= (d["target"] - d["entry"]) <= ia.max_target_atr * d["atr"]
        return d[keep]

    def _save_csv(self, candidates: pd.DataFrame, as_of: pd.Timestamp) -> Path:
        self.results_dir.mkdir(parents=True, exist_ok=True)
        path = self.results_dir / f"layer3_candidates_{as_of:%Y-%m-%d}.csv"
        candidates.to_csv(path, index=False)
        return path
