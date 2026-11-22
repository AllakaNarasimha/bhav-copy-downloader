import math
import time
from dataclasses import dataclass, field, fields
from pathlib import Path

import pandas as pd
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CONFIG = PROJECT_ROOT / "config" / "settings.yaml"
# Extra trading days beyond the longest indicator window, so it is fully populated on recent days.
MIN_HISTORY_MARGIN_DAYS = 5


@dataclass
class DataConfig:
    dir: str = "data"
    history_trading_days: int = 260
    # Layer 3 fails when the newest stored day is older than this many calendar days (0 = no check).
    max_data_age_days: int = 4
    request_delay_seconds: float = 0.5
    keep_raw: bool = False
    url_template: str = (
        "https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{date:%Y%m%d}_F_0000.csv.zip"
    )
    user_agent: str = "Mozilla/5.0"
    timeout_seconds: float = 30
    # A 404 within this many days of today is not cached as a holiday (NSE may not have published yet).
    recent_days: int = 3
    # All layer results (results/layer2, results/layer3, results/layer4), relative to `dir`.
    results_dir: str = "results"
    # Per-layer status JSON files (consumed by the next step), relative to `dir`.
    status_dir: str = "status"

    def layer_path(self, layer: str, base: Path | None = None) -> Path:
        return (base or self.path) / self.results_dir / layer

    @property
    def status_path(self) -> Path:
        return self.path / self.status_dir

    @property
    def path(self) -> Path:
        return PROJECT_ROOT / self.dir

    @property
    def history_calendar_days(self) -> int:
        # ~252 trading days per 365 calendar days, plus a buffer for uneven holidays.
        return math.ceil(self.history_trading_days * 365 / 252) + 10


@dataclass
class UniverseConfig:
    eq_only: bool = True
    symbols: list[str] = field(default_factory=list)
    # CSV with columns symbol,industry. Created from index_url, then refreshed weekly; "" = all EQ symbols.
    universe_file: str = "config/universe_nifty200.csv"
    # Source used to create universe_file.
    index_url: str = "https://www.niftyindices.com/IndexConstituent/ind_nifty200list.csv"
    # Sanity check on the downloaded list; fewer symbols is treated as a bad download.
    min_index_symbols: int = 150
    # Re-download the universe file when it is older than this many days.
    refresh_days: int = 7
    # A one-day gap between prev_close and the earlier close larger than this fraction is treated
    # as a split/bonus and the earlier prices are adjusted. Smaller gaps (dividends) are ignored.
    corporate_action_threshold: float = 0.05
    min_close: float = 50
    max_close: float = 0
    min_avg_volume: float = 200_000
    min_avg_turnover: float = 20_000_000
    liquidity_window: int = 20

    def resolve_symbols(self) -> set[str] | None:
        """Return the symbol set, or None meaning 'all symbols'."""
        result = {s.strip().upper() for s in self.symbols if s.strip()}
        if self.universe_file:
            path = PROJECT_ROOT / self.universe_file
            result |= {s.strip().upper() for s in pd.read_csv(path)["symbol"].dropna().astype(str)}
        return result or None


@dataclass
class ScannerConfig:
    sma_periods: list[int] = field(default_factory=lambda: [20, 50, 200])
    require_close_above_smas: list[int] = field(default_factory=lambda: [50, 200])
    breakout_lookback: int = 20
    volume_multiple: float = 1.5
    rsi_period: int = 14
    rsi_min: float = 55
    rsi_max: float = 80
    max_pct_below_52w_high: float = 15
    atr_period: int = 14
    # ATR as % of close must stay inside this band (too low = no movement, too high = too wild).
    min_atr_pct: float = 1.5
    max_atr_pct: float = 6.0
    # Setups to look for: "breakout" and/or "pullback".
    setups: list[str] = field(default_factory=lambda: ["breakout", "pullback"])
    # Pullback: price dipped to the moving average (tolerance %) after falling at least
    # pullback_min_depth_pct from its high of the last pullback_lookback days, then closed back above it.
    pullback_ma: int = 20
    pullback_tolerance_pct: float = 2.0
    pullback_min_depth_pct: float = 3.0
    pullback_lookback: int = 10
    # Structural stop (chart first): breakout stops sit below the lowest low of the last
    # stop_lookback_breakout days (the base floor); pullback stops sit below the lowest low of the last
    # stop_lookback_pullback days (the dip low). Both get a buffer of stop_atr_buffer x ATR.
    stop_lookback_breakout: int = 7
    stop_lookback_pullback: int = 10
    stop_atr_buffer: float = 0.15
    # The stop must be at least this many ATRs below entry (outside normal daily noise).
    min_stop_atr: float = 0.5
    # A setup whose structural stop is wider than this % of entry is skipped, never tightened.
    max_stop_pct: float = 6.5
    # Relative strength: return over rs_lookback days, ranked 0-100 across the whole universe.
    rs_lookback: int = 126
    min_rs_score: float = 0
    # Window (trading days) for the "52-week" high.
    high_52w_days: int = 252


