import json
from datetime import date
from pathlib import Path

from bhav_screener.utils.config import Settings, load_settings

SAVED = "saved"
HOLIDAY = "holiday"


class BhavcopyCache:
    """Remembers which dates were already handled, so re-runs make no network calls for them.

    File layout (<data dir>/cache/bhavcopy-cache.json):
        {"2026-11-20": {"status": "saved", "rows": 2760},
         "2026-11-14": {"status": "holiday"}}
    Failed dates are never stored, so they are retried on the next run.
    """

    def __init__(self, settings: Settings | None = None, data_dir: str | Path | None = None) -> None:
        settings = settings or load_settings()
        data_dir = Path(data_dir) if data_dir is not None else settings.data.path
        self.parquet_dir = settings.data.layer_path("layer1", data_dir)
        self.path = data_dir / "cache" / "bhavcopy-cache.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.entries: dict[str, dict] = (
            json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        )

    def is_done(self, day: date) -> bool:
        entry = self.entries.get(day.isoformat())
        if entry is None:
            return False
        if entry["status"] == SAVED:
            # A cache entry without its parquet file is stale.
            return (self.parquet_dir / f"{day:%Y-%m-%d}.parquet").exists()
        return True

    def mark_saved(self, day: date, rows: int) -> None:
        self.entries[day.isoformat()] = {"status": SAVED, "rows": rows}

    def mark_holiday(self, day: date) -> None:
        self.entries[day.isoformat()] = {"status": HOLIDAY}

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(dict(sorted(self.entries.items())), indent=1), encoding="utf-8")
        tmp.replace(self.path)
