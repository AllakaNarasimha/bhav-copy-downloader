import time
from io import StringIO
from pathlib import Path

import pandas as pd
import requests

from bhav_screener.utils.config import PROJECT_ROOT, Settings, load_settings

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        " (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/csv,text/plain,*/*",
    "Referer": "https://www.niftyindices.com/",
}


def fetch_index_constituents(url: str, min_symbols: int, timeout: float = 20) -> pd.DataFrame:
    """Download an index constituent CSV; return a DataFrame of sorted unique `symbol` + `industry`."""
    resp = requests.get(url, headers=HEADERS, timeout=timeout)
    resp.raise_for_status()

    df = pd.read_csv(StringIO(resp.text))
    df.columns = [str(c).strip() for c in df.columns]
    symbol_col = next((c for c in df.columns if c.lower() in {"symbol", "tckrsymb"}), None)
    if symbol_col is None:
        raise ValueError(f"Symbol column not found in CSV. Columns: {list(df.columns)}")
    industry_col = next((c for c in df.columns if c.lower() == "industry"), None)

    out = pd.DataFrame(
        {
            "symbol": df[symbol_col].astype(str).str.strip().str.upper(),
            "industry": df[industry_col].astype(str).str.strip() if industry_col else "Unknown",
        }
    )
    out = out[(out["symbol"] != "") & ~out["symbol"].str.startswith("NIFTY") & (out["symbol"] != "NAN")]
    out = out.drop_duplicates("symbol").sort_values("symbol").reset_index(drop=True)
    if len(out) < min_symbols:
        raise ValueError(f"Unexpectedly few symbols parsed: {len(out)} (expected >= {min_symbols})")
    return out


class NiftyUniverse:
    """Keeps the universe file (config/universe_nifty200.csv with symbol + industry) present and fresh.

    On creation the file is downloaded if missing, or refreshed if older than
    universe.refresh_days. If a refresh fails, the existing file is kept. Defaults come
    from config/settings.yaml.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        path: str | Path | None = None,
    ) -> None:
        self.cfg = (settings or load_settings()).universe
        self.path = Path(path) if path is not None else PROJECT_ROOT / self.cfg.universe_file
        if self.cfg.universe_file or path is not None:
            self._refresh_if_needed()

    def is_stale(self) -> bool:
        if not self.path.exists():
            return True
        age_days = (time.time() - self.path.stat().st_mtime) / 86400
        return age_days >= self.cfg.refresh_days

    def symbols(self) -> set[str] | None:
        """Symbols from the file plus any listed in settings; None means 'all symbols'."""
        return self.cfg.resolve_symbols()

    def sector_map(self) -> dict[str, str]:
        """symbol -> industry; empty if the sector file is unavailable."""
        if not self.path.exists():
            return {}
        df = pd.read_csv(self.path)
        return dict(zip(df["symbol"], df["industry"]))

    def _refresh_if_needed(self) -> None:
        if not self.is_stale():
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            table = fetch_index_constituents(self.cfg.index_url, self.cfg.min_index_symbols)
        except Exception as exc:
            if self.path.exists():
                print(f"[WARN] Could not refresh {self.path.name} ({exc}); keeping the existing file.")
                return
            raise RuntimeError(f"Could not create {self.path}: {exc}") from exc
        table.to_csv(self.path, index=False)
        print(f"Updated {self.path.name}: {len(table)} symbols")