@dataclass
class RiskConfig:
    # Target = entry + reward_risk * (entry - stop).
    reward_risk: float = 2.0
    # Order candidates are funded in: "stop_pct" (tightest structural stop first, RS score breaks ties)
    # or "rs_score" (strongest relative strength first).
    rank_by: str = "stop_pct"
    # Hard caps in INR: risk of one trade (qty x risk per share) and total new risk per day.
    max_risk_per_trade_inr: float = 10_000
    max_daily_risk_inr: float = 50_000
    # Total trading capital in INR.
    capital: float = 1_000_000
    # A single position's value may not exceed this % of capital.
    max_position_pct: float = 20
    max_positions: int = 10
    # Max positions from the same sector/industry.
    max_per_sector: int = 2


@dataclass
class IntradayConfig:
    # true = same-session trades: ATR-based stop and target, EOD square-off tag.
    # false = positional trades: structural swing-low stop from `scanner`.
    enabled: bool = True
    # Stop distance = stop_atr x ATR. Daily swing lows are too far away for a one-day trade.
    stop_atr: float = 0.6
    reward_risk: float = 1.5
    # The target must be reachable in one session: target distance <= max_target_atr x ATR.
    max_target_atr: float = 1.2
    # Skip setups whose stop is wider than this % of entry.
    max_stop_pct: float = 3.0
    # Written on every approved trade so the execution step never leaves an overnight position.
    exit_policy: str = "EOD_MANDATORY_CLOSE"
    square_off_time: str = "15:15"


@dataclass
class Settings:
    data: DataConfig = field(default_factory=DataConfig)
    universe: UniverseConfig = field(default_factory=UniverseConfig)
    scanner: ScannerConfig = field(default_factory=ScannerConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    intraday: IntradayConfig = field(default_factory=IntradayConfig)

    def validate(self) -> None:
        try:
            time.strptime(self.intraday.square_off_time, "%H:%M")
        except ValueError:
            raise ValueError(f"square_off_time {self.intraday.square_off_time!r} must look like '15:15'")
        unknown = set(self.scanner.setups) - {"breakout", "pullback"}
        if unknown:
            raise ValueError(f"Unknown setups {sorted(unknown)}; use 'breakout' and/or 'pullback'")
        if "pullback" in self.scanner.setups and self.scanner.pullback_ma not in self.scanner.sma_periods:
            raise ValueError(f"pullback_ma {self.scanner.pullback_ma} must be listed in sma_periods")
        missing = [p for p in self.scanner.require_close_above_smas if p not in self.scanner.sma_periods]
        if missing:
            raise ValueError(f"require_close_above_smas {missing} must also be listed in sma_periods")
        if self.risk.rank_by not in {"stop_pct", "rs_score"}:
            raise ValueError(f"rank_by {self.risk.rank_by!r} must be 'stop_pct' or 'rs_score'")
        positive = {
            "risk.capital": self.risk.capital,
            "risk.max_risk_per_trade_inr": self.risk.max_risk_per_trade_inr,
            "risk.max_daily_risk_inr": self.risk.max_daily_risk_inr,
            "risk.max_positions": self.risk.max_positions,
            "risk.max_per_sector": self.risk.max_per_sector,
            "intraday.stop_atr": self.intraday.stop_atr,
            "intraday.reward_risk": self.intraday.reward_risk,
            "intraday.max_target_atr": self.intraday.max_target_atr,
            "intraday.max_stop_pct": self.intraday.max_stop_pct,
        }
        bad = [name for name, value in positive.items() if value <= 0]
        if bad:
            raise ValueError(f"{bad} must be greater than 0")
        if not 0 < self.risk.max_position_pct <= 100:
            raise ValueError("risk.max_position_pct must be between 0 and 100")
        needed = max(*self.scanner.sma_periods, self.scanner.high_52w_days) + MIN_HISTORY_MARGIN_DAYS
        if self.data.history_trading_days < needed:
            raise ValueError(
                f"history_trading_days={self.data.history_trading_days} is too short; the longest "
                f"indicator window plus a {MIN_HISTORY_MARGIN_DAYS}-day margin needs at least {needed}"
            )


def load_settings(path: str | Path = DEFAULT_CONFIG) -> Settings:
    """Load settings.yaml over the defaults; unknown sections or keys raise an error."""
    settings = Settings()
    path = Path(path)
    raw = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}) if path.exists() else {}
    sections = {f.name: getattr(settings, f.name) for f in fields(settings)}
    for name, values in raw.items():
        if name not in sections:
            raise ValueError(f"Unknown config section '{name}' in {path}")
        section = sections[name]
        valid = {f.name for f in fields(section)}
        for key, value in (values or {}).items():
            if key not in valid:
                raise ValueError(f"Unknown key '{key}' in [{name}] of {path}")
            setattr(section, key, value)
    settings.validate()
    return settings
