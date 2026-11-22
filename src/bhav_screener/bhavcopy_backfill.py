import time
from datetime import date, timedelta
from pathlib import Path

import pyarrow.parquet as pq

from bhav_screener.nse_bhavcopy_downloader import NseBhavcopyDownloader
from bhav_screener.utils.bhavcopy_cache import BhavcopyCache
from bhav_screener.utils.bhavcopy_file_parser import BhavcopyFileParser
from bhav_screener.utils.config import Settings, load_settings
from bhav_screener.utils.status import write_status
from bhav_screener.utils.universe import NiftyUniverse

# Columns stored in parquet; the parser also keeps many raw NSE columns we do not need.
STORED_COLUMNS = [
    "trade_date", "symbol", "series", "open", "high", "low", "close",
    "prev_close", "volume", "turnover_cr",
]


class BhavcopyBackfill:
    """Downloads daily bhavcopies and stores normalized rows as one parquet file per trading day.

    Every default comes from config/settings.yaml; pass an argument only to override it.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        data_dir: str | Path | None = None,
        delay: float | None = None,
        keep_raw: bool | None = None,
    ) -> None:
        self.settings = settings or load_settings()
        # Creating the universe refreshes its file when missing or older than universe.refresh_days.
        self.universe = NiftyUniverse(self.settings)
        cfg = self.settings.data
        self.data_dir = Path(data_dir) if data_dir is not None else cfg.path
        self.delay = delay if delay is not None else cfg.request_delay_seconds
        self.keep_raw = keep_raw if keep_raw is not None else cfg.keep_raw
        self.parquet_dir = cfg.layer_path("layer1", self.data_dir)
        self.parquet_dir.mkdir(parents=True, exist_ok=True)
        self.downloader = NseBhavcopyDownloader(self.settings, out_dir=self.data_dir)
        self.cache = BhavcopyCache(self.settings, data_dir=self.data_dir)

    def parquet_path(self, day: date) -> Path:
        return self.parquet_dir / f"{day:%Y-%m-%d}.parquet"

    def run(self, days_back: int | None = None, end: date | None = None) -> dict[str, int]:
        """Backfill `days_back` calendar days up to `end` (default: settings window up to today)."""
        days_back = days_back if days_back is not None else self.settings.data.history_calendar_days
        end = end or date.today()
        stats = {"saved": 0, "cached": 0, "holidays": 0, "failed": 0}
        try:
            for offset in range(days_back, -1, -1):
                day = end - timedelta(days=offset)
                if day.weekday() >= 5:
                    continue
                if self._adopt_existing_parquet(day) or self.cache.is_done(day):
                    stats["cached"] += 1
                    continue
                try:
                    self._fetch_one(day, stats, recent=(end - day).days < self.settings.data.recent_days)
                except Exception as exc:
                    stats["failed"] += 1
                    print(f"{day}: failed ({exc})")
                time.sleep(self.delay)
        finally:
            self.cache.save()
            write_status(
                "layer1_backfill",
                "ok" if not stats["failed"] else "partial",
                self.settings,
                **stats,
                window_calendar_days=days_back,
                last_stored_date=self._last_stored_date(),
            )
        return stats

    def _last_stored_date(self) -> str | None:
        files = sorted(self.parquet_dir.glob("*.parquet"))
        return files[-1].stem if files else None

    def _adopt_existing_parquet(self, day: date) -> bool:
        """Register parquet files written before the cache existed."""
        path = self.parquet_path(day)
        if day.isoformat() in self.cache.entries or not path.exists():
            return False
        self.cache.mark_saved(day, pq.ParquetFile(path).metadata.num_rows)
        return True

    def _fetch_one(self, day: date, stats: dict[str, int], recent: bool) -> None:
        zip_path = self.downloader.download(day)
        if zip_path is None:
            stats["holidays"] += 1
            if not recent:
                self.cache.mark_holiday(day)
            return
        try:
            df = BhavcopyFileParser.parse_udiff_cm(
                zip_path,
                expected_date=day,
                eq_only=self.settings.universe.eq_only,
            )
        finally:
            if not self.keep_raw:
                zip_path.unlink(missing_ok=True)
        if df.empty:
            raise ValueError("file is empty or its trade date does not match")
        df = df[STORED_COLUMNS]
        df.to_parquet(self.parquet_path(day), engine="pyarrow", compression="zstd", index=False)
        self.cache.mark_saved(day, len(df))
        stats["saved"] += 1
        print(f"{day}: {len(df)} rows")
