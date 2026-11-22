from datetime import date
from pathlib import Path

import requests

from bhav_screener.utils.config import Settings, load_settings


class NseBhavcopyDownloader:
    """Downloads NSE cash-market (UDiFF) bhavcopy zips. Defaults come from config/settings.yaml."""

    def __init__(
        self,
        settings: Settings | None = None,
        out_dir: str | Path | None = None,
        url_template: str | None = None,
        user_agent: str | None = None,
        timeout: float | None = None,
    ) -> None:
        cfg = (settings or load_settings()).data
        self.out_dir = Path(out_dir) if out_dir is not None else cfg.path
        self.url_template = url_template or cfg.url_template
        self.timeout = timeout if timeout is not None else cfg.timeout_seconds
        self.raw_dir = cfg.layer_path("layer1", Path(out_dir) if out_dir is not None else None) / "raw"
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": user_agent or cfg.user_agent, "Accept": "*/*"})

    def download(self, day: date) -> Path | None:
        """Save the raw zip for `day`; return its path, or None if NSE has no file (holiday/weekend)."""
        resp = self.session.get(self.url_template.format(date=day), timeout=self.timeout)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        path = self.raw_dir / f"BhavCopy_NSE_CM_0_0_0_{day:%Y%m%d}_F_0000.csv.zip"
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(resp.content)
        tmp.replace(path)
        return path
