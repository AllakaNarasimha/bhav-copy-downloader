import pandas as pd

PRICE_COLS = ["open", "high", "low", "close", "prev_close"]
# On a normal day prev_close equals the earlier close; more than this share of symbols differing by
# more than GAP_TOLERANCE means a day is missing from the data.
GAP_TOLERANCE = 0.001
GAP_SYMBOL_SHARE = 0.3


def adjust_for_corporate_actions(df: pd.DataFrame, threshold: float = 0.05) -> pd.DataFrame:
    """Back-adjust prices and volume for splits and bonus issues.

    NSE restates `prev_close` on the ex-date, so a mismatch between a day's `prev_close` and the
    previous row's `close` marks a corporate action. Its ratio becomes the factor applied to all
    earlier rows of that symbol. Gaps smaller than `threshold` (dividends, rounding) are ignored.

    Input needs columns: trade_date, symbol, open, high, low, close, prev_close, volume.
    Adds `ca_factor` (the cumulative factor applied to each row; 1.0 = unadjusted).
    """
    df = df.sort_values(["symbol", "trade_date"]).reset_index(drop=True)
    by_symbol = df.groupby("symbol", sort=False)

    ratio = df["prev_close"] / by_symbol["close"].shift(1)
    event = ratio.where((ratio - 1).abs() > threshold, 1.0).fillna(1.0)
    # After a missing day (failed download) most symbols mismatch at once; that is a data gap, not a split.
    mismatch_share = ((ratio - 1).abs() > GAP_TOLERANCE).groupby(df["trade_date"]).transform("mean")
    event = event.where(mismatch_share <= GAP_SYMBOL_SHARE, 1.0)

    # Factor for a row = product of the events that happen after it.
    later_events = event.groupby(df["symbol"], sort=False).shift(-1).fillna(1.0)
    factor = (
        later_events.iloc[::-1].groupby(df["symbol"].iloc[::-1], sort=False).cumprod().iloc[::-1]
    )

    df[PRICE_COLS] = df[PRICE_COLS].mul(factor, axis=0)
    df["volume"] = df["volume"] / factor
    df["ca_factor"] = factor
    return df
